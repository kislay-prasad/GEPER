"""
Crash-safe local publication for `review/signoff.py::approve`.

THE DEFECT. On a linked run `approve` records the claim, the submission and the approval on the clinical service, and
only THEN writes local files. It used to write the reviewed results JSON first, then the PDFs, then the manifest, each
straight onto its final path. An interruption in between left `review_status: "reviewed"` on disk with no manifest and
possibly no or truncated PDFs, and the export gate (`require_reviewed`) looks only at `review_status` and the stored
content hash, so it accepted that state (docs/SIGNOFF_RECOVERY_DESIGN.md, sections 1 and 13).

WHAT IS TESTED. Everything here is DB-free: unlinked runs in temporary directories with the real renderers and real
files, plus a linked run whose four clinical helpers are replaced by stand-ins (no clinical service, no database, no
network). Failures are injected deterministically at the module's own seams (`_publish_replace`, `generate_pdf`,
`generate_short_pdf`, `_append_audit_log`, `_remove_staging_dir`, `_fsync_directory`).

  * Injected in-process failures: REAL FILESYSTEM operations on the host running the tests, with a failure raised at a
    chosen point. `SimulatedCrash` is a BaseException that also bypasses cleanup, standing in for a killed process.
  * Subprocess kill tests: a real child process that calls `os._exit` at a chosen point (no cleanup of any kind runs).
  * `_fsync_directory`'s POSIX branch is exercised with MOCKED `os.open` / `os.fsync`: this test host's own behaviour is
    whatever `os.name` says, so the POSIX durability claim itself is NOT demonstrated by running on Windows.

WHAT IS NOT CLAIMED. Atomicity of the file SET (each `os.replace` is atomic for one file only), power-loss durability
on Windows, or behaviour on network / bind-mounted filesystems. The tests prove the ORDER and the fail-closed property
for process interruption, and that status paths do not report an incomplete publication as signed.
"""

import hashlib
import json
import os
import subprocess
import sys
import textwrap
import uuid

import pytest

from review import signoff as s
from review import signoff_status as st
from tests.test_signoff import _make_document, _write_run
from utils.exceptions import SignoffError

IDENTITY = ("Dr. Rajesh Sharma", "MCI-12345", "AIIMS Delhi")

PUBLISH_ORDER = [
    s.PATIENT_META_FILENAME,
    s.FULL_PDF_FILENAME,
    s.SHORT_PDF_FILENAME,
    s.MANIFEST_FILENAME,
    s.RESULTS_FILENAME,
]
GEPER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class SimulatedCrash(BaseException):
    """A process that dies mid-publication. A BaseException so no `except Exception` handler can absorb it."""


def sha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def snapshot(run):
    """name -> (sha256) for every regular file directly in the run directory, plus the set of sub-directories."""
    files = {n: sha(os.path.join(run, n)) for n in sorted(os.listdir(run)) if os.path.isfile(os.path.join(run, n))}
    dirs = sorted(n for n in os.listdir(run) if os.path.isdir(os.path.join(run, n)))
    return files, dirs


def staging_dirs(run):
    return sorted(n for n in os.listdir(run) if n.startswith(s._STAGING_PREFIX))


@pytest.fixture
def run(tmp_path):
    return _write_run(str(tmp_path))


@pytest.fixture
def no_cleanup(monkeypatch):
    """A killed process runs no `finally`: make the staging directory's own cleanup a no-op."""
    monkeypatch.setattr(s, "_remove_staging_dir", lambda path: None)


def approve(run):
    return s.approve(run, *IDENTITY)


def disk_document(run):
    with open(os.path.join(run, s.RESULTS_FILENAME), encoding="utf-8") as fh:
        return json.load(fh)


def gate_accepts(run):
    """The export gate as an automated consumer would run it, given the run directory."""
    try:
        s.require_reviewed(disk_document(run), consumer="test", output_dir=run)
        return True
    except SignoffError:
        return False


def pending_status(run):
    (row,) = s.list_pending(os.path.dirname(run), show_all=True)
    return row["status"]


