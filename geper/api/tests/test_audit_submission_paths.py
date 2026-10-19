"""
api/tests/test_audit_submission_paths.py
────────────────────────────────────────
No clinical database. scripts/audit_submission_paths.py: the read-only pre-deployment audit of a SQLite submission store
(procedure: docs/VCF_STORAGE.md). It must report what the path policy would refuse, and change nothing.
"""

from __future__ import annotations

import hashlib
import importlib.util
import sqlite3
import uuid
from pathlib import Path

import pytest

from api.submission_store import SubmissionStore

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "audit_submission_paths.py"
ORG = str(uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001"))
OTHER = str(uuid.UUID("bbbbbbbb-0000-4000-8000-000000000002"))


@pytest.fixture(scope="module")
def audit_module():
    spec = importlib.util.spec_from_file_location("audit_submission_paths", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def store_file(tmp_path):
    return tmp_path / "copy-of-submissions.db"


def add(store, key, org, path, status):
    sub = store.create_submission(
        org_id=org,
        submission_key=key,
        vcf_path=path,
        assembly="GRCh38",
        sample_ref="s",
        consent_ref="c",
    )
    if status != "queued":
        store.update_status(sub.id, status)
    return sub.id


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_it_lists_exactly_the_open_rows_the_policy_would_refuse(audit_module, store_file, vcf_root, capsys):
    (vcf_root / ORG).mkdir()
    (vcf_root / ORG / "good.vcf").write_text("##fileformat=VCFv4.2\n", encoding="utf-8")
    store = SubmissionStore(store_file, reconcile_on_open=False)
    ids = {
        "ok": add(store, "k1", ORG, str(vcf_root / ORG / "good.vcf"), "queued"),
        "ok_missing": add(store, "k2", ORG, "not-yet.vcf", "queued"),
        "outside": add(store, "k3", ORG, "/etc/passwd.vcf", "queued"),
        "traversal": add(store, "k4", ORG, "../x.vcf", "running"),
        "wrong_org": add(store, "k5", ORG, str(vcf_root / OTHER / "theirs.vcf"), "interrupted"),
        "not_vcf": add(store, "k6", ORG, "notes.txt", "queued"),
        # rows that are no longer open are not the audit's business
        "done_outside": add(store, "k7", ORG, "/etc/passwd.vcf", "complete"),
        "failed_outside": add(store, "k8", ORG, "/etc/passwd.vcf", "failed"),
    }
    audited, refused = audit_module.audit(store_file)
    assert audited == 6
    assert {sub_id: code for sub_id, _status, _org, code in refused} == {
        ids["outside"]: "vcf_path_outside_organisation_directory",
        ids["traversal"]: "vcf_path_traversal",
        ids["wrong_org"]: "vcf_path_outside_organisation_directory",
        ids["not_vcf"]: "vcf_path_not_a_vcf",
    }


def test_it_changes_nothing_and_leaves_no_journal_files(audit_module, store_file, vcf_root):
    store = SubmissionStore(store_file, reconcile_on_open=False)
    add(store, "k1", ORG, "/etc/passwd.vcf", "queued")
    before = digest(store_file)
    siblings_before = sorted(p.name for p in store_file.parent.iterdir())
    audit_module.audit(store_file)
    assert digest(store_file) == before
    assert sorted(p.name for p in store_file.parent.iterdir()) == siblings_before


def test_the_connection_it_uses_cannot_write(audit_module, store_file):
    """The audit opens the store with mode=ro: a write through that same kind of connection is refused by SQLite."""
    SubmissionStore(store_file, reconcile_on_open=False)
    with sqlite3.connect(f"file:{store_file.resolve().as_posix()}?mode=ro", uri=True) as conn:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM submissions")


def test_the_report_names_no_path_and_the_exit_status_says_whether_anything_would_be_refused(
    audit_module, store_file, vcf_root, capsys
):
    store = SubmissionStore(store_file, reconcile_on_open=False)
    add(store, "k1", ORG, "/very/secret/place/private-name.vcf", "queued")
    assert audit_module.main(["audit", str(store_file)]) == 1
    out = capsys.readouterr().out
    assert "private-name" not in out and "/very/secret" not in out
    assert "vcf_path_outside_organisation_directory" in out and "would_be_refused=1" in out

    clean = store_file.parent / "clean.db"
    SubmissionStore(clean, reconcile_on_open=False)
    assert audit_module.main(["audit", str(clean)]) == 0


def test_it_cannot_run_without_a_store_or_without_a_configured_root(
    audit_module, store_file, tmp_path, monkeypatch, capsys
):
    assert audit_module.main(["audit", str(tmp_path / "missing.db")]) == 2
    assert audit_module.main(["audit"]) == 2
    SubmissionStore(store_file, reconcile_on_open=False)
    store = SubmissionStore(store_file, reconcile_on_open=False)
    add(store, "k1", ORG, "a.vcf", "queued")
    monkeypatch.delenv("GEPER_VCF_ROOT")
    _audited, refused = audit_module.audit(store_file)
    assert [code for *_rest, code in refused] == ["vcf_root_not_configured"]  # an unconfigured root refuses everything
