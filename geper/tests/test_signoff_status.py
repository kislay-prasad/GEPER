"""
tests/test_signoff_status.py
────────────────────────────

`review/signoff_status.py`: the READ-ONLY sign-off status classifier, and the `status` CLI command.

THIS FILE needs no clinical service and no database: it feeds the classifier facts directly, builds run directories on
disk, and drives the CLI where the answer does not depend on a server (an unlinked run; a linked run with no credentials).
The behaviour that DOES involve the service (authentication, malformed answers, state changing between reads, and the
guarantee that only GET requests reach the review routes) is pinned in api/tests/test_signoff_status_http.py.

What is pinned here:
  * every classification, with its group, its code, its exit code and whether it is a HOLD;
  * the local inspection: manifest problems, the document's own hash, overrides, the journal's mere presence;
  * the command writes NOTHING: not a journal, not a log, not a marker, and it leaves existing files byte-identical;
  * the output never carries a raw hash value, and always says content binding is NOT verified.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid

import pytest

from report.models import compute_content_hash
from review import cli, signoff_status as st

ORG = uuid.uuid4()
INTERP = uuid.uuid4()
REPORT = uuid.uuid4()
ME = uuid.uuid4()
OTHER = uuid.uuid4()
LINK_IDS = (ORG, INTERP, REPORT)


# ── building run directories ──────────────────────────────────────────────────────────────────────────────────────


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_run(
    tmp_path,
    *,
    status="draft",
    stored_hash="good",  # "good" | "bad" | "none"
    manifest="none",  # "none" | "valid" | "pdf_changed" | "pdf_missing" | "results_changed" | "garbage" | "hash_differs"
    audit=False,
    link=False,
    override_claim_ids=(),
    override_records=None,  # raw override dicts, used verbatim when given (malformed evidence)
    journal=False,
    manifest_report_id=None,
    manifest_approver=None,
    manifest_clinical=True,
    name="run",
):
    run = tmp_path / name
    run.mkdir()
    document = {
        "assembly": "GRCh38",
        "variants": [
            {
                "variant": {"chrom": "17", "pos": 43106534, "ref": "C", "alt": "A"},
                "overrides": (
                    list(override_records)
                    if override_records is not None
                    else [{"clinical": {"claim_id": cid}} for cid in override_claim_ids]
                ),
            }
        ],
        "caveats": [],
    }
    if status is not None:
        document["review_status"] = status
    if stored_hash != "none":
        good = compute_content_hash(document)
        document["content_hash"] = good if stored_hash == "good" else "0" * 64
    results_path = run / "geper_results.json"
    results_path.write_text(json.dumps(document), encoding="utf-8")

    full, short = b"%PDF full", b"%PDF short"
    (run / "geper_report_full.pdf").write_bytes(full)
    (run / "geper_report_short.pdf").write_bytes(short)
    if manifest != "none":
        m = {
            "pdf_sha256": _sha(full),
            "short_pdf_sha256": _sha(short),
            "results_json_sha256": _sha(results_path.read_bytes()),
            "content_hash": document.get("content_hash"),
        }
        if manifest_clinical:
            m["clinical"] = {
                "org_id": str(ORG),
                "interpretation_id": str(INTERP),
                "report_id": str(manifest_report_id or REPORT),
                "approver_user_id": str(manifest_approver or ME),
            }
        if manifest == "pdf_changed":
            (run / "geper_report_full.pdf").write_bytes(b"%PDF tampered")
        elif manifest == "pdf_missing":
            (run / "geper_report_short.pdf").unlink()
        elif manifest == "results_changed":
            m["results_json_sha256"] = "1" * 64
        elif manifest == "hash_differs":
            m["content_hash"] = "2" * 64
        elif manifest == "no_content_hash":
            m["content_hash"] = None
        text = "{not json" if manifest == "garbage" else json.dumps(m)
        (run / "geper_signoff_manifest.json").write_text(text, encoding="utf-8")
    if audit:
        (run / "geper_signoff_audit.log").write_text(json.dumps({"action": "approved"}) + "\n", encoding="utf-8")
    if link:
        (run / "clinical_link.json").write_text(
            json.dumps({"org_id": str(ORG), "interpretation_id": str(INTERP), "report_id": str(REPORT)}),
            encoding="utf-8",
        )
    if journal:
        (run / "geper_signoff_journal.jsonl").write_text("{}\n", encoding="utf-8")
    return run


def local_of(run):
    return st.inspect_local(str(run))


def claim(kind="sole_signatory", actor=ME, cid=None):
    return {"id": cid or uuid.uuid4(), "claim_type": kind, "actor_id": actor, "recorded_at": None}


def server(state="draft", *, retracted=False, approver=None, claims=(), interp=INTERP):
    return st.ServerFacts(
        state=state,
        interpretation_id=interp,
        is_retracted=retracted,
        approver_id=approver,
        has_content_hash=approver is not None,
        claims=list(claims),
    )


# ── the classification matrix ─────────────────────────────────────────────────────────────────────────────────────

SIGNED = dict(status="reviewed", manifest="valid", audit=True, link=True)

# name: (run kwargs, server kwargs or None for "report not found", (group, code, hold))
MATRIX = {
    "draft_no_claim": (dict(link=True), dict(), ("CONSISTENT", "NOT_STARTED", False)),
    "draft_with_my_claim": (dict(link=True), dict(claims=[claim()]), ("INCOMPLETE", "DRAFT_WITH_CLAIMS", False)),
    "draft_with_another_users_claim": (
        dict(link=True),
        dict(claims=[claim("accept", OTHER)]),
        ("INCOMPLETE", "DRAFT_WITH_CLAIMS", False),
    ),
    "draft_with_two_of_my_sole_signatory_claims": (
        dict(link=True),
        dict(claims=[claim(), claim()]),
        ("INVESTIGATE", "DUPLICATE_SOLE_SIGNATORY_CLAIMS", False),
    ),
    "under_review": (
        dict(link=True),
        dict(state="under_review", claims=[claim()]),
        ("INCOMPLETE", "UNDER_REVIEW", False),
    ),
    "approved_by_me_files_complete": (
        SIGNED,
        dict(state="approved", approver=ME),
        ("CONSISTENT", "SIGNED_OFF_BY_YOU", False),
    ),
    "approved_by_me_audit_line_missing": (
        dict(SIGNED, audit=False),
        dict(state="approved", approver=ME),
        ("INCOMPLETE", "SIGNED_OFF_AUDIT_ENTRY_MISSING", False),
    ),
    "approved_by_another_files_complete": (
        dict(SIGNED, manifest_approver=OTHER),
        dict(state="approved", approver=OTHER),
        ("CONSISTENT", "SIGNED_OFF_BY_ANOTHER_OPERATOR", False),
    ),
    "approved_by_me_reviewed_json_no_manifest": (
        dict(status="reviewed", link=True),
        dict(state="approved", approver=ME),
        ("INVESTIGATE", "APPROVED_FILES_INCOMPLETE_BY_YOU", True),
    ),
    "approved_by_another_reviewed_json_no_manifest": (
        dict(status="reviewed", link=True),
        dict(state="approved", approver=OTHER),
        ("INVESTIGATE", "APPROVED_FILES_INCOMPLETE_BY_ANOTHER_OPERATOR", True),
    ),
    "approved_by_me_manifest_pdf_changed": (
        dict(SIGNED, manifest="pdf_changed"),
        dict(state="approved", approver=ME),
        ("INVESTIGATE", "APPROVED_FILES_INCOMPLETE_BY_YOU", True),
    ),
    "approved_by_me_manifest_pdf_missing": (
        dict(SIGNED, manifest="pdf_missing"),
        dict(state="approved", approver=ME),
        ("INVESTIGATE", "APPROVED_FILES_INCOMPLETE_BY_YOU", True),
    ),
    "approved_by_me_manifest_garbage": (
        dict(SIGNED, manifest="garbage"),
        dict(state="approved", approver=ME),
        ("INVESTIGATE", "APPROVED_FILES_INCOMPLETE_BY_YOU", True),
    ),
    "approved_by_me_local_still_draft": (
        dict(link=True),
        dict(state="approved", approver=ME),
        ("INVESTIGATE", "APPROVED_NO_LOCAL_SIGNOFF_BY_YOU", False),
    ),
    "approved_by_another_local_still_draft": (
        dict(link=True),
        dict(state="approved", approver=OTHER),
        ("INVESTIGATE", "APPROVED_NO_LOCAL_SIGNOFF_BY_ANOTHER_OPERATOR", False),
    ),
    "released_files_complete": (
        SIGNED,
        dict(state="released", approver=ME),
        ("CONSISTENT", "RELEASED_CONSISTENT_BY_YOU", False),
    ),
    "released_files_incomplete": (
        dict(status="reviewed", link=True),
        dict(state="released", approver=ME),
        ("INVESTIGATE", "APPROVED_FILES_INCOMPLETE_BY_YOU", True),
    ),
    "retracted_draft": (dict(link=True), dict(retracted=True), ("INVESTIGATE", "RETRACTED", False)),
    "retracted_and_signed": (
        SIGNED,
        dict(state="approved", approver=ME, retracted=True),
        ("INVESTIGATE", "RETRACTED", False),
    ),
    "returned_state": (dict(link=True), dict(state="returned"), ("INVESTIGATE", "UNSUPPORTED_STATE_RETURNED", False)),
    "server_draft_local_reviewed_no_manifest": (
        dict(status="reviewed", link=True),
        dict(),
        ("INVESTIGATE", "LOCAL_REVIEWED_SERVER_NOT_APPROVED", True),
    ),
    "server_under_review_local_reviewed_with_manifest": (
        SIGNED,
        dict(state="under_review", claims=[claim()]),
        ("INVESTIGATE", "LOCAL_REVIEWED_SERVER_NOT_APPROVED", False),
    ),
    "manifest_present_but_json_not_reviewed": (
        dict(link=True, manifest="valid"),
        dict(),
        ("INVESTIGATE", "MANIFEST_WITHOUT_REVIEW", False),
    ),
    "server_names_a_different_interpretation": (
        dict(link=True),
        dict(interp=uuid.uuid4()),
        ("INVESTIGATE", "WRONG_TARGET", False),
    ),
    "manifest_names_a_different_report": (
        dict(SIGNED, manifest_report_id=uuid.uuid4()),
        dict(state="approved", approver=ME),
        ("INVESTIGATE", "WRONG_TARGET", False),
    ),
    "manifest_approver_differs_from_server": (
        dict(SIGNED, manifest_approver=OTHER),
        dict(state="approved", approver=ME),
        ("INVESTIGATE", "MANIFEST_APPROVER_DIFFERS", False),
    ),
    "reviewed_document_edited_after_hashing": (
        dict(SIGNED, stored_hash="bad"),
        dict(state="approved", approver=ME),
        ("INVESTIGATE", "LOCAL_HASH_INCONSISTENT", False),
    ),
    "override_record_names_a_claim_the_server_lacks": (
        dict(link=True, status="overridden", override_claim_ids=[str(uuid.uuid4())]),
        dict(),
        ("INVESTIGATE", "OVERRIDE_CLAIM_MISMATCH", False),
    ),
    "my_disagreement_claim_has_no_override_record": (
        dict(link=True, status="draft"),
        dict(claims=[claim("disagree")]),
        ("INVESTIGATE", "OVERRIDE_CLAIM_MISMATCH", False),
    ),
    "report_not_found_in_the_organisation": (
        dict(link=True),
        None,
        ("INVESTIGATE", "REPORT_NOT_FOUND_IN_ORGANISATION", False),
    ),
}


@pytest.mark.parametrize("name", sorted(MATRIX))
def test_classification(tmp_path, name):
    run_kwargs, server_kwargs, (group, code, hold) = MATRIX[name]
    run = build_run(tmp_path, **run_kwargs)
    facts = None if server_kwargs is None else server(**server_kwargs)
    result = st.classify(LINK_IDS, facts, local_of(run), ME)
    assert (result.group, result.code, result.hold) == (group, code, hold)
    assert result.exit_code == {"CONSISTENT": 0, "INCOMPLETE": 10, "INVESTIGATE": 20}[group]
    assert result.to_dict()["content_binding"] == "NOT VERIFIED"
    # deterministic: the same facts give the same answer
    again = st.classify(LINK_IDS, facts, local_of(run), ME)
    assert again.to_dict() == result.to_dict()


def test_an_override_whose_claim_exists_on_the_server_is_consistent(tmp_path):
    cid = uuid.uuid4()
    run = build_run(tmp_path, link=True, status="overridden", override_claim_ids=[str(cid)])
    result = st.classify(LINK_IDS, server(claims=[claim("disagree", ME, cid)]), local_of(run), ME)
    assert (result.group, result.code) == ("INCOMPLETE", "DRAFT_WITH_CLAIMS")


def test_a_disagreement_by_another_user_without_an_override_record_is_not_flagged(tmp_path):
    run = build_run(tmp_path, link=True)
    result = st.classify(LINK_IDS, server(claims=[claim("disagree", OTHER)]), local_of(run), ME)
    assert result.code == "DRAFT_WITH_CLAIMS"


def test_the_current_operator_is_identified_by_the_session_not_by_the_manifest(tmp_path):
    run = build_run(tmp_path, **SIGNED)
    as_me = st.classify(LINK_IDS, server("approved", approver=ME), local_of(run), ME)
    as_other = st.classify(LINK_IDS, server("approved", approver=ME), local_of(run), OTHER)
    assert as_me.code == "SIGNED_OFF_BY_YOU" and as_other.code == "SIGNED_OFF_BY_ANOTHER_OPERATOR"


# ── unlinked runs ─────────────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "kwargs, expected",
    [
        (dict(), ("CONSISTENT", "UNLINKED_DRAFT", False)),
        (
            dict(status="reviewed", manifest="valid", manifest_clinical=False),
            ("CONSISTENT", "UNLINKED_SIGNED_OFF", False),
        ),
        (dict(status="reviewed"), ("INVESTIGATE", "UNLINKED_REVIEWED_WITHOUT_VALID_MANIFEST", True)),
        (
            dict(status="reviewed", manifest="pdf_changed", manifest_clinical=False),
            ("INVESTIGATE", "UNLINKED_REVIEWED_WITHOUT_VALID_MANIFEST", True),
        ),
        (
            dict(status="reviewed", manifest="valid", stored_hash="bad", manifest_clinical=False),
            ("INVESTIGATE", "UNLINKED_LOCAL_HASH_INCONSISTENT", False),
        ),
        (dict(manifest="valid", manifest_clinical=False), ("INVESTIGATE", "UNLINKED_MANIFEST_WITHOUT_REVIEW", False)),
    ],
)
def test_unlinked_runs(tmp_path, kwargs, expected):
    result = st.classify_unlinked(local_of(build_run(tmp_path, **kwargs)))
    assert (result.group, result.code, result.hold) == expected


# ── the local inspection ──────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "manifest, problem",
    [
        ("pdf_changed", "full_pdf_differs_from_manifest"),
        ("pdf_missing", "short_pdf_missing"),
        ("results_changed", "results_json_changed_since_signoff"),
        ("hash_differs", "manifest_hash_differs_from_document"),
        ("garbage", "manifest_unparseable"),
    ],
)
def test_each_manifest_defect_is_named(tmp_path, manifest, problem):
    facts = local_of(build_run(tmp_path, status="reviewed", manifest=manifest))
    assert facts.manifest == "invalid" and problem in facts.manifest_problems
    assert facts.hold is True


def test_a_good_manifest_is_valid_and_not_a_hold(tmp_path):
    facts = local_of(build_run(tmp_path, status="reviewed", manifest="valid"))
    assert facts.manifest == "valid" and facts.manifest_problems == [] and facts.hold is False


def test_the_documents_own_hash_is_reported_as_self_consistency_only(tmp_path):
    assert local_of(build_run(tmp_path, stored_hash="good")).document_hash == "self_consistent"
    assert local_of(build_run(tmp_path, stored_hash="bad", name="b")).document_hash == "inconsistent"
    assert local_of(build_run(tmp_path, stored_hash="none", name="c")).document_hash == "absent"


def test_a_torn_audit_line_is_not_an_approved_entry(tmp_path):
    run = build_run(tmp_path, **SIGNED)
    (run / "geper_signoff_audit.log").write_text('{"action": "appro', encoding="utf-8")
    assert local_of(run).audit_has_approved is False


@pytest.mark.parametrize("what", ["no_directory", "no_results", "not_json", "not_an_object"])
def test_unreadable_local_artefacts_are_reported_not_guessed(tmp_path, what):
    run = tmp_path / "r"
    if what != "no_directory":
        run.mkdir()
    if what == "not_json":
        (run / "geper_results.json").write_text("{nope", encoding="utf-8")
    if what == "not_an_object":
        (run / "geper_results.json").write_text("[1, 2]", encoding="utf-8")
    result = st.run_status(str(run))
    assert (result.group, result.code, result.exit_code) == ("UNREADABLE", "LOCAL_UNREADABLE", 30)


def test_a_corrupt_link_file_does_not_demote_the_run_to_unlinked(tmp_path):
    run = build_run(tmp_path)
    (run / "clinical_link.json").write_text("{broken", encoding="utf-8")
    assert st.run_status(str(run)).code == "LINK_UNREADABLE"


def test_a_link_missing_ids_is_unreadable(tmp_path):
    run = build_run(tmp_path)
    (run / "clinical_link.json").write_text(json.dumps({"org_id": str(ORG)}), encoding="utf-8")
    assert st.run_status(str(run)).code == "LINK_INCOMPLETE"


def test_a_linked_run_without_credentials_is_unreadable_not_classified_from_files_alone(tmp_path):
    result = st.run_status(str(build_run(tmp_path, **SIGNED)))
    assert (result.group, result.code, result.exit_code) == ("UNREADABLE", "CREDENTIALS_NOT_SUPPLIED", 30)
    assert result.server is None and result.local is not None  # the local facts are still shown


def test_the_journal_is_only_noticed_never_read_or_created(tmp_path):
    with_journal = local_of(build_run(tmp_path, journal=True))
    without = local_of(build_run(tmp_path, name="other"))
    assert with_journal.journal_present is True and without.journal_present is False


# ── read-only, and no leakage ─────────────────────────────────────────────────────────────────────────────────────


def _snapshot(directory):
    out = {}
    for root, _dirs, files in os.walk(directory):
        for name in files:
            p = os.path.join(root, name)
            out[os.path.relpath(p, directory)] = (open(p, "rb").read(), os.stat(p).st_mtime_ns)
    return out


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(),
        dict(status="reviewed", manifest="valid", audit=True),
        dict(status="reviewed"),
        dict(status="reviewed", manifest="garbage"),
        dict(journal=True),
    ],
)
def test_status_writes_nothing_and_changes_nothing(tmp_path, kwargs, capsys):
    run = build_run(tmp_path, **kwargs)
    before = _snapshot(run)
    names_before = set(os.listdir(run))
    cli.main(["status", str(run)])
    assert _snapshot(run) == before
    assert set(os.listdir(run)) == names_before
    assert not any("journal" in n for n in os.listdir(run) if not kwargs.get("journal"))


def test_status_does_not_create_a_journal_even_for_a_stalled_linked_run(tmp_path, capsys):
    run = build_run(tmp_path, link=True)
    cli.main(["status", str(run)])  # no credentials: unreadable, and still nothing is written
    assert "geper_signoff_journal.jsonl" not in os.listdir(run)
    assert set(os.listdir(run)) == {n for n in os.listdir(run)}  # unchanged listing (no new file)


HEX64 = re.compile(r"[0-9a-f]{64}")


def test_output_never_contains_a_raw_hash_and_always_disclaims_binding(tmp_path, capsys):
    run = build_run(tmp_path, **SIGNED)
    result = st.classify(LINK_IDS, server("approved", approver=ME), local_of(run), ME)
    text = st.format_text(result)
    blob = json.dumps(result.to_dict())
    assert not HEX64.search(text) and not HEX64.search(blob)
    assert "Content binding: NOT VERIFIED" in text and result.to_dict()["content_binding"] == "NOT VERIFIED"
    for secret in (str(ME), str(OTHER), "password", "reason"):
        assert secret not in text


def test_output_does_not_claim_the_content_is_verified(tmp_path):
    run = build_run(tmp_path, **SIGNED)
    text = st.format_text(st.classify(LINK_IDS, server("approved", approver=ME), local_of(run), ME)).lower()
    assert "verified" not in text.replace("not verified", "")


def test_hold_is_stated_in_the_text_and_the_json(tmp_path):
    run = build_run(tmp_path, status="reviewed", link=True)
    result = st.classify(LINK_IDS, server("approved", approver=ME), local_of(run), ME)
    assert "[HOLD]" in st.format_text(result) and "HOLD" in st.format_text(result)
    assert result.to_dict()["hold"] is True


# ── the command ───────────────────────────────────────────────────────────────────────────────────────────────────


def test_exit_codes_are_distinct_and_documented():
    assert (st.EXIT_CONSISTENT, st.EXIT_INCOMPLETE, st.EXIT_INVESTIGATE, st.EXIT_UNREADABLE) == (0, 10, 20, 30)
    assert (
        len({0, 10, 20, 30, 1, 2}) == 6
    )  # none collides with the CLI's generic error (1) or argparse's usage error (2)
    doc = st.__doc__
    for code in ("0   CONSISTENT", "10   INCOMPLETE", "20   INVESTIGATE", "30   UNREADABLE"):
        assert code in doc


def test_cli_exit_codes(tmp_path, capsys):
    assert cli.main(["status", str(build_run(tmp_path))]) == 0
    assert cli.main(["status", str(build_run(tmp_path, status="reviewed", name="h"))]) == 20
    assert cli.main(["status", str(build_run(tmp_path, link=True, name="l"))]) == 30
    assert cli.main(["status", str(tmp_path / "missing")]) == 30
    out = capsys.readouterr().out
    assert (
        "UNREADABLE: CREDENTIALS_NOT_SUPPLIED" in out and "INVESTIGATE: UNLINKED_REVIEWED_WITHOUT_VALID_MANIFEST" in out
    )


def test_cli_json_output_is_parseable_and_deterministic(tmp_path, capsys):
    run = build_run(tmp_path, status="reviewed", manifest="valid", manifest_clinical=False)
    assert cli.main(["status", str(run), "--json"]) == 0
    first = capsys.readouterr().out
    assert cli.main(["status", str(run), "--json"]) == 0
    assert capsys.readouterr().out == first
    data = json.loads(first)
    assert data["code"] == "UNLINKED_SIGNED_OFF" and data["read_only"] is True and data["exit_code"] == 0


def test_cli_refuses_a_password_on_the_command_line(tmp_path):
    with pytest.raises(SystemExit) as caught:
        cli.main(["status", str(build_run(tmp_path)), "--clinical-password", "x"])
    assert caught.value.code == 2


def test_cli_with_an_email_but_no_password_variable_is_a_generic_error(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("GEPER_CLINICAL_PASSWORD", raising=False)
    assert cli.main(["status", str(build_run(tmp_path, link=True)), "--clinical-email", "a@b.test"]) == 1


def test_the_status_command_imports_no_write_path(tmp_path):
    """A structural guard: the module must not reference the write verbs of the sign-off tools or the HTTP adapter's writes."""
    source = open(st.__file__, encoding="utf-8").read()
    for forbidden in (
        "record_sole_signatory_claim",
        "record_disagreement_claim",
        "submit_for_review",
        "approve_report",
        "_write_document",
        "_append_audit_log",
        'open(path, "w"',
        ".write(",
    ):
        assert forbidden not in source, forbidden


