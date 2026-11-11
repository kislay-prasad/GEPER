"""
The LIMS export gate checks that a run's sign-off PUBLICATION is complete, not only that its results JSON says "reviewed".

Background. `review/signoff.py::approve` publishes a manifest, two PDFs and the reviewed results JSON (crash-safe
publication; docs/SIGNOFF_RECOVERY_DESIGN.md, section 1A). `require_reviewed` can verify that publication when it is
given the run directory, but `report/export_lims.py` passed none, so an automated consumer could still be handed a run
whose manifest was missing or whose PDFs were missing, truncated or replaced, as long as the JSON said "reviewed".

Behaviour tested here (DB-free; real approvals of an unlinked run in temporary directories):
  * With a run directory (`run_dir=`, which the CLI always supplies as the directory of the results JSON) the export is
    refused unless the manifest exists and every artefact it lists still matches. The check is READ-ONLY: it writes no
    "verified" audit line, so a refused export leaves only the existing `export_blocked` entry.
  * Without a run directory (a library call holding only a document) nothing can be checked and the earlier behaviour
    applies. That is an intentional, documented compatibility limit.
"""

import csv
import json
import os
import sys

import pytest

from report import export_lims as lims
from report.export_lims import LIMSExportBlockedError
from review import signoff as s
from tests.test_signoff import _make_document, _write_run


@pytest.fixture
def signed(tmp_path):
    run = _write_run(str(tmp_path))
    s.approve(run, "Dr. Rajesh Sharma", "MCI-12345", "AIIMS Delhi")
    return run


def document_of(run):
    with open(os.path.join(run, s.RESULTS_FILENAME), encoding="utf-8") as fh:
        return json.load(fh)


def audit(run):
    path = os.path.join(run, s.AUDIT_LOG_FILENAME)
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def export_json(run, out=None, **kwargs):
    out = out or os.path.join(run, "lims.json")
    return lims.export_lims_json(document_of(run), out, run_dir=run, **kwargs)


def export_csv(run, out=None, **kwargs):
    out = out or os.path.join(run, "lims.csv")
    return lims.export_lims_csv(document_of(run), out, run_dir=run, **kwargs)


def _delete(run, name):
    os.remove(os.path.join(run, name))