# ═══ successful publication ═══


def test_a_successful_unlinked_approval_publishes_every_artifact_with_a_valid_manifest(run):
    manifest = approve(run)
    for name in PUBLISH_ORDER:
        assert os.path.isfile(os.path.join(run, name)), name
    with open(os.path.join(run, s.MANIFEST_FILENAME), encoding="utf-8") as fh:
        on_disk = json.load(fh)
    assert on_disk == manifest
    assert manifest["pdf_sha256"] == sha(os.path.join(run, s.FULL_PDF_FILENAME))
    assert manifest["short_pdf_sha256"] == sha(os.path.join(run, s.SHORT_PDF_FILENAME))
    assert manifest["results_json_sha256"] == sha(os.path.join(run, s.RESULTS_FILENAME))
    assert manifest["content_hash"] == disk_document(run)["content_hash"]
    assert disk_document(run)["review_status"] == "reviewed"
    assert s._publication_problems(run, disk_document(run)) == []
    assert staging_dirs(run) == []  # the staging directory this call created is gone
    assert gate_accepts(run) and pending_status(run) == "REVIEWED"
    assert st.inspect_local(run).audit_has_approved is True  # the audit line was appended after publication


def test_the_manifest_keys_are_the_ones_the_earlier_writer_produced(run):
    assert set(approve(run)) == {
        "pdf_sha256",
        "short_pdf_sha256",
        "results_json_sha256",
        "content_hash",
        "clinician_name",
        "reg_number",
        "hospital",
        "identity_source",
        "clinician_user_id",
        "approved_at",
        "variants",
    }


def test_no_staged_file_is_named_like_a_report_run_while_rendering(run, monkeypatch):
    seen = {}
    real = s.generate_pdf

    def spy(document, output_path, **kwargs):
        seen["staging"] = sorted(os.listdir(os.path.dirname(output_path)))
        seen["path"] = output_path
        return real(document, output_path, **kwargs)

    monkeypatch.setattr(s, "generate_pdf", spy)
    approve(run)
    assert os.path.dirname(seen["path"]) != run  # rendered into the private staging directory
    assert os.path.basename(os.path.dirname(seen["path"])).startswith(s._STAGING_PREFIX)
    assert s.RESULTS_FILENAME not in seen["staging"]


# ═══ order ═══


def test_the_publication_order_puts_the_reviewed_json_last_and_the_audit_line_after_it(run, monkeypatch):
    real_replace, events = s._publish_replace, []

    def spy(src, dst):
        name = os.path.basename(dst)
        # at the instant a file is published, everything it depends on is already in its final place and correct
        if name == s.RESULTS_FILENAME:
            for earlier in PUBLISH_ORDER[:-1]:
                assert os.path.isfile(os.path.join(run, earlier)), earlier
            assert disk_document(run)["review_status"] != "reviewed"  # the JSON on disk is still the draft
        if name == s.MANIFEST_FILENAME:
            with open(src, encoding="utf-8") as fh:
                manifest = json.load(fh)
            assert manifest["pdf_sha256"] == sha(os.path.join(run, s.FULL_PDF_FILENAME))
            assert manifest["short_pdf_sha256"] == sha(os.path.join(run, s.SHORT_PDF_FILENAME))
        events.append(("replace", name))
        real_replace(src, dst)

    real_audit = s._append_audit_log

    def audit(output_dir, entry, **kwargs):
        events.append(("audit", entry["action"]))
        assert disk_document(run)["review_status"] == "reviewed"  # only after publication succeeded
        return real_audit(output_dir, entry, **kwargs)

    monkeypatch.setattr(s, "_publish_replace", spy)
    monkeypatch.setattr(s, "_append_audit_log", audit)
    approve(run)
    assert events == [("replace", n) for n in PUBLISH_ORDER] + [("audit", "approved")]