# ── review of PR 1: a CONSISTENT outcome needs sufficient evidence, and never rests on an identity that is not established ──


@pytest.mark.parametrize(
    "identity", [None, "not-a-uuid", str(ME)]
)  # unknown, garbage, and the right id but as a string
def test_an_operator_identity_that_is_not_established_is_never_reported_as_you_or_another(tmp_path, identity):
    run = build_run(tmp_path, **SIGNED)
    result = st.classify(LINK_IDS, server("approved", approver=ME), local_of(run), identity)
    assert (result.group, result.code, result.exit_code) == ("UNREADABLE", "OPERATOR_IDENTITY_UNAVAILABLE", 30)
    assert "BY_YOU" not in result.code and "ANOTHER" not in result.code
    assert "approved by" not in result.summary.lower() and result.server is None  # no attribution, no server picture


def test_a_report_not_found_needs_no_identity(tmp_path):
    result = st.classify(LINK_IDS, None, local_of(build_run(tmp_path, link=True)), None)
    assert result.code == "REPORT_NOT_FOUND_IN_ORGANISATION"


def test_a_deleted_link_file_cannot_demote_a_clinically_signed_run_to_a_consistent_unlinked_one(tmp_path):
    run = build_run(
        tmp_path, status="reviewed", manifest="valid", audit=True, link=False
    )  # manifest HAS a clinical block
    result = st.run_status(str(run))
    assert (result.group, result.code) == ("INVESTIGATE", "UNLINKED_BUT_MANIFEST_NAMES_A_CLINICAL_RECORD")


