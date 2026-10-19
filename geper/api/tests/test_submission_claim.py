"""
api/tests/test_submission_claim.py
──────────────────────────────────
No PostgreSQL. The submission store's job-claiming, requeue and open-time behaviour: F7 (two workers must not run the
same job) and the store side of F6 (a retry must not lose why a job failed).

The claim is ONE compare-and-set UPDATE (`status` queued -> running) in the store's SQLite file, so its guarantee is the
database's: it holds across threads and across processes that share the file. Both are exercised here with real
concurrency. It does not hold across hosts, because a SQLite file is not shared across hosts: that limit is the
architecture's (one store file per deployment), not something this module can change.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import pytest

import api.main as api_main
from api.submission_store import SubmissionStore

GEPER_DIR = Path(__file__).resolve().parents[2]
ORG = "00000000-0000-4000-8000-000000000001"
ORDER = "00000000-0000-4000-8000-000000000002"
SAMPLE = "00000000-0000-4000-8000-000000000003"


@pytest.fixture
def store(tmp_path):
    return SubmissionStore(tmp_path / "store.db")


def make(store, key="k", **overrides):
    values = dict(
        org_id=ORG,
        submission_key=key,
        vcf_path="/data/x.vcf",
        assembly="GRCh38",
        sample_ref="s",
        consent_ref="c",
        order_id=ORDER,
        sample_id=SAMPLE,
    )
    values.update(overrides)
    return store.create_submission(**values)


def status(store, submission):
    return store.get_submission(submission.id).status


# ═══ the claim ═══


def test_a_queued_submission_is_claimed_once_and_records_who_and_which_attempt(store):
    sub = make(store)
    assert store.claim(sub.id, "worker-a") == 1
    assert status(store, sub) == "running"
    assert store.claim(sub.id, "worker-b") is None  # already taken
    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute("SELECT claimed_by, attempts FROM submissions WHERE id = ?", (sub.id,)).fetchone() == (
            "worker-a",
            1,
        )


@pytest.mark.parametrize("state", ["running", "complete", "failed", "interrupted"])
def test_only_a_queued_submission_can_be_claimed(store, state):
    sub = make(store)
    store.update_status(sub.id, state)
    assert store.claim(sub.id, "worker") is None
    assert status(store, sub) == state  # and the refusal changed nothing


def test_an_unknown_submission_cannot_be_claimed(store):
    assert store.claim("no-such-id", "worker") is None


def test_the_attempt_number_counts_every_claim(store):
    sub = make(store)
    assert store.claim(sub.id, "w") == 1
    store.update_status(sub.id, "failed", error_message="x", failure_code="bij_ai_timeout")
    assert store.requeue(sub.id) is True
    assert store.claim(sub.id, "w") == 2


def test_many_threads_racing_for_one_submission_produce_exactly_one_winner(store):
    sub = make(store)
    results, barrier = [], threading.Barrier(24)

    def contend(n):
        barrier.wait()
        results.append(store.claim(sub.id, f"w{n}"))

    threads = [threading.Thread(target=contend, args=(n,)) for n in range(24)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert sorted(r for r in results if r is not None) == [1] and results.count(None) == 23


CLAIM_CHILD = (
    "import sys; sys.path.insert(0, sys.argv[1])\n"
    "from api.submission_store import SubmissionStore\n"
    "store = SubmissionStore(sys.argv[2], reconcile_on_open=False)\n"
    "print(store.claim(sys.argv[3], 'process-' + sys.argv[4]))\n"
)


def test_separate_processes_racing_for_one_submission_produce_exactly_one_winner(store, tmp_path):
    sub = make(store)
    children = [
        subprocess.Popen(
            [sys.executable, "-c", CLAIM_CHILD, str(GEPER_DIR), str(store.db_path), sub.id, str(n)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for n in range(5)
    ]
    outputs = []
    for child in children:
        out, err = child.communicate(timeout=120)
        assert child.returncode == 0, err
        outputs.append(out.strip())
    assert sorted(outputs) == ["1", "None", "None", "None", "None"]
    assert status(store, sub) == "running"


# ═══ requeue keeps the reason; retries are matched by failure code ═══


def test_requeue_moves_only_a_failed_row_and_keeps_its_reason(store):
    sub = make(store)
    store.claim(sub.id, "w")
    store.update_status(
        sub.id, "failed", error_message="Interpretation timed out after 2s", failure_code="bij_ai_timeout"
    )
    assert store.requeue(sub.id) is True
    again = store.get_submission(sub.id)
    assert again.status == "queued" and again.error_message == "Interpretation timed out after 2s"
    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute("SELECT failure_code FROM submissions WHERE id = ?", (sub.id,)).fetchone() == (
            "bij_ai_timeout",
        )


@pytest.mark.parametrize("state", ["queued", "running", "complete", "interrupted"])
def test_requeue_does_not_touch_a_row_that_is_not_failed(store, state):
    sub = make(store)
    store.update_status(sub.id, state)
    assert store.requeue(sub.id) is False
    assert status(store, sub) == state


def test_failed_submission_ids_match_organisation_order_and_code_exactly(store):
    timed_out = make(store, "a")
    other_code = make(store, "b")
    other_order = make(store, "c", order_id="00000000-0000-4000-8000-0000000000ff")
    other_org = make(store, "d", org_id="00000000-0000-4000-8000-0000000000aa")
    for sub, code in (
        (timed_out, "bij_ai_timeout"),
        (other_code, "vcf_missing"),
        (other_order, "bij_ai_timeout"),
        (other_org, "bij_ai_timeout"),
    ):
        store.update_status(sub.id, "failed", error_message="x", failure_code=code)
    assert store.failed_submission_ids(ORG, ORDER, "bij_ai_timeout") == [timed_out.id]
    assert store.failed_submission_ids(ORG, ORDER, "no_such_code") == []


# ═══ opening the store ═══


def test_opening_the_store_as_the_owner_marks_running_rows_interrupted(tmp_path):
    first = SubmissionStore(tmp_path / "s.db")
    sub = make(first)
    first.claim(sub.id, "w")
    assert SubmissionStore(tmp_path / "s.db").get_submission(sub.id).status == "interrupted"


def test_opening_the_store_without_reconcile_leaves_a_running_job_alone(tmp_path):
    first = SubmissionStore(tmp_path / "s.db")
    sub = make(first)
    first.claim(sub.id, "w")
    assert SubmissionStore(tmp_path / "s.db", reconcile_on_open=False).get_submission(sub.id).status == "running"


def test_the_api_opening_its_store_does_not_reclassify_a_job_a_worker_is_running(tmp_path, monkeypatch):
    path = tmp_path / "api-store.db"
    worker_side = SubmissionStore(path)
    sub = make(worker_side)
    worker_side.claim(sub.id, "w")
    monkeypatch.setenv("GEPER_SUBMISSION_STORE_PATH", str(path))
    monkeypatch.setattr(api_main, "_submission_store", None)  # a fresh API process
    opened = api_main.get_submission_store()
    assert opened.get_submission(sub.id).status == "running"


# ═══ stores created before these columns existed ═══


def test_a_store_file_from_before_the_claim_columns_is_upgraded_in_place(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE submissions (
                id TEXT PRIMARY KEY, org_id TEXT NOT NULL, order_id TEXT, submission_key TEXT NOT NULL,
                vcf_path TEXT NOT NULL, assembly TEXT NOT NULL, sample_ref TEXT NOT NULL, consent_ref TEXT NOT NULL,
                status TEXT NOT NULL, interpretation_id TEXT, run_document_ref TEXT, error_message TEXT,
                hpo_terms TEXT, qc_metrics TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE(org_id, submission_key)
            )
            """
        )
        conn.execute(
            "INSERT INTO submissions (id, org_id, order_id, submission_key, vcf_path, assembly, sample_ref, consent_ref, "
            "status, created_at, updated_at) VALUES ('old-1', ?, ?, 'k', '/x.vcf', 'GRCh38', 's', 'c', 'queued', 't', 't')",
            (ORG, ORDER),
        )
    upgraded = SubmissionStore(path)
    assert upgraded.get_submission("old-1").status == "queued"
    assert upgraded.claim("old-1", "w") == 1  # the old row has attempts 0 and no failure code, and can be claimed
    assert os.path.exists(path)