def test_a_reviewed_json_is_never_on_disk_before_its_pdfs_and_manifest(run, monkeypatch):
    real_replace = s._publish_replace

    def spy(src, dst):
        if os.path.basename(dst) != s.RESULTS_FILENAME:
            assert disk_document(run).get("review_status") != "reviewed"
        real_replace(src, dst)

    monkeypatch.setattr(s, "_publish_replace", spy)
    approve(run)


# ═══ failure before publication leaves the destination unchanged ═══


@pytest.mark.parametrize("renderer", ["generate_pdf", "generate_short_pdf"])
def test_a_render_failure_leaves_every_existing_destination_file_unchanged(run, monkeypatch, renderer):
    with open(os.path.join(run, s.PATIENT_META_FILENAME), "w", encoding="utf-8") as fh:
        json.dump({"patient_name": "Existing Patient", "note": "keep"}, fh)
    before = snapshot(run)
    boom = RuntimeError("renderer exploded")

    def fail(*args, **kwargs):
        raise boom

    monkeypatch.setattr(s, renderer, fail)
    with pytest.raises(s.PublicationError) as caught:
        approve(run)
    assert caught.value.__cause__ is boom
    assert caught.value.published == []
    assert "local publication failed" in str(caught.value).lower()
    assert snapshot(run) == before  # byte-identical, and the staging directory is gone too
    assert disk_document(run)["review_status"] != "reviewed"


def test_a_crash_during_rendering_leaves_the_destination_unchanged_and_only_a_staging_directory(
    run, monkeypatch, no_cleanup
):
    before_files, _ = snapshot(run)

    def crash(document, output_path, **kwargs):
        with open(output_path, "wb") as fh:
            fh.write(b"%PDF-1.4 truncated")  # a half-written staged PDF
        raise SimulatedCrash()

    monkeypatch.setattr(s, "generate_pdf", crash)
    with pytest.raises(SimulatedCrash):
        approve(run)
    after_files, _ = snapshot(run)
    assert after_files == before_files  # not a single destination file changed
    assert len(staging_dirs(run)) == 1
    assert not gate_accepts(run) and pending_status(run) == "DRAFT"