def test_an_approved_report_whose_manifest_names_no_clinical_record_is_not_consistent(tmp_path):
    run = build_run(tmp_path, **SIGNED, manifest_clinical=False)
    result = st.classify(LINK_IDS, server("approved", approver=ME), local_of(run), ME)
    assert (result.group, result.code) == ("INVESTIGATE", "MANIFEST_NOT_LINKED_TO_THIS_REPORT")


@pytest.mark.parametrize("missing", ["report_id", "approver_user_id"])
def test_a_manifest_missing_the_report_or_approver_it_must_name_is_not_consistent(tmp_path, missing):
    run = build_run(tmp_path, **SIGNED)
    path = run / "geper_signoff_manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    del manifest["clinical"][missing]
    path.write_text(json.dumps(manifest), encoding="utf-8")
    result = st.classify(LINK_IDS, server("approved", approver=ME), local_of(run), ME)
    assert (result.group, result.code) == ("INVESTIGATE", "MANIFEST_NOT_LINKED_TO_THIS_REPORT")


def test_a_reviewed_linked_document_with_no_content_hash_is_not_consistent(tmp_path):
    run = build_run(tmp_path, **dict(SIGNED, stored_hash="none"))
    result = st.classify(LINK_IDS, server("approved", approver=ME), local_of(run), ME)
    assert (result.group, result.code) == ("INVESTIGATE", "LOCAL_HASH_ABSENT")