def _truncate(run, name):
    path = os.path.join(run, name)
    data = open(path, "rb").read()
    open(path, "wb").write(data[: len(data) // 2])


def _edit_manifest(run, mutate):
    path = os.path.join(run, s.MANIFEST_FILENAME)
    manifest = json.load(open(path, encoding="utf-8"))
    mutate(manifest)
    json.dump(manifest, open(path, "w", encoding="utf-8"))


def _edit_results(run):
    document = document_of(run)
    document["variant_count"] = document.get("variant_count", 0) + 1
    json.dump(document, open(os.path.join(run, s.RESULTS_FILENAME), "w", encoding="utf-8"), indent=2)


DAMAGE = {
    "manifest_missing": lambda run: _delete(run, s.MANIFEST_FILENAME),
    "manifest_malformed": lambda run: open(os.path.join(run, s.MANIFEST_FILENAME), "w").write("{not json"),
    "manifest_lacks_a_hash": lambda run: _edit_manifest(run, lambda m: m.pop("short_pdf_sha256")),
    "manifest_content_hash_differs": lambda run: _edit_manifest(run, lambda m: m.update(content_hash="f" * 64)),
    "full_pdf_missing": lambda run: _delete(run, s.FULL_PDF_FILENAME),
    "short_pdf_missing": lambda run: _delete(run, s.SHORT_PDF_FILENAME),
    "full_pdf_truncated": lambda run: _truncate(run, s.FULL_PDF_FILENAME),
    "short_pdf_truncated": lambda run: _truncate(run, s.SHORT_PDF_FILENAME),
    "full_pdf_replaced": lambda run: open(os.path.join(run, s.FULL_PDF_FILENAME), "wb").write(b"%PDF-other"),
    "results_json_modified": _edit_results,
}


# ═══ a complete, reviewed publication still exports ═══


def test_a_complete_reviewed_publication_exports_to_json_and_csv(signed):
    out_json, out_csv = export_json(signed), export_csv(signed)
    assert json.load(open(out_json, encoding="utf-8"))["export_format_version"] == "1.0.0"
    with open(out_csv, encoding="utf-8", newline="") as fh:
        assert csv.DictReader(fh).fieldnames == lims._CSV_COLUMNS
    lims.build_lims_export(document_of(signed), run_dir=signed)  # the library choke point, given the directory


def test_a_successful_export_adds_no_audit_entry_from_the_completeness_check(signed):
    before = audit(signed)
    export_json(signed)
    assert audit(signed) == before  # the check is read-only: no new "verified" line appears


# ═══ an incomplete or inconsistent publication is refused ═══


@pytest.mark.parametrize("damage", sorted(DAMAGE))
def test_json_export_is_refused_for_an_incomplete_or_inconsistent_publication(signed, damage):
    export_json(signed)  # control: intact, exports
    os.remove(os.path.join(signed, "lims.json"))
    DAMAGE[damage](signed)
    out = os.path.join(signed, "lims.json")
    with pytest.raises(LIMSExportBlockedError):
        export_json(signed, out)
    assert not os.path.exists(out)  # no export file is created


@pytest.mark.parametrize("damage", sorted(DAMAGE))
def test_csv_export_is_refused_for_an_incomplete_or_inconsistent_publication(signed, damage):
    DAMAGE[damage](signed)
    out = os.path.join(signed, "lims.csv")
    with pytest.raises(LIMSExportBlockedError):
        export_csv(signed, out)
    assert not os.path.exists(out)


@pytest.mark.parametrize("damage", sorted(set(DAMAGE) - {"results_json_modified"}))
def test_the_refusal_names_an_incomplete_publication(signed, damage):
    DAMAGE[damage](signed)
    with pytest.raises(LIMSExportBlockedError, match="incomplete or inconsistent"):
        export_json(signed)


def test_a_reviewed_json_with_no_manifest_is_refused_the_state_the_old_order_could_leave(signed):
    _delete(signed, s.MANIFEST_FILENAME)
    assert document_of(signed)["review_status"] == "reviewed"
    with pytest.raises(LIMSExportBlockedError, match="manifest_missing"):
        export_json(signed)


# ═══ a rejected export leaves an honest audit trail ═══


@pytest.mark.parametrize("damage", ["manifest_missing", "short_pdf_truncated", "full_pdf_missing"])
def test_a_rejected_export_records_export_blocked_and_nothing_that_looks_like_success(signed, damage):
    before = [entry["action"] for entry in audit(signed)]
    DAMAGE[damage](signed)
    with pytest.raises(LIMSExportBlockedError):
        export_json(signed)
    actions = [entry["action"] for entry in audit(signed)]
    assert actions == before + ["export_blocked"]
    blocked = audit(signed)[-1]
    assert blocked["format"] == "json" and blocked["reason"] and "incomplete or inconsistent" in blocked["reason"]
    assert not any(a in ("content_verified", "exported", "export_succeeded") for a in actions[len(before) :])


def test_a_rejected_csv_export_records_export_blocked_too(signed):
    _delete(signed, s.MANIFEST_FILENAME)
    with pytest.raises(LIMSExportBlockedError):
        export_csv(signed)
    assert audit(signed)[-1]["action"] == "export_blocked" and audit(signed)[-1]["format"] == "csv"


# ═══ unreviewed reports are still refused for review first ═══


def test_an_unreviewed_report_is_refused_for_its_review_status_before_anything_else(tmp_path):
    run = _write_run(str(tmp_path))  # a draft: no manifest, no PDFs
    with pytest.raises(LIMSExportBlockedError, match="review_status"):
        lims.export_lims_json(_make_document(), os.path.join(run, "x.json"), run_dir=run)
    assert not os.path.exists(os.path.join(run, "x.json"))


def test_a_withdrawn_report_is_refused(signed):
    s.withdraw(signed, "signed in error", "actor@example.test")
    with pytest.raises(LIMSExportBlockedError, match="review_status"):
        export_json(signed)


# ═══ compatibility ═══


def test_a_document_only_call_without_a_directory_keeps_the_earlier_behaviour(signed):
    """Intentional compatibility limit: with no `run_dir` nothing on disk can be checked."""
    _delete(signed, s.MANIFEST_FILENAME)
    out = os.path.join(signed, "lims.json")
    lims.export_lims_json(document_of(signed), out)  # no run_dir
    assert os.path.exists(out)
    lims.build_lims_export(document_of(signed))  # and no run_dir here either


def test_a_document_that_does_not_belong_to_the_given_directory_is_refused(signed, tmp_path):
    other = _write_run(str(tmp_path), "other_run")
    results = os.path.join(other, s.RESULTS_FILENAME)
    different = json.load(open(results, encoding="utf-8"))
    different["variants"] = different["variants"][:1]  # genuinely different clinical content, so a different hash
    json.dump(different, open(results, "w", encoding="utf-8"))
    s.approve(other, "Dr. Other", "MCI-99999", "Elsewhere")
    foreign = document_of(other)  # reviewed, hash-valid, but not the document this directory's manifest describes
    with pytest.raises(LIMSExportBlockedError, match="incomplete or inconsistent"):
        lims.export_lims_json(foreign, os.path.join(signed, "x.json"), run_dir=signed)


def test_the_cli_exports_a_complete_run_and_refuses_an_incomplete_one(signed, monkeypatch):
    out = os.path.join(signed, "cli.json")
    results = os.path.join(signed, s.RESULTS_FILENAME)
    monkeypatch.setattr(sys, "argv", ["export_lims", results, "--json", out])
    lims._main()
    assert os.path.exists(out)
    os.remove(out)
    _delete(signed, s.FULL_PDF_FILENAME)
    with pytest.raises(LIMSExportBlockedError, match="incomplete or inconsistent"):
        lims._main()
    assert not os.path.exists(out)


def test_a_linked_run_with_an_incomplete_publication_is_refused_for_that_before_its_status_is_read(signed):
    with open(os.path.join(signed, s.CLINICAL_LINK_FILENAME), "w", encoding="utf-8") as fh:
        json.dump({"org_id": "o", "interpretation_id": "i", "report_id": "r"}, fh)
    _delete(signed, s.MANIFEST_FILENAME)
    with pytest.raises(LIMSExportBlockedError, match="incomplete or inconsistent"):
        export_json(signed)  # no report_status supplied; the publication problem is reported first


# ═══ a document is bound to the directory it is exported from ═══
#
# GEPER assigns no run identifier of its own (report/summary.py::_derive_run_id says so), and the content hash EXCLUDES
# workflow state and timestamps, so two runs with identical clinical content share a content hash and no identifier tells
# them apart. What does identify the run is the signed results file itself: the manifest records the SHA-256 of
# `geper_results.json`'s bytes. The exporter therefore requires the document it is handed to equal that file.


def _run_with_identity(root, name, generated_at, input_vcf):
    run = _write_run(str(root), name)
    path = os.path.join(run, s.RESULTS_FILENAME)
    document = json.load(open(path, encoding="utf-8"))
    document["generated_at"], document["input_vcf"] = generated_at, input_vcf
    json.dump(document, open(path, "w", encoding="utf-8"))
    s.approve(run, "Dr. Rajesh Sharma", "MCI-12345", "AIIMS Delhi")
    return run


def test_two_runs_with_identical_clinical_content_but_different_identities_cannot_be_swapped(tmp_path):
    run_a = _run_with_identity(tmp_path, "run_a", "2026-08-01T00:00:00+00:00", "patient_a.vcf")
    run_b = _run_with_identity(tmp_path, "run_b", "2026-09-15T12:30:00+00:00", "patient_b.vcf")
    doc_a, doc_b = document_of(run_a), document_of(run_b)
    assert doc_a["content_hash"] == doc_b["content_hash"]  # the content hash cannot tell them apart...
    assert (doc_a["generated_at"], doc_a["input_vcf"]) != (doc_b["generated_at"], doc_b["input_vcf"])
    export_json(run_a)  # each exports from its own directory
    export_json(run_b)
    out = os.path.join(run_a, "swapped.json")
    with pytest.raises(LIMSExportBlockedError, match="document_differs_from_results_json"):
        lims.export_lims_json(doc_b, out, run_dir=run_a)  # ...but the document is bound to the file the manifest signed
    with pytest.raises(LIMSExportBlockedError, match="document_differs_from_results_json"):
        lims.export_lims_csv(doc_a, os.path.join(run_b, "swapped.csv"), run_dir=run_b)
    assert not os.path.exists(out)
    assert audit(run_a)[-1]["action"] == "export_blocked"


def test_a_document_edited_in_memory_after_loading_is_not_the_signed_document(signed):
    document = document_of(signed)
    document["input_vcf"] = "edited-in-memory.vcf"  # not part of the content hash, but not what was signed
    with pytest.raises(LIMSExportBlockedError, match="document_differs_from_results_json"):
        lims.export_lims_json(document, os.path.join(signed, "x.json"), run_dir=signed)


def test_the_directory_aware_gate_applies_the_same_binding(signed):
    document = document_of(signed)
    document["input_vcf"] = "other.vcf"
    with pytest.raises(s.SignoffError, match="document_differs_from_results_json"):
        s.require_reviewed(document, consumer="test", output_dir=signed)
    s.require_reviewed(document_of(signed), consumer="test", output_dir=signed)  # the genuine document still passes