def test_a_staging_failure_before_any_render_leaves_the_destination_unchanged(run, monkeypatch):
    before = snapshot(run)

    def no_space(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(s.tempfile, "mkdtemp", no_space)
    with pytest.raises(s.PublicationError) as caught:
        approve(run)
    assert isinstance(caught.value.__cause__, OSError)
    assert snapshot(run) == before


# ═══ failure at each replacement fails closed ═══


@pytest.mark.parametrize("fail_at", range(len(PUBLISH_ORDER)), ids=[f"before_{n}" for n in PUBLISH_ORDER])
def test_a_failed_replacement_leaves_a_detectable_incomplete_state_that_fails_closed(run, monkeypatch, fail_at):
    real_replace, calls = s._publish_replace, []

    def flaky(src, dst):
        if len(calls) == fail_at:
            raise PermissionError(13, "destination is open elsewhere")
        calls.append(os.path.basename(dst))
        real_replace(src, dst)

    monkeypatch.setattr(s, "_publish_replace", flaky)
    with pytest.raises(s.PublicationError) as caught:
        approve(run)
    error = caught.value
    assert isinstance(error.__cause__, PermissionError)
    assert error.published == PUBLISH_ORDER[:fail_at]
    assert "local publication failed" in str(error).lower()
    if fail_at:
        assert "not rolled back" in str(error).lower()
    # fail closed: nothing says "reviewed" until the LAST replacement, and no status path reports a signed report
    assert disk_document(run)["review_status"] != "reviewed"
    assert not gate_accepts(run)
    assert pending_status(run) == "DRAFT"
    assert staging_dirs(run) == []  # this call's staging directory was cleaned even though publication failed
    assert not os.path.exists(os.path.join(run, s.AUDIT_LOG_FILENAME))  # no audit line for an unpublished sign-off


@pytest.mark.parametrize("crash_at", range(len(PUBLISH_ORDER)), ids=[f"before_{n}" for n in PUBLISH_ORDER])
def test_a_crash_before_each_replacement_fails_closed_and_leaves_only_the_staging_directory(
    run, monkeypatch, no_cleanup, crash_at
):
    real_replace, calls = s._publish_replace, []

    def dying(src, dst):
        if len(calls) == crash_at:
            raise SimulatedCrash()
        calls.append(os.path.basename(dst))
        real_replace(src, dst)

    monkeypatch.setattr(s, "_publish_replace", dying)
    with pytest.raises(SimulatedCrash):
        approve(run)
    assert calls == PUBLISH_ORDER[:crash_at]
    assert disk_document(run)["review_status"] != "reviewed"
    assert not gate_accepts(run) and pending_status(run) == "DRAFT"
    assert len(staging_dirs(run)) == 1  # left for a person; never auto-deleted
    # the PR1 classifier sees an approved server with no reviewed local JSON as a state to investigate, never as signed
    facts = st.inspect_local(run)
    assert facts.reviewed is False


def test_a_crash_immediately_after_the_reviewed_json_is_published_is_complete_but_for_the_audit_line(
    run, monkeypatch, no_cleanup
):
    real_audit = s._append_audit_log

    def dying(output_dir, entry, **kwargs):
        raise SimulatedCrash()

    monkeypatch.setattr(s, "_append_audit_log", dying)
    with pytest.raises(SimulatedCrash):
        approve(run)
    monkeypatch.setattr(s, "_append_audit_log", real_audit)  # the "restarted process": the gate may audit again
    assert s._publication_problems(run, disk_document(run)) == []
    assert gate_accepts(run) and pending_status(run) == "REVIEWED"
    facts = st.inspect_local(run)
    assert facts.manifest == "valid" and facts.audit_has_approved is False  # "SIGNED_OFF_AUDIT_ENTRY_MISSING" state


# ═══ audit failure ═══


def test_an_audit_failure_is_reported_honestly_and_nothing_is_rolled_back(run, monkeypatch):

    def failing(output_dir, entry, **kwargs):
        raise OSError(5, "audit volume went away")

    monkeypatch.setattr(s, "_append_audit_log", failing)
    with pytest.raises(s.PublicationError) as caught:
        approve(run)
    error = caught.value
    assert isinstance(error.__cause__, OSError)
    assert error.published == PUBLISH_ORDER  # every file WAS published
    message = str(error).lower()
    assert "audit" in message and "published" in message and "not rolled back" in message
    assert s._publication_problems(run, disk_document(run)) == []  # the published set is intact and verifies
    assert disk_document(run)["review_status"] == "reviewed"
    assert st.inspect_local(run).audit_has_approved is False


# ═══ staging ownership and cleanup ═══


def test_cleanup_removes_only_the_directory_this_invocation_created(run):
    previous = os.path.join(run, f"{s._STAGING_PREFIX}from-an-earlier-crash")
    os.makedirs(previous)
    with open(os.path.join(previous, "full.pdf.staged"), "wb") as fh:
        fh.write(b"half")
    bystander = os.path.join(run, "notes.txt")
    with open(bystander, "w", encoding="utf-8") as fh:
        fh.write("not ours")
    approve(run)
    assert staging_dirs(run) == [os.path.basename(previous)]
    assert open(os.path.join(previous, "full.pdf.staged"), "rb").read() == b"half"  # untouched
    assert open(bystander, encoding="utf-8").read() == "not ours"


def test_staging_directories_left_by_earlier_crashes_are_never_deleted_automatically(run, monkeypatch, no_cleanup):
    real_replace = s._publish_replace

    def dying(src, dst):
        raise SimulatedCrash()

    monkeypatch.setattr(s, "_publish_replace", dying)
    with pytest.raises(SimulatedCrash):
        approve(run)
    (left,) = staging_dirs(run)
    contents = sorted(os.listdir(os.path.join(run, left)))
    monkeypatch.setattr(s, "_publish_replace", real_replace)
    monkeypatch.undo()  # also restores the real cleanup
    approve(run)  # a later, successful approval of the same run
    assert left in staging_dirs(run)
    assert sorted(os.listdir(os.path.join(run, left))) == contents


def test_a_leftover_staging_directory_is_not_mistaken_for_a_second_run(run, monkeypatch, no_cleanup):
    def dying(src, dst):
        raise SimulatedCrash()

    monkeypatch.setattr(s, "_publish_replace", dying)
    with pytest.raises(SimulatedCrash):
        approve(run)
    (left,) = staging_dirs(run)
    staged = os.listdir(os.path.join(run, left))
    assert s.RESULTS_FILENAME not in staged and s.MANIFEST_FILENAME not in staged
    rows = s.list_pending(os.path.dirname(run), show_all=True)
    assert [r["output_dir"] for r in rows] == [run]


def test_a_cleanup_failure_is_reported_as_a_warning_and_never_masks_the_result(run, monkeypatch):
    warnings = []

    def refuse(path):
        raise OSError(16, "device busy")

    monkeypatch.setattr(s.shutil, "rmtree", refuse)
    monkeypatch.setattr(s.logger, "warning", warnings.append)
    manifest = approve(run)  # publication itself succeeded
    assert manifest["pdf_sha256"] == sha(os.path.join(run, s.FULL_PDF_FILENAME))
    assert len(staging_dirs(run)) == 1  # left behind, reported, not hidden
    assert any("staging" in str(message).lower() for message in warnings)
    assert gate_accepts(run)


def test_a_cleanup_failure_after_a_publication_failure_does_not_replace_the_original_error(run, monkeypatch):
    def flaky(src, dst):
        raise PermissionError(13, "denied")

    def refuse(path):
        raise OSError(16, "device busy")

    monkeypatch.setattr(s, "_publish_replace", flaky)
    monkeypatch.setattr(s.shutil, "rmtree", refuse)
    with pytest.raises(s.PublicationError) as caught:
        approve(run)
    assert isinstance(caught.value.__cause__, PermissionError)


def test_the_staging_directory_is_inside_the_destination_directory_so_replacement_stays_on_one_filesystem(
    run, monkeypatch
):
    seen = {}
    real = s._publish_replace

    def spy(src, dst):
        seen.setdefault("pairs", []).append((os.path.dirname(src), os.path.dirname(dst)))
        real(src, dst)

    monkeypatch.setattr(s, "_publish_replace", spy)
    approve(run)
    for src_dir, dst_dir in seen["pairs"]:
        assert os.path.dirname(src_dir) == dst_dir == run
        assert os.path.basename(src_dir).startswith(s._STAGING_PREFIX)


# ═══ durability steps ═══


def test_staged_files_and_the_audit_line_are_fsynced_and_the_directory_is_synced_around_the_json(run, monkeypatch):
    synced, directory = [], []
    real_fsync = os.fsync
    monkeypatch.setattr(s.os, "fsync", lambda fd: (synced.append(fd), real_fsync(fd))[1])
    real_dir = s._fsync_directory
    monkeypatch.setattr(s, "_fsync_directory", lambda path: (directory.append(path), real_dir(path))[1])
    approve(run)
    assert len(synced) >= 5 + 1  # five staged files and the audit line (the directory sync adds more on POSIX)
    assert directory == [run, run]  # once before the JSON is published (so it cannot overtake the manifest), once after


def test_directory_fsync_is_a_documented_no_op_off_posix_and_a_real_fsync_on_posix(tmp_path, monkeypatch):
    """MOCKED: this host's own platform decides the real branch; both branches are exercised here with stubs, so the
    POSIX durability behaviour itself is NOT demonstrated by running on a non-POSIX host."""
    calls = []
    monkeypatch.setattr(s, "_open_directory", lambda path: calls.append(("open", path)) or 99)
    monkeypatch.setattr(s.os, "fsync", lambda fd: calls.append(("fsync", fd)))
    monkeypatch.setattr(s.os, "close", lambda fd: calls.append(("close", fd)))
    monkeypatch.setattr(s, "_IS_POSIX", False)
    s._fsync_directory(str(tmp_path))
    assert calls == []  # nothing attempted on a platform that cannot sync a directory
    monkeypatch.setattr(s, "_IS_POSIX", True)
    s._fsync_directory(str(tmp_path))
    assert calls == [("open", str(tmp_path)), ("fsync", 99), ("close", 99)]


def test_a_directory_sync_that_the_filesystem_refuses_does_not_fail_a_sign_off(run, monkeypatch):
    warnings = []
    monkeypatch.setattr(s, "_IS_POSIX", True)
    monkeypatch.setattr(s, "_open_directory", lambda path: (_ for _ in ()).throw(OSError(22, "unsupported")))
    monkeypatch.setattr(s.logger, "warning", warnings.append)
    approve(run)  # the platform said no: documented best effort, not an error
    assert gate_accepts(run)
    assert any("fsync directory" in str(message) for message in warnings)


# ═══ list_pending and require_reviewed verify completeness ═══


def _corrupt_manifest(run, mutate):
    path = os.path.join(run, s.MANIFEST_FILENAME)
    manifest = json.load(open(path, encoding="utf-8"))
    mutate(manifest)
    json.dump(manifest, open(path, "w", encoding="utf-8"))


def _truncate(run, name):
    path = os.path.join(run, name)
    data = open(path, "rb").read()
    open(path, "wb").write(data[: len(data) // 2])


def _delete(run, name):
    os.remove(os.path.join(run, name))


DAMAGE = {
    "manifest_missing": lambda run: _delete(run, s.MANIFEST_FILENAME),
    "manifest_malformed": lambda run: open(os.path.join(run, s.MANIFEST_FILENAME), "w").write("{not json"),
    "manifest_not_an_object": lambda run: open(os.path.join(run, s.MANIFEST_FILENAME), "w").write("[1, 2]"),
    "manifest_empty_object": lambda run: open(os.path.join(run, s.MANIFEST_FILENAME), "w").write("{}"),
    "manifest_lacks_pdf_hash": lambda run: _corrupt_manifest(run, lambda m: m.pop("pdf_sha256")),
    "manifest_lacks_short_pdf_hash": lambda run: _corrupt_manifest(run, lambda m: m.pop("short_pdf_sha256")),
    "manifest_lacks_results_hash": lambda run: _corrupt_manifest(run, lambda m: m.pop("results_json_sha256")),
    "manifest_pdf_hash_malformed": lambda run: _corrupt_manifest(run, lambda m: m.update(pdf_sha256="abc")),
    "manifest_results_hash_wrong": lambda run: _corrupt_manifest(run, lambda m: m.update(results_json_sha256="0" * 64)),
    "manifest_content_hash_differs": lambda run: _corrupt_manifest(run, lambda m: m.update(content_hash="f" * 64)),
    "full_pdf_missing": lambda run: _delete(run, s.FULL_PDF_FILENAME),
    "short_pdf_missing": lambda run: _delete(run, s.SHORT_PDF_FILENAME),
    "full_pdf_truncated": lambda run: _truncate(run, s.FULL_PDF_FILENAME),
    "short_pdf_truncated": lambda run: _truncate(run, s.SHORT_PDF_FILENAME),
    "full_pdf_replaced": lambda run: open(os.path.join(run, s.FULL_PDF_FILENAME), "wb").write(b"%PDF-other"),
    "results_json_edited": lambda run: _edit_results(run),
}


def _edit_results(run):
    document = disk_document(run)
    document["variant_count"] = document.get("variant_count", 0) + 1
    json.dump(document, open(os.path.join(run, s.RESULTS_FILENAME), "w", encoding="utf-8"), indent=2)


@pytest.mark.parametrize("damage", sorted(DAMAGE))
def test_list_pending_does_not_report_reviewed_for_an_incomplete_or_inconsistent_publication(run, damage):
    approve(run)
    assert pending_status(run) == "REVIEWED"  # control: intact
    DAMAGE[damage](run)
    assert pending_status(run) == "DRAFT"


@pytest.mark.parametrize("damage", sorted(DAMAGE))
def test_require_reviewed_rejects_an_incomplete_or_inconsistent_publication(run, damage):
    approve(run)
    assert gate_accepts(run)  # control: intact
    DAMAGE[damage](run)
    # `results_json_edited` is also caught by the content hash; the rest are caught only by the publication check
    assert not gate_accepts(run)


def test_require_reviewed_rejects_a_reviewed_json_with_no_manifest_when_given_the_directory(run):
    """The state the old order could leave behind after an interruption."""
    approve(run)
    os.remove(os.path.join(run, s.MANIFEST_FILENAME))
    with pytest.raises(SignoffError, match="incomplete or inconsistent"):
        s.require_reviewed(disk_document(run), consumer="LIMS export", output_dir=run)


def test_a_rejected_publication_writes_no_verification_audit_entry(run):
    approve(run)
    before = open(os.path.join(run, s.AUDIT_LOG_FILENAME), encoding="utf-8").read()
    _delete(run, s.SHORT_PDF_FILENAME)
    assert not gate_accepts(run)
    assert open(os.path.join(run, s.AUDIT_LOG_FILENAME), encoding="utf-8").read() == before


def test_without_a_directory_require_reviewed_keeps_checking_only_the_document(run):
    """Compatibility: a caller that cannot name a directory (every current caller) gets the earlier behaviour. Such a
    caller cannot see the manifest or the PDFs, so completeness cannot be checked for it."""
    approve(run)
    os.remove(os.path.join(run, s.MANIFEST_FILENAME))
    s.require_reviewed(disk_document(run))  # unchanged: status and content hash only
    s.require_reviewed({"review_status": "reviewed"})


def test_list_pending_entries_name_why_a_manifest_that_exists_does_not_verify(run):
    approve(run)
    _delete(run, s.FULL_PDF_FILENAME)
    (row,) = s.list_pending(os.path.dirname(run), show_all=True)
    assert row["status"] == "DRAFT"
    assert row["manifest_problems"] == [f"{s.FULL_PDF_FILENAME}_missing"]


def test_an_unsigned_run_has_no_manifest_problems(run):
    (row,) = s.list_pending(os.path.dirname(run))
    assert row["status"] == "DRAFT" and row["manifest_problems"] == []


def test_withdraw_and_override_still_work_on_a_verified_publication(run):
    approve(run)
    s.withdraw(run, "signed in error", "actor@example.test")
    assert pending_status(run) == "DRAFT"


# ═══ linked run with stand-in clinical access (no database, no network) ═══


@pytest.fixture
def linked(run, monkeypatch):
    org, interpretation, report = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with open(os.path.join(run, s.CLINICAL_LINK_FILENAME), "w", encoding="utf-8") as fh:
        json.dump({"org_id": str(org), "interpretation_id": str(interpretation), "report_id": str(report)}, fh)
    record = {
        "org_id": str(org),
        "interpretation_id": str(interpretation),
        "report_id": str(report),
        "claim_id": str(uuid.uuid4()),
        "claim_type": "sole_signatory",
        "approver_user_id": str(uuid.uuid4()),
    }
    calls = []

    def record_signoff(dao, session, link, reason):
        calls.append(("signoff", snapshot(run)))  # the directory as it is when the server is told
        return dict(record)

    monkeypatch.setattr(s, "_open_clinical", lambda *a, **k: (object(), object(), lambda: calls.append(("closed",))))
    monkeypatch.setattr(
        s,
        "_linked_signing_identity",
        lambda dao, session, identity: s.ClinicianIdentity(
            full_name=IDENTITY[0],
            registration_number=IDENTITY[1],
            hospital=IDENTITY[2],
            user_id=record["approver_user_id"],
        ),
    )
    monkeypatch.setattr(s, "_linked_revision_status", lambda dao, session, link: None)
    monkeypatch.setattr(s, "_record_clinical_signoff", record_signoff)
    return run, record, calls


def _approve_linked(run):
    return s.approve(run, clinical_credentials=s.ClinicalCredentials("a@example.test", "pw"), clinical_reason="concur")


def test_a_linked_approval_publishes_after_the_clinical_writes_and_records_the_clinical_block(linked):
    run, record, calls = linked
    before = snapshot(run)
    manifest = _approve_linked(run)
    assert calls[0] == ("signoff", before)  # the server was told while the directory was still untouched
    assert ("closed",) in calls
    assert manifest["clinical"] == record
    assert s._publication_problems(run, disk_document(run)) == []
    assert gate_accepts(run) and pending_status(run) == "REVIEWED"


def test_a_linked_render_failure_says_server_approval_may_have_succeeded_and_changes_no_local_file(linked, monkeypatch):
    run, _, calls = linked
    before = snapshot(run)

    def fail(*args, **kwargs):
        raise RuntimeError("render failed")

    monkeypatch.setattr(s, "generate_short_pdf", fail)
    with pytest.raises(s.PublicationError) as caught:
        _approve_linked(run)
    assert any(c[0] == "signoff" for c in calls)  # the clinical write did happen first
    assert "clinical record" in str(caught.value).lower()
    assert "server approval may already have succeeded" in str(caught.value).lower()
    assert snapshot(run) == before
    assert not gate_accepts(run)  # the old order would have left a reviewed JSON the gate accepted


def test_an_unlinked_failure_does_not_claim_a_server_approval(run, monkeypatch):
    monkeypatch.setattr(s, "generate_pdf", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    with pytest.raises(s.PublicationError) as caught:
        approve(run)
    assert "server approval" not in str(caught.value).lower()
    assert "no clinical record" in str(caught.value).lower()


# ═══ real process kills (subprocess; temporary directory only; no clinical data) ═══

CHILD = textwrap.dedent(
    """
    import os, sys
    sys.path.insert(0, {geper!r})
    from review import signoff as s
    run, mode, at = sys.argv[1], sys.argv[2], int(sys.argv[3])
    count = [0]
    real = s._publish_replace
    def replace(src, dst):
        if mode == "before" and count[0] == at:
            os._exit(77)
        real(src, dst)
        count[0] += 1
        if mode == "after" and count[0] == at + 1:
            os._exit(77)
    s._publish_replace = replace
    if mode == "render":
        def die(*a, **k):
            os._exit(77)
        s.generate_short_pdf = die
    s.approve(run, "Dr. Rajesh Sharma", "MCI-12345", "AIIMS Delhi")
    """
)


def _kill_child(run, mode, at):
    result = subprocess.run(
        [sys.executable, "-c", CHILD.format(geper=GEPER_DIR), run, mode, str(at)],
        capture_output=True,
        text=True,
        timeout=240,
        cwd=GEPER_DIR,
    )
    assert result.returncode == 77, result.stderr[-2000:]


@pytest.mark.parametrize(
    "mode, at, signed",
    [
        ("render", 0, False),  # killed while the second PDF is being rendered
        ("before", 3, False),  # killed with the PDFs published, before the manifest
        ("before", 4, False),  # killed with the manifest published, before the reviewed JSON
        ("after", 4, True),  # killed with the reviewed JSON published, before the audit line
    ],
    ids=["during_render", "before_manifest", "before_json", "after_json_before_audit"],
)
def test_a_real_process_kill_leaves_a_state_the_status_paths_classify_correctly(run, mode, at, signed):
    _kill_child(run, mode, at)
    assert gate_accepts(run) is signed
    assert pending_status(run) == ("REVIEWED" if signed else "DRAFT")
    assert len(staging_dirs(run)) == 1  # the killed process cleaned nothing: it is left for a person
    rows = s.list_pending(os.path.dirname(run), show_all=True)
    assert [r["output_dir"] for r in rows] == [run]
    if signed:
        assert st.inspect_local(run).audit_has_approved is False  # complete but for the audit line
    else:
        assert disk_document(run)["review_status"] != "reviewed"


def test_the_fixture_document_is_a_draft_before_any_approval():
    assert _make_document().get("review_status") in (None, "draft")