def test_a_manifest_that_lacks_the_documents_content_hash_is_invalid(tmp_path):
    facts = local_of(build_run(tmp_path, status="reviewed", manifest="no_content_hash"))
    assert facts.manifest == "invalid" and "manifest_lacks_content_hash" in facts.manifest_problems


def test_an_unlinked_reviewed_document_with_no_content_hash_is_not_consistent(tmp_path):
    run = build_run(tmp_path, status="reviewed", stored_hash="none", manifest="valid", manifest_clinical=False)
    assert st.classify_unlinked(local_of(run)).code == "UNLINKED_LOCAL_HASH_ABSENT"


@pytest.mark.parametrize("name", sorted(MATRIX))
def test_no_consistent_outcome_exists_without_the_evidence_it_names(tmp_path, name):
    """For every matrix case that is CONSISTENT: a server answer, an established operator, and (when a sign-off is
    claimed) a reviewed document with a self-consistent hash and a valid manifest naming this report."""
    run_kwargs, server_kwargs, (group, code, hold) = MATRIX[name]
    if group != "CONSISTENT":
        return
    local = local_of(build_run(tmp_path, **run_kwargs))
    assert server_kwargs is not None
    if code != "NOT_STARTED":
        assert local.reviewed and local.manifest == "valid" and local.document_hash == "self_consistent"
        assert local.manifest_clinical.get("report_id") == str(REPORT) and local.manifest_clinical.get(
            "approver_user_id"
        )
        assert server_kwargs["state"] in ("approved", "released")
    else:
        assert not local.reviewed and local.manifest == "absent" and server_kwargs.get("claims", []) == []


# ══ review of PR 1, round 2: B1 (override evidence), B2 (complete manifest identity), B3 (contradictory approval audit) ══


def _classify(tmp_path, run_kwargs, server_kwargs=None, name="r"):
    run = build_run(tmp_path, name=name, **run_kwargs)
    return st.classify(LINK_IDS, server(**(server_kwargs or {})), local_of(run), ME)


CID = uuid.uuid4()
BAD_OVERRIDE_RECORDS = {
    "no_clinical_pointer": [{"new_classification": "Pathogenic"}],
    "clinical_pointer_not_an_object": [{"clinical": "x"}],
    "claim_id_missing": [{"clinical": {}}],
    "claim_id_null": [{"clinical": {"claim_id": None}}],
    "claim_id_empty": [{"clinical": {"claim_id": ""}}],
    "claim_id_a_number": [{"clinical": {"claim_id": 12345}}],
    "claim_id_malformed": [{"clinical": {"claim_id": "not-a-uuid"}}],
    "claim_id_valid_but_unknown_to_the_server": [{"clinical": {"claim_id": str(uuid.uuid4())}}],
}


@pytest.mark.parametrize("case", sorted(BAD_OVERRIDE_RECORDS))
@pytest.mark.parametrize("state", ["draft", "under_review", "approved"])
def test_b1_an_override_record_without_a_matching_server_claim_is_never_consistent(tmp_path, case, state):
    if state == "approved":
        local_kwargs = dict(SIGNED, override_records=BAD_OVERRIDE_RECORDS[case])
        facts = server("approved", approver=ME)
    else:
        local_kwargs = dict(link=True, status="overridden", override_records=BAD_OVERRIDE_RECORDS[case])
        facts = server(state, claims=[claim()] if state == "under_review" else [])
    result = st.classify(LINK_IDS, facts, local_of(build_run(tmp_path, **local_kwargs)), ME)
    assert (result.group, result.code, result.exit_code) == ("INVESTIGATE", "OVERRIDE_CLAIM_MISMATCH", 20)


def test_b1_review_status_overridden_with_a_draft_server_and_no_claims_is_not_not_started(tmp_path):
    result = _classify(tmp_path, dict(link=True, status="overridden"))
    assert (result.group, result.code) == ("INVESTIGATE", "OVERRIDE_CLAIM_MISMATCH")


def test_b1_override_records_under_a_draft_status_are_a_contradiction(tmp_path):
    result = _classify(
        tmp_path,
        dict(link=True, status="draft", override_claim_ids=[str(CID)]),
        dict(claims=[claim("disagree", ME, CID)]),
    )
    assert (result.group, result.code) == ("INVESTIGATE", "OVERRIDE_CLAIM_MISMATCH")


def test_b1_a_valid_override_with_a_matching_server_claim_is_unchanged(tmp_path):
    in_progress = _classify(
        tmp_path,
        dict(link=True, status="overridden", override_claim_ids=[str(CID)]),
        dict(claims=[claim("disagree", ME, CID)]),
    )
    assert (in_progress.group, in_progress.code) == ("INCOMPLETE", "DRAFT_WITH_CLAIMS")
    signed = st.classify(
        LINK_IDS,
        server("approved", approver=ME, claims=[claim("sole_signatory"), claim("disagree", ME, CID)]),
        local_of(build_run(tmp_path, name="s", **dict(SIGNED, override_claim_ids=[str(CID)]))),
        ME,
    )
    assert (signed.group, signed.code) == ("CONSISTENT", "SIGNED_OFF_BY_YOU")


def test_b1_the_claim_id_may_be_written_in_upper_case(tmp_path):
    result = _classify(
        tmp_path,
        dict(link=True, status="overridden", override_claim_ids=[str(CID).upper()]),
        dict(claims=[claim("disagree", ME, CID)]),
    )
    assert result.code == "DRAFT_WITH_CLAIMS"


def test_b1_not_started_requires_every_one_of_its_conditions(tmp_path):
    normal = _classify(tmp_path, dict(link=True))
    assert (normal.group, normal.code) == ("CONSISTENT", "NOT_STARTED")
    contradictions = [
        dict(link=True, status="overridden"),
        dict(link=True, override_records=[{}]),
        dict(link=True, audit=True),
        dict(link=True, status="reviewed"),
        dict(link=True, manifest="valid"),
        dict(link=True, status="something-else"),
    ]
    for index, contradiction in enumerate(contradictions):
        result = _classify(tmp_path, contradiction, name=f"c{index}")
        assert result.code != "NOT_STARTED" and result.group != "CONSISTENT", contradiction


def test_b3_a_local_approval_audit_line_with_a_draft_no_claims_server_is_not_not_started(tmp_path):
    result = _classify(tmp_path, dict(link=True, audit=True))
    assert (result.group, result.code, result.exit_code) == (
        "INVESTIGATE",
        "LOCAL_AUDIT_RECORDS_AN_APPROVAL_THE_SERVER_LACKS",
        20,
    )


@pytest.mark.parametrize(
    "server_kwargs", [dict(), dict(claims=[claim()]), dict(state="under_review", claims=[claim()])]
)
def test_b3_the_contradiction_holds_whatever_the_unapproved_server_state(tmp_path, server_kwargs):
    result = _classify(tmp_path, dict(link=True, audit=True), server_kwargs)
    assert result.code == "LOCAL_AUDIT_RECORDS_AN_APPROVAL_THE_SERVER_LACKS"


def test_b3_a_normal_draft_and_a_normal_signed_run_keep_their_classification(tmp_path):
    assert _classify(tmp_path, dict(link=True), name="a").code == "NOT_STARTED"
    signed = st.classify(
        LINK_IDS, server("approved", approver=ME), local_of(build_run(tmp_path, name="b", **SIGNED)), ME
    )
    assert signed.code == "SIGNED_OFF_BY_YOU"


MANIFEST_IDENTITY_KEYS = ["org_id", "interpretation_id", "report_id", "approver_user_id"]


def _edit_manifest_clinical(run, edit):
    path = run / "geper_signoff_manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    edit(manifest["clinical"])
    path.write_text(json.dumps(manifest), encoding="utf-8")


@pytest.mark.parametrize("state", ["approved", "released"])
@pytest.mark.parametrize("missing", MANIFEST_IDENTITY_KEYS)
def test_b2_each_missing_manifest_identity_field_prevents_a_consistent_signoff(tmp_path, state, missing):
    run = build_run(tmp_path, **SIGNED)
    _edit_manifest_clinical(run, lambda c: c.pop(missing))
    result = st.classify(LINK_IDS, server(state, approver=ME), local_of(run), ME)
    assert (result.group, result.code) == ("INVESTIGATE", "MANIFEST_NOT_LINKED_TO_THIS_REPORT")


@pytest.mark.parametrize("key", ["org_id", "interpretation_id", "report_id"])
def test_b2_a_mismatched_manifest_identity_is_wrong_target(tmp_path, key):
    run = build_run(tmp_path, **SIGNED)
    _edit_manifest_clinical(run, lambda c: c.__setitem__(key, str(uuid.uuid4())))
    result = st.classify(LINK_IDS, server("approved", approver=ME), local_of(run), ME)
    assert (result.group, result.code) == ("INVESTIGATE", "WRONG_TARGET")


def test_b2_a_manifest_approver_that_differs_from_the_service_is_not_consistent(tmp_path):
    run = build_run(tmp_path, **SIGNED)
    _edit_manifest_clinical(run, lambda c: c.__setitem__("approver_user_id", str(uuid.uuid4())))
    result = st.classify(LINK_IDS, server("approved", approver=ME), local_of(run), ME)
    assert (result.group, result.code) == ("INVESTIGATE", "MANIFEST_APPROVER_DIFFERS")


@pytest.mark.parametrize("key", MANIFEST_IDENTITY_KEYS)
@pytest.mark.parametrize("bad", ["not-a-uuid", "", None, 12345, ["x"]])
def test_b2_a_malformed_manifest_identity_value_never_yields_a_consistent_signoff(tmp_path, key, bad):
    run = build_run(tmp_path, **SIGNED)
    _edit_manifest_clinical(run, lambda c: c.__setitem__(key, bad))
    result = st.classify(LINK_IDS, server("approved", approver=ME), local_of(run), ME)
    assert result.group == "INVESTIGATE"
    assert result.code in ("WRONG_TARGET", "MANIFEST_APPROVER_DIFFERS", "MANIFEST_NOT_LINKED_TO_THIS_REPORT")


@pytest.mark.parametrize(
    "state, expected", [("approved", "SIGNED_OFF_BY_YOU"), ("released", "RELEASED_CONSISTENT_BY_YOU")]
)
def test_b2_a_complete_correct_manifest_is_still_consistent(tmp_path, state, expected):
    run = build_run(tmp_path, **SIGNED)
    assert st.classify(LINK_IDS, server(state, approver=ME), local_of(run), ME).code == expected
    upper = build_run(tmp_path, name="u", **SIGNED)
    _edit_manifest_clinical(upper, lambda c: [c.__setitem__(k, c[k].upper()) for k in MANIFEST_IDENTITY_KEYS])
    # the same UUIDs spelled in upper case are still the same identity
    assert st.classify(LINK_IDS, server(state, approver=ME), local_of(upper), ME).code == expected


# ── the command's help states what authenticating does ──


def test_the_status_help_documents_the_authentication_side_effects(capsys):
    with pytest.raises(SystemExit) as caught:
        cli.main(["status", "--help"])
    assert caught.value.code == 0
    text = " ".join(capsys.readouterr().out.split()).lower()
    for phrase in (
        "existing clinical adapter",
        "login session",
        "audit",
        "lockout",
        "totp",
        "same time window",
        "not fully verified",
        "ends the session",
        "changes no clinical report",
    ):
        assert phrase in text, phrase
