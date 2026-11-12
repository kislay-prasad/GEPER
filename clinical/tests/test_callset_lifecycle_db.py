"""
CALL-SET LIFECYCLE AND DATABASE-SECURITY TESTS (migration 0010), COLLECTED BY THE REPOSITORY'S SUITE.

This is the repository-collected lifecycle and database-security suite. It is derived from a design-verification
suite, `test_lifecycle.py`, written to verify the proposed v3 SQL
(docs/design/genome_callsets/0010_call_sets.proposed.sql); that design suite is NOT included in the repository.
This file runs in CI: it builds a THROWAWAY database from the repository's own
`clinical/schema.sql` with `clinical.bootstrap` (real roles, real grants), and exercises the triggers, CHECKs, grants
and the purge's database half through REAL LOGIN connections as `clinical_app` and `clinical_retention`.

Differences from the design suite (not in the repository): it always runs against the repository schema (the proposed
SQL is not applied again);
the weakened-variant matrix is not carried (it needs the proposed SQL as a separate file; it was green 20/20 before
promotion, and `test_callset_migration.py` pins the search_path and `TEMP` properties it demonstrated); the four shadowing
attacks run against the real objects; and expectations changed by the final review (cross-run duplicate rule, wrong-run
retraction, the event/tombstone lock, identity guards) are updated.

Needs CLINICAL_TEST_DSN (a disposable superuser DSN); it creates and drops its own database. Never run it concurrently
with another suite on the same cluster: it sets the passwords of the cluster-wide application roles.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import pathlib
import re
import sys
import threading
import urllib.parse
import uuid
from datetime import datetime, timezone

import psycopg
import pytest
from psycopg.types.json import Jsonb

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

ADMIN_DSN = os.environ.get("CLINICAL_TEST_DSN")
PASSWORDS = {"clinical_app": "test-app-password", "clinical_retention": "test-retention-password"}
DBNAME = "callset_lifecycle_ci"

GOOD_CHECKS = {"sample_identity": True, "assembly": True, "caller": True}
ALL_EVENTS = (
    "submitted",
    "validated",
    "rejected",
    "withdrawn",
    "superseded",
    "retraction_requested",
    "retraction_declined",
    "retracted",
)
STATES = ("pending_validation", "validated", "rejected", "withdrawn", "superseded", "retracted")
ALLOWED = {  # THE TRANSITION TABLE. Everything not listed is forbidden.
    "pending_validation": {"validated", "rejected", "withdrawn"},
    "validated": {"retraction_requested", "retracted"},
    "rejected": set(),
    "withdrawn": set(),
    "superseded": set(),
    "retracted": set(),
}
FUNCS = (
    "actor_holds_role",
    "actor_context",
    "caller_state",
    "call_set_state",
    "open_retraction_request",
    "call_sets_before_insert",
    "call_sets_require_submitted",
    "call_sets_immutable",
    "call_sets_require_redaction_audit",
    "call_set_events_before_insert",
    "call_set_events_after_insert",
    "call_set_events_immutable",
    "caller_approvals_before_insert",
    "caller_approvals_no_update",
)
TABLES = (
    "call_sets",
    "call_set_events",
    "caller_approvals",
    "role_assignments",
    "users",
    "vcfs",
    "sequencing_runs",
    "audit_log",
)
MARK = b"GENOMIC-MARKER-7f3a9c"

pytestmark = pytest.mark.skipif(not ADMIN_DSN, reason="CLINICAL_TEST_DSN not set")


# ─── infrastructure: the REAL schema, REAL roles, REAL logins ────────────────


def _dsn_for(user, password, db):
    u = urllib.parse.urlparse(ADMIN_DSN)
    return f"postgresql://{user}:{password}@{u.hostname}:{u.port}/{db}"


@pytest.fixture(scope="module")
def dsn():
    u = urllib.parse.urlparse(ADMIN_DSN)
    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
        admin.execute(f"DROP DATABASE IF EXISTS {DBNAME} WITH (FORCE)")
        admin.execute(f"CREATE DATABASE {DBNAME}")
    target = _dsn_for(u.username, u.password, DBNAME)
    from clinical.bootstrap import apply_schema, ensure_roles

    boot = psycopg.connect(target, autocommit=False)
    ensure_roles(boot, app_password=PASSWORDS["clinical_app"], retention_password=PASSWORDS["clinical_retention"])
    apply_schema(boot)
    boot.commit()
    boot.close()
    yield target
    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
        admin.execute(f"DROP DATABASE IF EXISTS {DBNAME} WITH (FORCE)")


def connect(dsn, role=None):
    if role:
        u = urllib.parse.urlparse(dsn)
        return psycopg.connect(_dsn_for(role, PASSWORDS[role], u.path.lstrip("/")), autocommit=True)
    return psycopg.connect(dsn, autocommit=True)


@pytest.fixture()
def su(dsn):
    c = connect(dsn)
    yield c
    c.close()


@pytest.fixture()
def app(dsn):
    c = connect(dsn, "clinical_app")
    yield c
    c.close()


@pytest.fixture()
def ret(dsn):
    c = connect(dsn, "clinical_retention")
    yield c
    c.close()


@contextlib.contextmanager
def ctx(conn, actor):
    """One transaction with the actor context set -- what a DAO verb would do."""
    with conn.transaction():
        conn.execute("SELECT set_config('clinical.actor_id', %s, true)", (str(actor),))
        yield


class Org:
    pass


def add_user(su, o, roles):
    uid = su.execute(
        "INSERT INTO users (org_id,email,password_hash,password_changed_at,created_at,updated_at) "
        "VALUES (%s,%s,'x',now(),now(),now()) RETURNING user_id",
        (o.id, f"u{uuid.uuid4().hex[:10]}@t.example"),
    ).fetchone()[0]
    for r in roles:
        su.execute(
            "INSERT INTO role_assignments (user_id,org_id,role,assigned_by,assigned_at,basis) VALUES (%s,%s,%s,%s,now(),%s)",
            (uid, o.id, r, uid, "design test" if r == "Approver" else None),
        )
    return uid


def add_run(su, o, sample=None):
    run = uuid.uuid4()
    su.execute(
        "INSERT INTO sequencing_runs (org_id,id,sample_id,created_at,created_by) VALUES (%s,%s,%s,now(),%s)",
        (o.id, run, sample or o.sample, o.u["lab1"]),
    )
    return run


def make_org(su, app):
    o = Org()
    o.id = su.execute(
        "INSERT INTO organisations (name,created_at) VALUES ('Design Org', now()) RETURNING org_id"
    ).fetchone()[0]
    spec = {
        "lab1": ["Lab technician"],
        "lab2": ["Lab technician"],
        "int1": ["Interpreter"],
        "int2": ["Interpreter"],
        "app1": ["Approver"],
        "app2": ["Approver"],
        "adm1": ["Administrator"],
        "adm2": ["Administrator"],
        "dual": ["Lab technician", "Interpreter"],
        "norole": [],
        "ordr": ["Orderer"],
    }
    o.u = {n: add_user(su, o, r) for n, r in spec.items()}
    patient = su.execute(
        "INSERT INTO patients (org_id,name,dob,sex,created_at) VALUES (%s,'Design Patient','1990-01-01','F',now()) RETURNING patient_id",
        (o.id,),
    ).fetchone()[0]
    test = su.execute(
        "INSERT INTO tests (org_id,name,assembly,created_at) VALUES (%s,'Panel','GRCh38',now()) RETURNING test_id",
        (o.id,),
    ).fetchone()[0]
    consent = su.execute(
        "INSERT INTO consents (patient_id,org_id,scope,recorded_by,recorded_at) VALUES (%s,%s,'testing',%s,now()) RETURNING consent_id",
        (patient, o.id, o.u["ordr"]),
    ).fetchone()[0]
    order = su.execute(
        "INSERT INTO orders (patient_id,org_id,test_id,required_scope,ordered_by,consent_id) VALUES (%s,%s,%s,'testing',%s,%s) RETURNING order_id",
        (patient, o.id, test, o.u["ordr"], consent),
    ).fetchone()[0]
    o.sample = su.execute(
        "INSERT INTO samples (order_id,org_id,type,collected_at,collected_by,qc_status) VALUES (%s,%s,'blood',%s,%s,'passed') RETURNING sample_id",
        (order, o.id, datetime(2026, 1, 1, tzinfo=timezone.utc), o.u["lab1"]),
    ).fetchone()[0]
    o.run, o.run2 = add_run(su, o), add_run(su, o)
    o.approval = approve_caller(app, o, "Caller", "1.0", "sv")
    return o


def doc(cls="sv", assembly="GRCh38", n=1, marker=False):
    recs = [{"line_no": i, **({"note": MARK.decode()} if marker else {})} for i in range(n)]
    return Jsonb(
        {"schema_version": "1", "assembly": assembly, "record_class": cls, "counts": {"records": n}, "records": recs}
    )


def insert_approval(conn, o, name, version, cls, typ, actor, role, basis="ref"):
    with ctx(conn, actor):
        return conn.execute(
            "INSERT INTO caller_approvals (org_id,caller_name,caller_version,record_class,event_type,actor_id,actor_role,occurred_at,basis) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,now(),%s) RETURNING id",
            (o.id, name, version, cls, typ, actor, role, basis),
        ).fetchone()[0]


def approve_caller(conn, o, name, version, cls, basis="lab validation ref"):
    insert_approval(conn, o, name, version, cls, "proposed", o.u["adm1"], "Administrator", basis)
    return insert_approval(conn, o, name, version, cls, "approved", o.u["app1"], "Approver", basis)


def raw_call_set(
    conn,
    o,
    sid,
    creator,
    data,
    *,
    document=None,
    assembly="GRCh38",
    cls="sv",
    filename="a.vcf",
    h=None,
    retention=1825,
    caller=("Caller", "1.0"),
    run=None,
    supersedes=None,
):
    conn.execute(
        "INSERT INTO call_sets (org_id,id,sequencing_run_id,record_class,assembly,caller_name,caller_version,caller_approved_at_submission,"
        "vcf_sample_name,source_filename,source_vcf,source_content_hash,parser_version,parsed_document,supersedes_id,created_at,created_by,retention_days) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,false,'S1',%s,%s,%s,'p1',%s,%s,now(),%s,%s)",
        (
            o.id,
            sid,
            run or o.run,
            cls,
            assembly,
            caller[0],
            caller[1],
            filename,
            data,
            h or hashlib.sha256(data).hexdigest(),
            document if document is not None else doc(cls, assembly),
            supersedes,
            creator,
            retention,
        ),
    )


def ev(conn, o, sid, typ, actor, role, *, reason=None, related=None, responds=None, approval=None, checks=None):
    if checks is None:
        checks = GOOD_CHECKS if typ == "validated" else {}
    if typ == "validated" and approval is None:
        approval = o.approval
    with ctx(conn, o.u[actor]):
        return conn.execute(
            "INSERT INTO call_set_events (org_id,call_set_id,event_type,actor_id,actor_role,occurred_at,reason,checks,related_call_set_id,responds_to,caller_approval_id) "
            "VALUES (%s,%s,%s,%s,%s,now(),%s,%s,%s,%s,%s) RETURNING id",
            (o.id, sid, typ, o.u[actor], role, reason, Jsonb(checks), related, responds, approval),
        ).fetchone()[0]


def submit(
    conn,
    o,
    lab="lab1",
    *,
    run=None,
    cls="sv",
    assembly="GRCh38",
    caller=("Caller", "1.0"),
    payload=None,
    supersedes=None,
    retention=1825,
    marker=False,
):
    sid = uuid.uuid4()
    if supersedes is not None and run is None:
        # a replacement is on its predecessor's run (the composite foreign key says so)
        run = conn.execute("SELECT sequencing_run_id FROM call_sets WHERE id = %s", (supersedes,)).fetchone()[0]
    data = (
        payload if payload is not None else (MARK if marker else b"") + uuid.uuid4().bytes + b"##fileformat=VCFv4.2\n"
    )
    with ctx(conn, o.u[lab]):
        raw_call_set(
            conn,
            o,
            sid,
            o.u[lab],
            data,
            document=doc(cls, assembly, marker=marker),
            assembly=assembly,
            cls=cls,
            caller=caller,
            run=run,
            supersedes=supersedes,
            retention=retention,
        )
        conn.execute(
            "INSERT INTO call_set_events (org_id,call_set_id,event_type,actor_id,actor_role,occurred_at) VALUES (%s,%s,'submitted',%s,'Lab technician',now())",
            (o.id, sid, o.u[lab]),
        )
    return sid


def state(conn, o, sid):
    return conn.execute("SELECT public.call_set_state(%s,%s)", (o.id, sid)).fetchone()[0]


def n_events(conn, o, sid):
    return conn.execute(
        "SELECT count(*) FROM call_set_events WHERE org_id=%s AND call_set_id=%s", (o.id, sid)
    ).fetchone()[0]


def validate(conn, o, sid, actor="int2"):
    return ev(conn, o, sid, "validated", actor, "Interpreter")


def make_state(conn, o, target, **kw):
    # ONE active validated call set per run/class/assembly (wave 4): every state built here gets a run of its own,
    # unless the caller names one; a chain (superseded) shares its single run across its members.
    if kw.get("run") is None:
        kw["run"] = add_run(conn, o)
    if target == "pending_validation":
        return submit(conn, o, **kw)
    if target == "validated":
        s = submit(conn, o, **kw)
        validate(conn, o, s)
        return s
    if target == "rejected":
        s = submit(conn, o, **kw)
        ev(conn, o, s, "rejected", "int2", "Interpreter", reason="wrong sample")
        return s
    if target == "withdrawn":
        s = submit(conn, o, **kw)
        ev(conn, o, s, "withdrawn", "lab1", "Lab technician", reason="mistake")
        return s
    if target == "retracted":
        s = make_state(conn, o, "validated", **kw)
        ev(conn, o, s, "retracted", "app1", "Approver", reason="later found wrong")
        return s
    if target == "superseded":
        pred = make_state(conn, o, "validated", **kw)
        succ = submit(conn, o, supersedes=pred, **kw)
        validate(conn, o, succ)
        return pred
    raise AssertionError(target)


def fails(fn, exc=psycopg.Error):
    with pytest.raises(exc):
        fn()


# ─── 1. the exhaustive state x event matrix ──────────────────────────────────


@pytest.mark.parametrize("target", STATES)
@pytest.mark.parametrize("event", ALL_EVENTS)
def test_every_state_and_event_pair_matches_the_transition_table(su, app, target, event):
    o = make_org(su, app)
    sid = make_state(app, o, target)
    assert state(app, o, sid) == target
    other_validated = make_state(app, o, "validated")
    before = n_events(app, o, sid)
    attempts = {
        "submitted": lambda: ev(app, o, sid, "submitted", "lab1", "Lab technician"),
        "validated": lambda: validate(app, o, sid, "int2"),
        "rejected": lambda: ev(app, o, sid, "rejected", "int2", "Interpreter", reason="r"),
        "withdrawn": lambda: ev(app, o, sid, "withdrawn", "lab1", "Lab technician", reason="r"),
        "superseded": lambda: ev(app, o, sid, "superseded", "int2", "Interpreter", related=other_validated),
        "retraction_requested": lambda: ev(app, o, sid, "retraction_requested", "int1", "Interpreter", reason="r"),
        "retraction_declined": lambda: ev(
            app,
            o,
            sid,
            "retraction_declined",
            "app1",
            "Approver",
            reason="r",
            responds=app.execute("SELECT id FROM call_set_events WHERE call_set_id=%s LIMIT 1", (sid,)).fetchone()[0],
        ),
        "retracted": lambda: ev(app, o, sid, "retracted", "app1", "Approver", reason="r"),
    }
    if event in ALLOWED[target]:
        attempts[event]()
        assert n_events(app, o, sid) >= before + 1
    else:
        fails(attempts[event])
        assert n_events(app, o, sid) == before, "a refused event must leave no trace"
        assert state(app, o, sid) == target


# ─── 2. permitted transitions and their consequences ─────────────────────────


def test_submit_creates_a_pending_set_with_the_database_snapshot_of_caller_status(su, app):
    o = make_org(su, app)
    s = submit(app, o)
    assert state(app, o, s) == "pending_validation"
    assert app.execute("SELECT caller_approved_at_submission FROM call_sets WHERE id=%s", (s,)).fetchone()[0] is True
    u = submit(app, o, caller=("Unlisted", "9.9"))
    assert app.execute("SELECT caller_approved_at_submission FROM call_sets WHERE id=%s", (u,)).fetchone()[0] is False


def test_the_database_clock_not_the_caller_stamps_events(su, app):
    o = make_org(su, app)
    s = submit(app, o)
    with ctx(app, o.u["lab1"]):
        app.execute(
            "INSERT INTO call_set_events (org_id,call_set_id,event_type,actor_id,actor_role,occurred_at,reason) "
            "VALUES (%s,%s,'withdrawn',%s,'Lab technician','1999-01-01',%s)",
            (o.id, s, o.u["lab1"], "mistake"),
        )
    assert app.execute(
        "SELECT occurred_at > now() - interval '1 minute' FROM call_set_events WHERE call_set_id=%s AND event_type='withdrawn'",
        (s,),
    ).fetchone()[0]


def test_validation_records_the_approval_relied_on_and_changes_nothing_in_the_evidence(su, app):
    o = make_org(su, app)
    s = submit(app, o)
    before = su.execute("SELECT md5(t::text) FROM call_sets t WHERE id=%s", (s,)).fetchone()[0]
    e = validate(app, o, s)
    assert state(app, o, s) == "validated"
    assert app.execute("SELECT caller_approval_id FROM call_set_events WHERE id=%s", (e,)).fetchone()[0] == o.approval
    assert su.execute("SELECT md5(t::text) FROM call_sets t WHERE id=%s", (s,)).fetchone()[0] == before


def test_rejection_and_withdrawal_keep_the_evidence_and_the_reason(su, app):
    o = make_org(su, app)
    r, w = make_state(app, o, "rejected"), make_state(app, o, "withdrawn")
    assert app.execute(
        "SELECT length(source_vcf) > 0 AND parsed_document IS NOT NULL FROM call_sets WHERE id=%s", (r,)
    ).fetchone()[0]
    assert (
        app.execute(
            "SELECT reason FROM call_set_events WHERE call_set_id=%s AND event_type='rejected'", (r,)
        ).fetchone()[0]
        == "wrong sample"
    )
    assert state(app, o, w) == "withdrawn"


def test_an_interpreter_can_request_retraction_and_the_approver_can_decline_then_it_can_be_requested_again(su, app):
    o = make_org(su, app)
    s = make_state(app, o, "validated")
    req = ev(app, o, s, "retraction_requested", "int1", "Interpreter", reason="possible sample swap")
    assert state(app, o, s) == "validated"
    assert app.execute("SELECT public.open_retraction_request(%s,%s)", (o.id, s)).fetchone()[0] == req
    dec = ev(app, o, s, "retraction_declined", "app1", "Approver", reason="checked, identity confirmed", responds=req)
    assert state(app, o, s) == "validated"
    assert app.execute("SELECT public.open_retraction_request(%s,%s)", (o.id, s)).fetchone()[0] is None
    req2 = ev(app, o, s, "retraction_requested", "int1", "Interpreter", reason="new evidence")
    assert req2 != req and dec


def test_an_approver_can_retract_in_answer_to_a_request(su, app):
    o = make_org(su, app)
    s = make_state(app, o, "validated")
    req = ev(app, o, s, "retraction_requested", "int1", "Interpreter", reason="swap")
    ev(app, o, s, "retracted", "app1", "Approver", reason="confirmed swap", responds=req)
    assert state(app, o, s) == "retracted"


def test_an_approver_can_retract_directly_without_a_request(su, app):
    o = make_org(su, app)
    s = make_state(app, o, "validated")
    ev(app, o, s, "retracted", "app1", "Approver", reason="found by approver")
    assert state(app, o, s) == "retracted"


# ─── 3. supersession ─────────────────────────────────────────────────────────


def test_validating_a_successor_writes_a_linked_superseded_event_on_the_predecessor(su, app):
    o = make_org(su, app)
    pred = make_state(app, o, "validated")
    succ = submit(app, o, supersedes=pred)
    assert state(app, o, pred) == "validated"
    validate(app, o, succ, "int1")
    assert state(app, o, pred) == "superseded" and state(app, o, succ) == "validated"
    row = app.execute(
        "SELECT related_call_set_id, actor_id, actor_role FROM call_set_events WHERE call_set_id=%s AND event_type='superseded'",
        (pred,),
    ).fetchone()
    assert row == (succ, o.u["int1"], "Interpreter")


def test_a_rejected_or_withdrawn_successor_leaves_the_predecessor_validated(su, app):
    o = make_org(su, app)
    pred = make_state(app, o, "validated")
    succ = submit(app, o, supersedes=pred)
    ev(app, o, succ, "rejected", "int2", "Interpreter", reason="bad")
    assert state(app, o, pred) == "validated"


def test_a_retraction_while_a_successor_is_pending_leaves_the_lineage_but_no_superseded_event(su, app):
    o = make_org(su, app)
    pred = make_state(app, o, "validated")
    succ = submit(app, o, supersedes=pred)
    ev(app, o, pred, "retracted", "app1", "Approver", reason="wrong")
    validate(app, o, succ)
    assert state(app, o, pred) == "retracted" and state(app, o, succ) == "validated"
    assert (
        app.execute(
            "SELECT count(*) FROM call_set_events WHERE call_set_id=%s AND event_type='superseded'", (pred,)
        ).fetchone()[0]
        == 0
    )
    assert app.execute("SELECT supersedes_id FROM call_sets WHERE id=%s", (succ,)).fetchone()[0] == pred


@pytest.mark.parametrize("pred_state", ["pending_validation", "rejected", "withdrawn", "retracted", "superseded"])
def test_a_replacement_may_only_name_a_currently_validated_set(su, app, pred_state):
    o = make_org(su, app)
    pred = make_state(app, o, pred_state)
    fails(lambda: submit(app, o, supersedes=pred))


def test_only_one_replacement_per_set_and_it_must_share_run_and_class(su, app):
    o = make_org(su, app)
    pred = make_state(app, o, "validated")
    submit(app, o, supersedes=pred)
    fails(lambda: submit(app, o, supersedes=pred))
    other = make_state(app, o, "validated")
    fails(lambda: submit(app, o, supersedes=other, run=o.run2))
    fails(lambda: submit(app, o, supersedes=other, cls="cnv"))


def test_superseded_cannot_be_written_by_hand(su, app):
    o = make_org(su, app)
    a, b = make_state(app, o, "validated"), make_state(app, o, "validated")
    fails(lambda: ev(app, o, a, "superseded", "int2", "Interpreter", related=b))
    fails(lambda: ev(app, o, a, "superseded", "int2", "Interpreter", related=a))


def test_a_failed_replacement_validation_leaves_the_old_set_validated_and_the_successor_pending(su, app):
    """Atomicity: force the AFTER trigger's superseded insert to fail and prove BOTH events vanish."""
    o = make_org(su, app)
    pred = make_state(app, o, "validated")
    succ = submit(app, o, supersedes=pred)
    su.execute(
        "CREATE FUNCTION public.zz_block_superseded() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
        "IF NEW.event_type = 'superseded' THEN RAISE EXCEPTION 'injected failure'; END IF; RETURN NEW; END $$"
    )
    su.execute(
        "CREATE TRIGGER zz_block BEFORE INSERT ON public.call_set_events FOR EACH ROW EXECUTE FUNCTION public.zz_block_superseded()"
    )
    try:
        fails(lambda: validate(app, o, succ, "int1"))
    finally:
        su.execute("DROP TRIGGER zz_block ON public.call_set_events")
        su.execute("DROP FUNCTION public.zz_block_superseded()")
    assert state(app, o, pred) == "validated" and state(app, o, succ) == "pending_validation"
    assert (
        app.execute(
            "SELECT count(*) FROM call_set_events WHERE org_id=%s AND call_set_id=%s AND event_type IN ('validated','superseded')",
            (o.id, succ),
        ).fetchone()[0]
        == 0
    )
    assert (
        app.execute(
            "SELECT count(*) FROM call_set_events WHERE org_id=%s AND call_set_id=%s AND event_type='superseded'",
            (o.id, pred),
        ).fetchone()[0]
        == 0
    )
    validate(app, o, succ, "int1")  # and it works once the fault is gone
    assert state(app, o, pred) == "superseded"


# ─── 4. roles and separation of recorded actors ──────────────────────────────

WRONG_ROLE = [
    ("validated", "lab1", "Lab technician"),
    ("validated", "app1", "Approver"),
    ("validated", "adm1", "Administrator"),
    ("validated", "norole", "Interpreter"),
    ("rejected", "lab1", "Lab technician"),
    ("rejected", "app1", "Approver"),
    ("withdrawn", "int1", "Interpreter"),
    ("retraction_requested", "app1", "Approver"),
    ("retraction_requested", "lab1", "Lab technician"),
    ("retracted", "int1", "Interpreter"),
    ("retracted", "lab1", "Lab technician"),
    ("retraction_declined", "int1", "Interpreter"),
]


@pytest.mark.parametrize("event,actor,claimed", WRONG_ROLE)
def test_the_wrong_role_cannot_perform_an_event(su, app, event, actor, claimed):
    o = make_org(su, app)
    sid = make_state(app, o, "pending_validation" if event in ("validated", "rejected", "withdrawn") else "validated")
    req = (
        ev(app, o, sid, "retraction_requested", "int1", "Interpreter", reason="r")
        if event == "retraction_declined"
        else None
    )
    before = n_events(app, o, sid)
    fails(lambda: ev(app, o, sid, event, actor, claimed, reason="r", responds=req))
    assert n_events(app, o, sid) == before


@pytest.mark.parametrize("claimed", ["Interpreter", "Lab technician", "Approver"])
def test_claiming_a_role_one_does_not_hold_is_refused(su, app, claimed):
    o = make_org(su, app)
    s = submit(app, o)
    ev_for = {"Interpreter": "rejected", "Lab technician": "withdrawn", "Approver": "retracted"}[claimed]
    fails(lambda: ev(app, o, s, ev_for, "norole", claimed, reason="r"))


def test_a_revoked_role_no_longer_works_even_between_submission_and_validation(su, app):
    o = make_org(su, app)
    s = submit(app, o)
    su.execute(
        "UPDATE role_assignments SET revoked_at=now(), revoked_by=%s WHERE user_id=%s AND role='Interpreter'",
        (o.u["adm1"], o.u["int2"]),
    )
    fails(lambda: validate(app, o, s, "int2"))
    validate(app, o, s, "int1")


def test_the_submitter_can_neither_validate_nor_reject_their_own_submission_even_with_both_roles(su, app):
    o = make_org(su, app)
    s = submit(app, o, lab="dual")
    fails(lambda: validate(app, o, s, "dual"))
    fails(lambda: ev(app, o, s, "rejected", "dual", "Interpreter", reason="self"))
    assert state(app, o, s) == "pending_validation"
    validate(app, o, s, "int1")


def test_only_the_submitter_may_withdraw(su, app):
    o = make_org(su, app)
    s = submit(app, o, lab="lab1")
    fails(lambda: ev(app, o, s, "withdrawn", "lab2", "Lab technician", reason="not mine"))
    ev(app, o, s, "withdrawn", "lab1", "Lab technician", reason="mine")


def test_only_a_lab_technician_may_submit(su, app):
    o = make_org(su, app)
    for who in ("int1", "app1", "adm1", "norole"):
        fails(lambda who=who: submit(app, o, lab=who))


def test_the_submitted_event_must_exist_and_be_by_the_creator_at_commit(su, app):
    o = make_org(su, app)
    sid, d1 = uuid.uuid4(), uuid.uuid4().bytes

    def bare():
        with ctx(app, o.u["lab1"]):
            raw_call_set(app, o, sid, o.u["lab1"], d1)

    fails(bare)
    sid2, d2 = uuid.uuid4(), uuid.uuid4().bytes

    def wrong_actor():
        with ctx(app, o.u["lab1"]):
            raw_call_set(app, o, sid2, o.u["lab1"], d2)
            ev(app, o, sid2, "submitted", "lab2", "Lab technician")

    fails(wrong_actor)
    assert app.execute("SELECT count(*) FROM call_sets WHERE id IN (%s,%s)", (sid, sid2)).fetchone()[0] == 0


# ─── 5. attestations and reasons ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "checks",
    [
        {},
        {"sample_identity": True},
        {"sample_identity": True, "assembly": True},
        {"sample_identity": True, "assembly": False, "caller": True},
        {**GOOD_CHECKS, "note": "free text"},
        {**GOOD_CHECKS, "clinical": True},
    ],
)
def test_validation_needs_exactly_the_three_true_attestations(su, app, checks):
    o = make_org(su, app)
    s = submit(app, o)
    fails(lambda: ev(app, o, s, "validated", "int2", "Interpreter", checks=checks))


def test_other_events_must_not_carry_attestations(su, app):
    o = make_org(su, app)
    s = submit(app, o)
    fails(lambda: ev(app, o, s, "rejected", "int2", "Interpreter", reason="r", checks=GOOD_CHECKS))


@pytest.mark.parametrize(
    "event,actor,role", [("rejected", "int2", "Interpreter"), ("withdrawn", "lab1", "Lab technician")]
)
@pytest.mark.parametrize("reason", [None, "", "   "])
def test_reasons_are_mandatory_where_the_design_says(su, app, event, actor, role, reason):
    o = make_org(su, app)
    s = submit(app, o)
    fails(lambda: ev(app, o, s, event, actor, role, reason=reason))


@pytest.mark.parametrize("reason", [None, " "])
def test_retraction_events_need_reasons(su, app, reason):
    o = make_org(su, app)
    s = make_state(app, o, "validated")
    fails(lambda: ev(app, o, s, "retraction_requested", "int1", "Interpreter", reason=reason))
    fails(lambda: ev(app, o, s, "retracted", "app1", "Approver", reason=reason))


# ─── 6. retraction requests ──────────────────────────────────────────────────


def test_only_one_request_may_be_open(su, app):
    o = make_org(su, app)
    s = make_state(app, o, "validated")
    ev(app, o, s, "retraction_requested", "int1", "Interpreter", reason="one")
    fails(lambda: ev(app, o, s, "retraction_requested", "int2", "Interpreter", reason="two"))


def test_a_decline_must_answer_the_open_request(su, app):
    o = make_org(su, app)
    s = make_state(app, o, "validated")
    sub = app.execute(
        "SELECT id FROM call_set_events WHERE call_set_id=%s AND event_type='submitted'", (s,)
    ).fetchone()[0]
    fails(lambda: ev(app, o, s, "retraction_declined", "app1", "Approver", reason="r", responds=sub))
    req = ev(app, o, s, "retraction_requested", "int1", "Interpreter", reason="r")
    fails(lambda: ev(app, o, s, "retraction_declined", "app1", "Approver", reason="r"))
    ev(app, o, s, "retraction_declined", "app1", "Approver", reason="ok", responds=req)
    fails(lambda: ev(app, o, s, "retraction_declined", "app2", "Approver", reason="again", responds=req))


def test_a_retraction_may_only_cite_the_open_request(su, app):
    o = make_org(su, app)
    s = make_state(app, o, "validated")
    sub = app.execute(
        "SELECT id FROM call_set_events WHERE call_set_id=%s AND event_type='submitted'", (s,)
    ).fetchone()[0]
    fails(lambda: ev(app, o, s, "retracted", "app1", "Approver", reason="r", responds=sub))


def test_requests_do_not_exist_on_non_validated_sets(su, app):
    o = make_org(su, app)
    for st in ("pending_validation", "rejected", "withdrawn", "superseded", "retracted"):
        s = make_state(app, o, st)
        fails(lambda s=s: ev(app, o, s, "retraction_requested", "int1", "Interpreter", reason="r"))


# ─── 7. caller approval, quarantine, retired callers ─────────────────────────


def test_a_set_from_an_unapproved_caller_is_preserved_but_cannot_be_validated(su, app):
    o = make_org(su, app)
    s = submit(app, o, caller=("Unlisted", "2.0"))
    assert state(app, o, s) == "pending_validation"
    assert app.execute("SELECT length(source_vcf) > 0 FROM call_sets WHERE id=%s", (s,)).fetchone()[0]
    fails(lambda: validate(app, o, s))
    fails(lambda: ev(app, o, s, "validated", "int2", "Interpreter", approval=uuid.uuid4()))
    assert state(app, o, s) == "pending_validation"


def test_approving_the_caller_later_lets_the_same_untouched_set_be_validated(su, app):
    o = make_org(su, app)
    s = submit(app, o, caller=("Late", "1.0"))
    before = su.execute("SELECT md5(t::text) FROM call_sets t WHERE id=%s", (s,)).fetchone()[0]
    fails(lambda: validate(app, o, s))
    new_approval = approve_caller(app, o, "Late", "1.0", "sv")
    ev(app, o, s, "validated", "int2", "Interpreter", approval=new_approval)
    assert state(app, o, s) == "validated"
    assert su.execute("SELECT md5(t::text) FROM call_sets t WHERE id=%s", (s,)).fetchone()[0] == before


def test_approval_is_per_exact_version_and_class(su, app):
    o = make_org(su, app)
    for caller, cls in ((("Caller", "1.1"), "sv"), (("Caller", "1.0"), "cnv")):
        s = submit(app, o, caller=caller, cls=cls)
        fails(lambda s=s: validate(app, o, s))


def test_a_retired_caller_cannot_validate_and_a_stale_approval_id_is_refused(su, app):
    o = make_org(su, app)
    s = submit(app, o)
    insert_approval(app, o, "Caller", "1.0", "sv", "retired", o.u["app1"], "Approver", "withdrawn from use")
    fails(lambda: validate(app, o, s))
    new = approve_caller(app, o, "Caller", "1.0", "sv")
    fails(lambda: ev(app, o, s, "validated", "int2", "Interpreter", approval=o.approval))
    ev(app, o, s, "validated", "int2", "Interpreter", approval=new)


def test_a_caller_retired_between_submission_and_validation_blocks_validation_but_not_history(su, app):
    o = make_org(su, app)
    done = make_state(app, o, "validated")
    pending = submit(app, o)
    insert_approval(app, o, "Caller", "1.0", "sv", "retired", o.u["app1"], "Approver", "retired")
    fails(lambda: validate(app, o, pending))
    assert state(app, o, done) == "validated"
    ev(app, o, pending, "rejected", "int2", "Interpreter", reason="caller retired")
    assert state(app, o, pending) == "rejected"


def test_only_an_approved_row_in_force_can_be_cited_not_a_proposal_or_a_retirement(su, app):
    o = make_org(su, app)
    prop = insert_approval(app, o, "P", "1", "sv", "proposed", o.u["adm1"], "Administrator")
    s = submit(app, o, caller=("P", "1"))
    fails(lambda: ev(app, o, s, "validated", "int2", "Interpreter", approval=prop))
    appr = approve_caller(app, o, "R", "1", "sv")
    ret = insert_approval(app, o, "R", "1", "sv", "retired", o.u["app1"], "Approver")
    s2 = submit(app, o, caller=("R", "1"))
    fails(lambda: ev(app, o, s2, "validated", "int2", "Interpreter", approval=ret))
    fails(lambda: ev(app, o, s2, "validated", "int2", "Interpreter", approval=appr))
    assert state(app, o, s) == state(app, o, s2) == "pending_validation"


def test_an_approval_for_another_caller_version_or_class_cannot_be_borrowed(su, app):
    o = make_org(su, app)
    other = approve_caller(app, o, "Caller", "2.0", "sv")
    s = submit(app, o, caller=("Caller", "1.1"))
    fails(lambda: ev(app, o, s, "validated", "int2", "Interpreter", approval=other))
    s3 = submit(app, o, caller=("Solo", "1.0"), cls="sv")
    solo_cnv = approve_caller(app, o, "Solo", "1.0", "cnv")
    fails(lambda: ev(app, o, s3, "validated", "int2", "Interpreter", approval=solo_cnv))


def test_a_set_with_no_caller_version_can_never_be_validated(su, app):
    o = make_org(su, app)
    sid, data = uuid.uuid4(), uuid.uuid4().bytes
    with ctx(app, o.u["lab1"]):
        app.execute(
            "INSERT INTO call_sets (org_id,id,sequencing_run_id,record_class,assembly,caller_name,caller_approved_at_submission,vcf_sample_name,"
            "source_filename,source_vcf,source_content_hash,parser_version,parsed_document,created_at,created_by,retention_days) "
            "VALUES (%s,%s,%s,'sv','GRCh38','Caller',false,'S','a.vcf',%s,%s,'p',%s,now(),%s,1825)",
            (o.id, sid, o.run, data, hashlib.sha256(data).hexdigest(), doc(), o.u["lab1"]),
        )
        app.execute(
            "INSERT INTO call_set_events (org_id,call_set_id,event_type,actor_id,actor_role,occurred_at) VALUES (%s,%s,'submitted',%s,'Lab technician',now())",
            (o.id, sid, o.u["lab1"]),
        )
    fails(lambda: validate(app, o, sid))


CALLER_ALLOWED = {None: {"proposed"}, "proposed": {"approved"}, "approved": {"retired"}, "retired": {"proposed"}}
ACTOR_FOR = {"proposed": ("adm1", "Administrator"), "approved": ("app1", "Approver"), "retired": ("app1", "Approver")}


def caller_in_state(app, o, name, target):
    if target is None:
        return
    insert_approval(app, o, name, "1", "sv", "proposed", o.u["adm1"], "Administrator")
    if target in ("approved", "retired"):
        insert_approval(app, o, name, "1", "sv", "approved", o.u["app1"], "Approver")
    if target == "retired":
        insert_approval(app, o, name, "1", "sv", "retired", o.u["app1"], "Approver")


@pytest.mark.parametrize("target", [None, "proposed", "approved", "retired"])
@pytest.mark.parametrize("event", ["proposed", "approved", "retired"])
def test_every_caller_approval_transition_matches_the_table(su, app, target, event):
    o = make_org(su, app)
    name = f"C{uuid.uuid4().hex[:6]}"
    caller_in_state(app, o, name, target)
    actor, role = ACTOR_FOR[event]
    if event in CALLER_ALLOWED[target]:
        insert_approval(app, o, name, "1", "sv", event, o.u[actor], role)
    else:
        fails(lambda: insert_approval(app, o, name, "1", "sv", event, o.u[actor], role))


def test_the_proposer_cannot_approve_and_roles_are_separate(su, app):
    o = make_org(su, app)
    insert_approval(app, o, "N", "1", "sv", "proposed", o.u["adm1"], "Administrator")
    fails(lambda: insert_approval(app, o, "N", "1", "sv", "approved", o.u["adm1"], "Administrator"))
    fails(lambda: insert_approval(app, o, "N", "1", "sv", "approved", o.u["lab1"], "Approver"))
    both = add_user(su, o, ["Administrator", "Approver"])
    insert_approval(app, o, "M", "1", "sv", "proposed", both, "Administrator")
    fails(lambda: insert_approval(app, o, "M", "1", "sv", "approved", both, "Approver"))
    insert_approval(app, o, "M", "1", "sv", "approved", o.u["app2"], "Approver")


def test_approvers_cannot_propose_and_administrators_cannot_retire(su, app):
    o = make_org(su, app)
    fails(lambda: insert_approval(app, o, "N", "1", "sv", "proposed", o.u["app1"], "Approver"))
    approve_caller(app, o, "N", "1", "sv")
    fails(lambda: insert_approval(app, o, "N", "1", "sv", "retired", o.u["adm1"], "Administrator"))


@pytest.mark.parametrize(
    "name,version,basis",
    [("", "1", "b"), (" x", "1", "b"), ("x", "", "b"), ("x", "1 ", "b"), ("x", "1", ""), ("x", "1", "  ")],
)
def test_caller_approval_fields_are_validated(su, app, name, version, basis):
    o = make_org(su, app)
    fails(lambda: insert_approval(app, o, name, version, "sv", "proposed", o.u["adm1"], "Administrator", basis))


# ─── 8. duplicates ───────────────────────────────────────────────────────────


def test_an_exact_replay_on_the_same_run_is_refused_by_the_index(su, app):
    o = make_org(su, app)
    data = uuid.uuid4().bytes
    submit(app, o, payload=data)
    fails(lambda: submit(app, o, payload=data))


@pytest.mark.parametrize(
    "first",
    ["pending_validation", "validated", "rejected", "withdrawn", "retracted"],
)
def test_the_same_bytes_on_another_run_are_refused_in_every_state_unless_retracted_for_wrong_run_placement(
    su, app, first
):
    """The final review's rule: any earlier submission of the same bytes on another run blocks a new one, whatever its
    state, unless it was RETRACTED with reason_code wrong_run_placement (a plain retraction does not count)."""
    o = make_org(su, app)
    data = uuid.uuid4().bytes
    s = submit(app, o, payload=data)
    if first == "validated":
        validate(app, o, s)
    elif first == "rejected":
        ev(app, o, s, "rejected", "int2", "Interpreter", reason="wrong run")
    elif first == "withdrawn":
        ev(app, o, s, "withdrawn", "lab1", "Lab technician", reason="wrong run")
    elif first == "retracted":
        validate(app, o, s)
        ev(app, o, s, "retracted", "app1", "Approver", reason="wrong run")
    fails(lambda: submit(app, o, run=o.run2, payload=data))


def test_a_wrong_run_retraction_is_the_one_thing_that_lets_the_same_bytes_land_on_another_run(su, app):
    o = make_org(su, app)
    data = uuid.uuid4().bytes
    s = submit(app, o, payload=data)
    validate(app, o, s)
    ev(app, o, s, "retracted", "app1", "Approver", reason="wrong run", checks={"reason_code": "wrong_run_placement"})
    fresh = submit(app, o, run=o.run2, payload=data)
    assert fresh != s and state(su, o, fresh) == "pending_validation"
    assert state(su, o, s) == "retracted"


def test_an_unknown_reason_code_is_not_accepted_on_a_retraction(su, app):
    o = make_org(su, app)
    s = submit(app, o)
    validate(app, o, s)
    fails(lambda: ev(app, o, s, "retracted", "app1", "Approver", reason="x", checks={"reason_code": "something_else"}))


def test_duplicates_are_scoped_to_the_organisation(su, app):
    a, b = make_org(su, app), make_org(su, app)
    data = uuid.uuid4().bytes
    submit(app, a, payload=data)
    submit(app, b, payload=data)


def test_a_cross_organisation_run_or_actor_or_approval_cannot_be_referenced(su, app):
    a, b = make_org(su, app), make_org(su, app)
    fails(lambda: submit(app, a, run=b.run))
    s = submit(app, a)
    fails(lambda: ev(app, a, s, "validated", "int2", "Interpreter", approval=b.approval))

    def foreign_actor():
        with ctx(app, b.u["int1"]):
            app.execute(
                "INSERT INTO call_set_events (org_id,call_set_id,event_type,actor_id,actor_role,occurred_at,reason) VALUES (%s,%s,'rejected',%s,'Interpreter',now(),'x')",
                (a.id, s, b.u["int1"]),
            )

    fails(foreign_actor)
    assert state(app, a, s) == "pending_validation"


# ─── 9. immutability and grants (as the REAL login roles) ────────────────────


@pytest.mark.parametrize("table", ["call_sets", "call_set_events", "caller_approvals"])
def test_the_application_can_neither_update_nor_delete_evidence(su, app, table):
    o = make_org(su, app)
    submit(app, o)
    col = {"call_sets": "parser_version", "call_set_events": "reason", "caller_approvals": "basis"}[table]
    fails(
        lambda: app.execute(f"UPDATE {table} SET {col}='x' WHERE org_id=%s", (o.id,)),
        psycopg.errors.InsufficientPrivilege,
    )
    fails(lambda: app.execute(f"DELETE FROM {table} WHERE org_id=%s", (o.id,)), psycopg.errors.InsufficientPrivilege)


def test_even_the_table_owner_cannot_change_content_the_triggers_refuse(su, app):
    o = make_org(su, app)
    s = submit(app, o)
    for sql in (
        "UPDATE call_sets SET source_vcf='\\x00' WHERE id=%s",
        "UPDATE call_sets SET parsed_document='{}'::jsonb WHERE id=%s",
        "UPDATE call_sets SET assembly='GRCh37' WHERE id=%s",
        "UPDATE call_sets SET caller_name='Other' WHERE id=%s",
        "UPDATE call_sets SET created_by=%s WHERE id=%s",
        "UPDATE call_sets SET retention_days=5 WHERE id=%s",
        "UPDATE call_sets SET created_at=now() - interval '1 day' WHERE id=%s",
        "UPDATE call_sets SET caller_approved_at_submission=NOT caller_approved_at_submission WHERE id=%s",
    ):
        params = (o.u["lab2"], s) if "created_by" in sql else (s,)
        fails(lambda sql=sql, params=params: su.execute(sql, params))
    fails(lambda: su.execute("UPDATE call_set_events SET reason='x' WHERE call_set_id=%s", (s,)))
    fails(lambda: su.execute("UPDATE call_set_events SET event_type='validated' WHERE call_set_id=%s", (s,)))
    fails(lambda: su.execute("UPDATE caller_approvals SET basis='x' WHERE org_id=%s", (o.id,)))


def test_tombstoning_is_the_retention_principals_alone(su, app, ret):
    o = make_org(su, app)
    s = make_state(app, o, "rejected")
    with ctx(ret, o.u["adm1"]):
        ret.execute("UPDATE call_sets SET tombstoned_at=now(), tombstoned_by=%s WHERE id=%s", (o.u["adm1"], s))
        ret.execute(
            "UPDATE call_set_events SET tombstoned_at=now(), tombstoned_by=%s WHERE call_set_id=%s", (o.u["adm1"], s)
        )
    for sql, params in (
        ("UPDATE call_sets SET parser_version='x' WHERE id=%s", (s,)),
        ("UPDATE caller_approvals SET basis='x' WHERE org_id=%s", (o.id,)),
        ("UPDATE call_sets SET retention_days=1 WHERE id=%s", (s,)),
        ("DELETE FROM call_sets WHERE id=%s", (s,)),
        ("DELETE FROM call_set_events WHERE call_set_id=%s", (s,)),
    ):
        fails(lambda sql=sql, params=params: ret.execute(sql, params), psycopg.errors.InsufficientPrivilege)
    fails(
        lambda: ret.execute(
            "INSERT INTO call_set_events (org_id,call_set_id,event_type,actor_id,actor_role,occurred_at,reason) VALUES (%s,%s,'withdrawn',%s,'Lab technician',now(),'x')",
            (o.id, s, o.u["lab1"]),
        ),
        psycopg.errors.InsufficientPrivilege,
    )
    t = make_state(app, o, "rejected")
    fails(
        lambda: app.execute("UPDATE call_sets SET tombstoned_at=now(), tombstoned_by=%s WHERE id=%s", (o.u["adm1"], t)),
        psycopg.errors.InsufficientPrivilege,
    )
    assert su.execute("SELECT tombstoned_at IS NOT NULL FROM call_sets WHERE id=%s", (s,)).fetchone()[0]


def test_a_tombstone_is_final_and_pairs_are_complete(su, app, ret):
    o = make_org(su, app)
    s = make_state(app, o, "rejected")
    fails(lambda: su.execute("UPDATE call_sets SET tombstoned_at=now() WHERE id=%s", (s,)))
    with ctx(ret, o.u["adm1"]):
        ret.execute("UPDATE call_sets SET tombstoned_at=now(), tombstoned_by=%s WHERE id=%s", (o.u["adm1"], s))
    fails(lambda: su.execute("UPDATE call_sets SET tombstoned_at=NULL, tombstoned_by=NULL WHERE id=%s", (s,)))


def test_events_cannot_be_written_onto_a_tombstoned_set_and_it_cannot_be_superseded(su, app, ret):
    o = make_org(su, app)
    pending = submit(app, o)
    with ctx(ret, o.u["adm1"]):
        ret.execute("UPDATE call_sets SET tombstoned_at=now(), tombstoned_by=%s WHERE id=%s", (o.u["adm1"], pending))
    fails(lambda: ev(app, o, pending, "rejected", "int2", "Interpreter", reason="late"))
    pred = make_state(app, o, "validated")
    succ = submit(app, o, supersedes=pred)
    with ctx(ret, o.u["adm1"]):
        ret.execute("UPDATE call_sets SET tombstoned_at=now(), tombstoned_by=%s WHERE id=%s", (o.u["adm1"], pred))
    validate(app, o, succ)
    assert state(app, o, succ) == "validated" and state(app, o, pred) == "validated"
    fails(lambda: submit(app, o, supersedes=pred))


@pytest.mark.parametrize("pair", [("tombstoned_at", "tombstoned_by"), ("content_redacted_at", "content_redacted_by")])
def test_a_call_set_cannot_be_inserted_already_tombstoned_or_redacted(su, app, pair):
    o = make_org(su, app)
    sid, data = uuid.uuid4(), uuid.uuid4().bytes

    def attempt():
        with ctx(app, o.u["lab1"]):
            app.execute(
                f"INSERT INTO call_sets (org_id,id,sequencing_run_id,record_class,assembly,caller_name,caller_version,caller_approved_at_submission,vcf_sample_name,source_filename,"
                f"source_vcf,source_content_hash,parser_version,parsed_document,created_at,created_by,retention_days,{pair[0]},{pair[1]}) "
                f"VALUES (%s,%s,%s,'sv','GRCh38','Caller','1.0',false,'S','a.vcf',%s,%s,'p',%s,now(),%s,1825,now(),%s)",
                (o.id, sid, o.run, data, hashlib.sha256(data).hexdigest(), doc(), o.u["lab1"], o.u["lab1"]),
            )
            app.execute(
                "INSERT INTO call_set_events (org_id,call_set_id,event_type,actor_id,actor_role,occurred_at) VALUES (%s,%s,'submitted',%s,'Lab technician',now())",
                (o.id, sid, o.u["lab1"]),
            )

    fails(attempt)
    assert su.execute("SELECT count(*) FROM call_sets WHERE id=%s", (sid,)).fetchone()[0] == 0


# ─── 10. the evidence envelope ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "case",
    [
        "hash",
        "empty",
        "assembly",
        "class",
        "count",
        "records",
        "version",
        "object",
        "filename",
        "retention_null",
        "retention_zero",
    ],
)
def test_envelope_hash_and_snapshot_constraints_refuse_bad_evidence(su, app, case):
    o = make_org(su, app)
    d = uuid.uuid4().bytes
    kw = dict(h=hashlib.sha256(d).hexdigest(), document=doc())
    if case == "hash":
        kw["h"] = hashlib.sha256(b"other").hexdigest()
    if case == "empty":
        d, kw["h"] = b"", hashlib.sha256(b"").hexdigest()
    if case == "assembly":
        kw["document"] = doc(assembly="GRCh37")
    if case == "class":
        kw["document"] = doc(cls="cnv")
    if case == "count":
        kw["document"] = Jsonb(
            {"schema_version": "1", "assembly": "GRCh38", "record_class": "sv", "counts": {"records": 3}, "records": []}
        )
    if case == "records":
        kw["document"] = Jsonb(
            {"schema_version": "1", "assembly": "GRCh38", "record_class": "sv", "counts": {"records": 0}, "records": {}}
        )
    if case == "version":
        kw["document"] = Jsonb(
            {"schema_version": "2", "assembly": "GRCh38", "record_class": "sv", "counts": {"records": 0}, "records": []}
        )
    if case == "object":
        kw["document"] = Jsonb([])
    if case == "filename":
        kw["filename"] = "../etc/passwd"
    if case == "retention_zero":
        kw["retention"] = 0

    def attempt():
        sid = uuid.uuid4()
        with ctx(su, o.u["lab1"]):
            if case == "retention_null":
                su.execute(
                    "INSERT INTO call_sets (org_id,id,sequencing_run_id,record_class,assembly,caller_name,caller_version,caller_approved_at_submission,vcf_sample_name,source_filename,"
                    "source_vcf,source_content_hash,parser_version,parsed_document,created_at,created_by) VALUES (%s,%s,%s,'sv','GRCh38','Caller','1.0',false,'S','a.vcf',%s,%s,'p',%s,now(),%s)",
                    (o.id, sid, o.run, d, kw["h"], doc(), o.u["lab1"]),
                )
            else:
                raw_call_set(su, o, sid, o.u["lab1"], d, **kw)
            # include the submitted event so that ONLY the constraint under test can make this fail
            su.execute(
                "INSERT INTO call_set_events (org_id,call_set_id,event_type,actor_id,actor_role,occurred_at) VALUES (%s,%s,'submitted',%s,'Lab technician',now())",
                (o.id, sid, o.u["lab1"]),
            )

    fails(attempt)


def test_the_envelope_test_has_a_passing_twin(su, app):
    """The same insert with GOOD evidence commits, so the refusals above are about the evidence and nothing else."""
    o = make_org(su, app)
    sid, d = uuid.uuid4(), uuid.uuid4().bytes
    with ctx(su, o.u["lab1"]):
        raw_call_set(su, o, sid, o.u["lab1"], d)
        su.execute(
            "INSERT INTO call_set_events (org_id,call_set_id,event_type,actor_id,actor_role,occurred_at) VALUES (%s,%s,'submitted',%s,'Lab technician',now())",
            (o.id, sid, o.u["lab1"]),
        )
    assert state(su, o, sid) == "pending_validation"


def test_the_check_constraints_hold_on_their_own_even_with_every_trigger_disabled(su, app):
    """Layering: CHECKs are independent of the triggers. (Disabling triggers needs the table owner, which the
    application is not; this is run as the owner purely to prove the constraints are not trigger-dependent.)"""
    o = make_org(su, app)
    s = submit(app, o)
    su.execute("ALTER TABLE public.call_sets DISABLE TRIGGER USER")
    try:
        fails(lambda: su.execute("UPDATE call_sets SET source_vcf=NULL WHERE id=%s", (s,)))  # content without redaction
        fails(lambda: su.execute("UPDATE call_sets SET parsed_document=NULL WHERE id=%s", (s,)))
        fails(
            lambda: su.execute(
                "UPDATE call_sets SET content_redacted_at=now(), content_redacted_by=%s WHERE id=%s", (o.u["adm1"], s)
            )
        )  # redacted but content present / not tombstoned
        fails(
            lambda: su.execute(
                "UPDATE call_sets SET source_vcf=NULL, parsed_document=NULL, content_redacted_at=now(), content_redacted_by=%s WHERE id=%s",
                (o.u["adm1"], s),
            )
        )  # not tombstoned
        fails(lambda: su.execute("UPDATE call_sets SET retention_days=NULL WHERE id=%s", (s,)))
        fails(lambda: su.execute("UPDATE call_sets SET retention_days=0 WHERE id=%s", (s,)))
        fails(lambda: su.execute("UPDATE call_sets SET tombstoned_at=now() WHERE id=%s", (s,)))
    finally:
        su.execute("ALTER TABLE public.call_sets ENABLE TRIGGER USER")


# ─── 11. races ───────────────────────────────────────────────────────────────


def _race(dsn, jobs):
    outcomes, barrier = [], threading.Barrier(len(jobs))

    def run(job):
        conn = connect(dsn, "clinical_app")
        try:
            barrier.wait()
            job(conn)
            outcomes.append(True)
        except psycopg.Error:
            outcomes.append(False)
        finally:
            conn.close()

    ts = [threading.Thread(target=run, args=(j,)) for j in jobs]
    [t.start() for t in ts]
    [t.join() for t in ts]
    return outcomes


def test_concurrent_decisions_on_one_pending_set_yield_exactly_one_winner(su, app, dsn):
    o = make_org(su, app)
    for _ in range(5):
        s = submit(app, o)
        outcomes = _race(
            dsn,
            [
                lambda c: validate(c, o, s, "int1"),
                lambda c: ev(c, o, s, "rejected", "int2", "Interpreter", reason="race"),
            ],
        )
        assert sorted(outcomes) == [False, True], outcomes
        assert (
            app.execute(
                "SELECT count(*) FROM call_set_events WHERE call_set_id=%s AND event_type IN ('validated','rejected','withdrawn')",
                (s,),
            ).fetchone()[0]
            == 1
        )


def test_two_successors_racing_for_one_predecessor_yield_exactly_one(su, app, dsn):
    o = make_org(su, app)
    pred = make_state(app, o, "validated")
    assert sorted(_race(dsn, [lambda c: submit(c, o, supersedes=pred), lambda c: submit(c, o, supersedes=pred)])) == [
        False,
        True,
    ]


def test_a_successor_validation_racing_the_predecessors_retraction_never_leaves_an_inconsistent_state(su, app, dsn):
    for _ in range(4):
        o = make_org(su, app)
        pred = make_state(app, o, "validated")
        succ = submit(app, o, supersedes=pred)
        _race(
            dsn,
            [
                lambda c: validate(c, o, succ, "int1"),
                lambda c: ev(c, o, pred, "retracted", "app1", "Approver", reason="race"),
            ],
        )
        p, s = state(app, o, pred), state(app, o, succ)
        assert (p, s) in {
            ("superseded", "validated"),
            ("retracted", "validated"),
            ("retracted", "pending_validation"),
        }, (p, s)
        assert (
            app.execute(
                "SELECT count(*) FROM call_set_events WHERE call_set_id=%s AND event_type IN ('superseded','retracted')",
                (pred,),
            ).fetchone()[0]
            == 1
        )


def test_two_retraction_requests_racing_yield_one_open_request(su, app, dsn):
    o = make_org(su, app)
    s = make_state(app, o, "validated")
    assert sorted(
        _race(
            dsn,
            [
                lambda c: ev(c, o, s, "retraction_requested", "int1", "Interpreter", reason="a"),
                lambda c: ev(c, o, s, "retraction_requested", "int2", "Interpreter", reason="b"),
            ],
        )
    ) == [False, True]


def test_a_decline_racing_a_retract_never_leaves_a_contradiction(su, app, dsn):
    o = make_org(su, app)
    s = make_state(app, o, "validated")
    req = ev(app, o, s, "retraction_requested", "int1", "Interpreter", reason="r")
    _race(
        dsn,
        [
            lambda c: ev(c, o, s, "retraction_declined", "app1", "Approver", reason="no", responds=req),
            lambda c: ev(c, o, s, "retracted", "app2", "Approver", reason="yes", responds=req),
        ],
    )
    assert state(app, o, s) in ("validated", "retracted")
    assert (
        app.execute(
            "SELECT count(*) FROM call_set_events WHERE call_set_id=%s AND event_type IN ('retraction_declined','retracted')",
            (s,),
        ).fetchone()[0]
        >= 1
    )


def test_a_validation_racing_a_caller_retirement_is_all_or_nothing(su, app, dsn):
    for _ in range(4):
        o = make_org(su, app)
        s = submit(app, o)
        _race(
            dsn,
            [
                lambda c: validate(c, o, s, "int1"),
                lambda c: insert_approval(c, o, "Caller", "1.0", "sv", "retired", o.u["app1"], "Approver", "race"),
            ],
        )
        latest = app.execute(
            "SELECT event_type FROM caller_approvals WHERE org_id=%s AND caller_name='Caller' ORDER BY seq DESC LIMIT 1",
            (o.id,),
        ).fetchone()[0]
        assert state(app, o, s) in ("validated", "pending_validation") and latest == "retired"


def test_two_submissions_of_the_same_bytes_to_different_runs_yield_exactly_one(su, app, dsn):
    o = make_org(su, app)
    data = uuid.uuid4().bytes
    assert sorted(
        _race(dsn, [lambda c: submit(c, o, payload=data, run=o.run), lambda c: submit(c, o, payload=data, run=o.run2)])
    ) == [False, True]


def test_two_submissions_of_the_same_bytes_to_the_same_run_yield_exactly_one(su, app, dsn):
    o = make_org(su, app)
    data = uuid.uuid4().bytes
    assert sorted(_race(dsn, [lambda c: submit(c, o, payload=data), lambda c: submit(c, o, payload=data)])) == [
        False,
        True,
    ]


# ═══ B-1: SEARCH_PATH / TEMP ═══════════════════════════════════════════════════


def _fn_sigs(su):
    return [
        r[0]
        for r in su.execute(
            "SELECT p.oid::regprocedure::text FROM pg_proc p WHERE p.pronamespace='public'::regnamespace AND p.proname = ANY(%s)",
            (list(FUNCS),),
        ).fetchall()
    ]


def test_the_effective_temp_privilege_is_gone_for_every_route_to_it(su, dsn):
    db = su.execute("SELECT current_database()").fetchone()[0]
    su.execute("DROP ROLE IF EXISTS design_member")
    su.execute("CREATE ROLE design_member NOLOGIN")
    su.execute("GRANT clinical_app TO design_member")
    try:
        for role in ("clinical_app", "clinical_retention", "design_member"):
            assert su.execute("SELECT has_database_privilege(%s, %s, 'TEMP')", (role, db)).fetchone()[0] is False, role
        acl = su.execute("SELECT datacl::text FROM pg_database WHERE datname=%s", (db,)).fetchone()[0]
        assert "=T" not in acl and "=c/" in acl, acl
        assert (
            su.execute(
                "SELECT count(*) FROM pg_auth_members WHERE member=(SELECT oid FROM pg_roles WHERE rolname='clinical_app')"
            ).fetchone()[0]
            == 0
        )
        for role in ("clinical_app", "clinical_retention"):
            with connect(dsn, role) as c:
                fails(lambda c=c: c.execute("CREATE TEMP TABLE x (i int)"), psycopg.errors.InsufficientPrivilege)
    finally:
        su.execute("DROP ROLE design_member")


def test_the_temp_check_can_itself_fail(su, dsn):
    """Control: put the default PUBLIC grant back and the same check must see the privilege."""
    db = su.execute("SELECT current_database()").fetchone()[0]
    su.execute(f'GRANT TEMPORARY ON DATABASE "{db}" TO PUBLIC')
    try:
        assert su.execute("SELECT has_database_privilege('clinical_app', %s, 'TEMP')", (db,)).fetchone()[0] is True
        with connect(dsn, "clinical_app") as c:
            c.execute("CREATE TEMP TABLE control_t (i int)")
    finally:
        su.execute(f'REVOKE TEMPORARY ON DATABASE "{db}" FROM PUBLIC')
    assert su.execute("SELECT has_database_privilege('clinical_app', %s, 'TEMP')", (db,)).fetchone()[0] is False


def test_every_function_pins_search_path_with_pg_temp_last_and_only_one_is_a_definer(su):
    rows = su.execute(
        "SELECT proname, proconfig, prosecdef FROM pg_proc WHERE pronamespace='public'::regnamespace AND proname = ANY(%s)",
        (list(FUNCS),),
    ).fetchall()
    assert {r[0] for r in rows} == set(FUNCS), {r[0] for r in rows} ^ set(FUNCS)
    for name, config, definer in rows:
        assert config and any(c == "search_path=pg_catalog, public, pg_temp" for c in config), (name, config)
        assert definer is (name == "call_sets_require_redaction_audit"), name


def test_no_function_body_names_a_sensitive_table_or_helper_unqualified(su):
    for sig in _fn_sigs(su):
        src = su.execute("SELECT pg_get_functiondef(%s::regprocedure)", (sig,)).fetchone()[0]
        body = src.split("$function$")[1] if "$function$" in src else src
        body = re.sub(r"'(?:[^']|'')*'", "''", body)
        body = re.sub(r"--[^\n]*", "", body)
        for t in TABLES:
            for m in re.finditer(rf"(?<![\w.]){t}(?![\w])", body):
                pytest.fail(f"{sig}: unqualified reference to {t}: ...{body[max(0, m.start() - 30) : m.end() + 20]!r}")
        for f in ("call_set_state", "caller_state", "actor_holds_role", "actor_context", "open_retraction_request"):
            for m in re.finditer(rf"(?<![\w.]){f}\(", body):
                pytest.fail(f"{sig}: unqualified call to {f}")


def _attack_no_lab_role(dsn, o, su):
    b = connect(dsn, "clinical_app")
    try:
        b.execute("CREATE TEMP TABLE role_assignments (org_id uuid, user_id uuid, role text, revoked_at timestamptz)")
        b.execute("INSERT INTO pg_temp.role_assignments VALUES (%s,%s,'Lab technician',NULL)", (o.id, o.u["norole"]))
        submit(b, o, lab="norole")
        return True
    except psycopg.Error:
        return False
    finally:
        b.close()


def _attack_self_validation(dsn, o, su):
    b = connect(dsn, "clinical_app")
    try:
        s = submit(b, o, lab="dual")
        b.execute("CREATE TEMP TABLE call_sets (LIKE public.call_sets INCLUDING DEFAULTS)")
        b.execute("INSERT INTO pg_temp.call_sets SELECT * FROM public.call_sets WHERE id = %s", (s,))
        b.execute("UPDATE pg_temp.call_sets SET created_by = %s WHERE id = %s", (o.u["int1"], s))
        ev(b, o, s, "validated", "dual", "Interpreter")
        return True
    except psycopg.Error:
        return False
    finally:
        b.close()


def _attack_fake_caller_approval(dsn, o, su):
    b = connect(dsn, "clinical_app")
    try:
        s = submit(b, o, caller=("Rogue", "9"))
        b.execute("CREATE TEMP TABLE caller_approvals (LIKE public.caller_approvals INCLUDING DEFAULTS)")
        b.execute(
            "INSERT INTO pg_temp.caller_approvals (org_id,id,seq,caller_name,caller_version,record_class,event_type,actor_id,actor_role,occurred_at,basis) "
            "VALUES (%s,%s,999999,'Rogue','9','sv','approved',%s,'Approver',now(),'forged')",
            (o.id, o.approval, o.u["app1"]),
        )
        ev(b, o, s, "validated", "int1", "Interpreter", approval=o.approval)
        return True
    except psycopg.Error:
        return False
    finally:
        b.close()


def _attack_skip_vcf_inheritance(dsn, o, su):
    run = add_run(su, o)
    su.execute(
        "INSERT INTO vcfs (org_id,id,sequencing_run_id,vcf_path,content_hash,created_at,created_by,retention_days) VALUES (%s,%s,%s,'/tmp/x.vcf',%s,now(),%s,3650)",
        (o.id, uuid.uuid4(), run, "0" * 64, o.u["lab1"]),
    )
    b = connect(dsn, "clinical_app")
    try:
        b.execute("CREATE TEMP TABLE vcfs (LIKE public.vcfs INCLUDING DEFAULTS)")
        submit(b, o, run=run, retention=1825)
        return True
    except psycopg.Error:
        return False
    finally:
        b.close()


ATTACKS = {
    "no_lab_role": _attack_no_lab_role,
    "self_validation": _attack_self_validation,
    "fake_caller_approval": _attack_fake_caller_approval,
    "skip_vcf_inheritance": _attack_skip_vcf_inheritance,
}


@pytest.mark.parametrize("attack", list(ATTACKS))
def test_repository_schema_resists_temp_table_shadowing(dsn, attack):
    """The variant matrix is skipped in repo mode; this is its 'final' row, run against schema.sql's own objects."""
    su_r, app_r = connect(dsn), connect(dsn, "clinical_app")
    try:
        o = make_org(su_r, app_r)
        assert ATTACKS[attack](dsn, o, su_r) is False, f"{attack} succeeded against the repository schema"
    finally:
        su_r.close()
        app_r.close()


# ═══ H-1: actor context ═══════════════════════════════════════════════════════


def test_the_actor_context_must_be_present_and_match(su, app):
    o = make_org(su, app)
    s = submit(app, o)
    fails(
        lambda: app.execute(
            "INSERT INTO call_set_events (org_id,call_set_id,event_type,actor_id,actor_role,occurred_at,reason) VALUES (%s,%s,'rejected',%s,'Interpreter',now(),'r')",
            (o.id, s, o.u["int1"]),
        )
    )
    fails(
        lambda: app.execute(
            "INSERT INTO caller_approvals (org_id,caller_name,caller_version,record_class,event_type,actor_id,actor_role,occurred_at,basis) VALUES (%s,'Z','1','sv','proposed',%s,'Administrator',now(),'b')",
            (o.id, o.u["adm1"]),
        )
    )

    def no_ctx_submit():
        with app.transaction():
            raw_call_set(app, o, uuid.uuid4(), o.u["lab1"], uuid.uuid4().bytes)

    fails(no_ctx_submit)

    def mismatched():
        with ctx(app, o.u["int2"]):
            app.execute(
                "INSERT INTO call_set_events (org_id,call_set_id,event_type,actor_id,actor_role,occurred_at,reason) VALUES (%s,%s,'rejected',%s,'Interpreter',now(),'r')",
                (o.id, s, o.u["int1"]),
            )

    fails(mismatched)
    assert state(app, o, s) == "pending_validation"


def test_the_actor_context_is_not_a_security_boundary(su, app):
    """A malicious application role can set the context to anything and record any actor who holds the role."""
    o = make_org(su, app)
    s = submit(app, o, lab="lab1")
    with ctx(app, o.u["int2"]):
        app.execute(
            "INSERT INTO call_set_events (org_id,call_set_id,event_type,actor_id,actor_role,occurred_at,checks,caller_approval_id) "
            "VALUES (%s,%s,'validated',%s,'Interpreter',now(),%s,%s)",
            (o.id, s, o.u["int2"], Jsonb(GOOD_CHECKS), o.approval),
        )
    assert state(app, o, s) == "validated"  # the database cannot know int2 did not do this
    with app.transaction():  # and the application can grant itself a role (existing design)
        app.execute(
            "INSERT INTO role_assignments (user_id,org_id,role,assigned_by,assigned_at) VALUES (%s,%s,'Interpreter',%s,now())",
            (o.u["norole"], o.id, o.u["norole"]),
        )
    assert (
        su.execute("SELECT public.actor_holds_role(%s,%s,'Interpreter')", (o.id, o.u["norole"])).fetchone()[0] is True
    )


# ═══ H-3: retention snapshot ══════════════════════════════════════════════════


def add_vcf(su, o, run, days, tombstoned=False):
    vid = uuid.uuid4()
    su.execute(
        "INSERT INTO vcfs (org_id,id,sequencing_run_id,vcf_path,content_hash,created_at,created_by,retention_days) VALUES (%s,%s,%s,'/tmp/x.vcf',%s,now(),%s,%s)",
        (o.id, vid, run, "0" * 64, o.u["lab1"], days),
    )
    if tombstoned:
        su.execute("UPDATE vcfs SET tombstoned_at=now(), tombstoned_by=%s WHERE id=%s", (o.u["adm1"], vid))
    return vid


def test_the_retention_snapshot_inherits_the_runs_vcf_value(su, app):
    o = make_org(su, app)
    run = add_run(su, o)
    add_vcf(su, o, run, 3650)
    fails(lambda: submit(app, o, run=run, retention=1825))
    s = submit(app, o, run=run, retention=3650)
    assert app.execute("SELECT retention_days FROM call_sets WHERE id=%s", (s,)).fetchone()[0] == 3650


def test_with_several_vcfs_the_longest_value_is_inherited_and_tombstoned_vcfs_are_ignored(su, app):
    o = make_org(su, app)
    run = add_run(su, o)
    add_vcf(su, o, run, 1825)
    add_vcf(su, o, run, 3650)
    add_vcf(su, o, run, 9999, tombstoned=True)
    fails(lambda: submit(app, o, run=run, retention=1825))
    fails(lambda: submit(app, o, run=run, retention=9999))
    submit(app, o, run=run, retention=3650)


def test_without_a_vcf_the_dao_resolved_positive_value_is_accepted(su, app):
    o = make_org(su, app)
    s = submit(app, o, retention=1825)
    assert app.execute("SELECT retention_days FROM call_sets WHERE id=%s", (s,)).fetchone()[0] == 1825


def test_the_retention_snapshot_is_immutable_for_everyone(su, app, ret):
    o = make_org(su, app)
    s = submit(app, o)
    fails(
        lambda: app.execute("UPDATE call_sets SET retention_days=1 WHERE id=%s", (s,)),
        psycopg.errors.InsufficientPrivilege,
    )
    fails(
        lambda: ret.execute("UPDATE call_sets SET retention_days=1 WHERE id=%s", (s,)),
        psycopg.errors.InsufficientPrivilege,
    )
    fails(lambda: su.execute("UPDATE call_sets SET retention_days=1 WHERE id=%s", (s,)))
    assert su.execute("SELECT retention_days FROM call_sets WHERE id=%s", (s,)).fetchone()[0] == 1825


# ═══ H-2 / B-2: purge eligibility, redaction, rollback ═══════════════════════


NOW = datetime(2040, 1, 1, tzinfo=timezone.utc)

ELIGIBLE_SQL = """
SELECT cs.id FROM call_sets cs
 WHERE cs.org_id = %(org)s AND cs.tombstoned_at IS NULL AND cs.retention_days IS NOT NULL
   AND (SELECT MAX(re.released_at) FROM vcfs v
          JOIN interpretations i ON i.org_id = v.org_id AND i.vcf_id = v.id
          JOIN reports r ON r.org_id = i.org_id AND r.interpretation_id = i.id
          JOIN release_events re ON re.org_id = r.org_id AND re.report_id = r.id
         WHERE v.org_id = cs.org_id AND v.sequencing_run_id = cs.sequencing_run_id) IS NOT NULL
   AND (SELECT MAX(re.released_at) FROM vcfs v
          JOIN interpretations i ON i.org_id = v.org_id AND i.vcf_id = v.id
          JOIN reports r ON r.org_id = i.org_id AND r.interpretation_id = i.id
          JOIN release_events re ON re.org_id = r.org_id AND re.report_id = r.id
         WHERE v.org_id = cs.org_id AND v.sequencing_run_id = cs.sequencing_run_id) + (cs.retention_days * INTERVAL '1 day') <= %(now)s
   AND NOT EXISTS (SELECT 1 FROM call_sets s WHERE s.org_id = cs.org_id AND s.supersedes_id = cs.id AND s.tombstoned_at IS NULL)
"""


def release_on_run(su, o, run, released_at, vcf_days=1825):
    vcf = add_vcf(su, o, run, vcf_days)
    interp = su.execute(
        "INSERT INTO interpretations (org_id,vcf_id,run_document,submission_key,created_at,created_by) VALUES (%s,%s,'{}'::jsonb,%s,now(),%s) RETURNING id",
        (o.id, vcf, uuid.uuid4().hex, o.u["lab1"]),
    ).fetchone()[0]
    report = su.execute(
        "INSERT INTO reports (org_id,interpretation_id,created_at,created_by) VALUES (%s,%s,now(),%s) RETURNING id",
        (o.id, interp, o.u["lab1"]),
    ).fetchone()[0]
    su.execute(
        "INSERT INTO release_events (org_id,report_id,consumer,released_at,released_by,content_hash) VALUES (%s,%s,'clinician',%s,%s,%s)",
        (o.id, report, released_at, o.u["lab1"], "1" * 64),
    )


def eligible(conn, o, now=NOW):
    return {r[0] for r in conn.execute(ELIGIBLE_SQL, {"org": o.id, "now": now}).fetchall()}


def purge(
    ret,
    o,
    ids,
    *,
    actor,
    fail_at=None,
    audit=True,
    audit_in_savepoint=False,
    actor_context=None,
    now=None,
    audit_resource=None,
):
    """The REQUIRED SHAPE of the dedicated call-set purge: one transaction, no internal commit.
    (Test harness only; the real code would live in RetentionPrincipal._purge_call_sets.)"""
    now = now or datetime(2040, 1, 1, tzinfo=timezone.utc)
    with ret.transaction():
        ret.execute("SELECT set_config('clinical.actor_id', %s, true)", (str(actor_context or actor),))
        for i, sid in enumerate(ids):
            ret.execute(
                "UPDATE call_sets SET tombstoned_at=%s, tombstoned_by=%s, source_vcf=NULL, parsed_document=NULL, content_redacted_at=%s, content_redacted_by=%s WHERE org_id=%s AND id=%s",
                (now, actor, now, actor, o.id, sid),
            )
            if fail_at == ("redact", i):
                raise RuntimeError("injected failure after redaction")
            ret.execute(
                "UPDATE call_set_events SET tombstoned_at=%s, tombstoned_by=%s WHERE org_id=%s AND call_set_id=%s",
                (now, actor, o.id, sid),
            )
            if fail_at == ("events", i):
                raise RuntimeError("injected failure after events cascade")
            if audit:
                sql = (
                    'INSERT INTO audit_log (org_id,user_id,"timestamp",actor_role,action,resource_type,resource_id,outcome,details,ip_address) '
                    "VALUES (%s,%s,%s,'System','retention_purge','call_set',%s,'success',%s,NULL)"
                )
                params = (o.id, actor, now, str(audit_resource or sid), json.dumps({"artefact_class": "call_set"}))
                if audit_in_savepoint:
                    with ret.transaction():
                        ret.execute(sql, params)
                else:
                    ret.execute(sql, params)
            if fail_at == ("audit", i):
                raise RuntimeError("injected failure after audit")


def test_purge_eligibility_follows_release_plus_the_snapshot_and_never_without_a_release(su, app, ret):
    o = make_org(su, app)
    run = add_run(su, o)
    s = submit(app, o, run=run, retention=1825)
    assert s not in eligible(ret, o), "no VCF and no release: nothing anchors the clock"
    release_on_run(su, o, run, datetime(2030, 1, 1, tzinfo=timezone.utc), 1825)
    assert s not in eligible(ret, o, datetime(2034, 12, 30, tzinfo=timezone.utc)), "not yet 1825 days after release"
    assert s in eligible(ret, o, datetime(2035, 1, 2, tzinfo=timezone.utc))


def test_a_run_without_a_vcf_is_never_eligible_and_a_run_with_a_vcf_but_no_release_is_not_either(su, app, ret):
    o = make_org(su, app)
    bare = submit(app, o, run=o.run, retention=1825)
    run_v = add_run(su, o)
    add_vcf(su, o, run_v, 1825)
    unreleased = submit(app, o, run=run_v, retention=1825)
    assert not ({bare, unreleased} & eligible(ret, o, datetime(2100, 1, 1, tzinfo=timezone.utc)))


def test_a_live_successor_blocks_purge_until_it_is_itself_purged(su, app, ret):
    o = make_org(su, app)
    run = add_run(su, o)
    pred = make_state(app, o, "validated", run=run, retention=1825)
    succ = submit(app, o, supersedes=pred, run=run, retention=1825)
    release_on_run(su, o, run, datetime(2030, 1, 1, tzinfo=timezone.utc), 1825)
    assert pred not in eligible(ret, o) and succ in eligible(ret, o)
    purge(ret, o, [succ], actor=o.u["adm1"])
    assert pred in eligible(ret, o)


def test_an_already_tombstoned_set_is_not_selected_again(su, app, ret):
    o = make_org(su, app)
    run = add_run(su, o)
    s = submit(app, o, run=run, retention=1825)
    release_on_run(su, o, run, datetime(2030, 1, 1, tzinfo=timezone.utc), 1825)
    assert s in eligible(ret, o)
    purge(ret, o, [s], actor=o.u["adm1"])
    assert s not in eligible(ret, o)


def seeded(su, app):
    o = make_org(su, app)
    s = make_state(app, o, "validated", marker=True)
    ev(app, o, s, "retraction_requested", "int1", "Interpreter", reason="possible swap")
    return o, s


def test_redaction_removes_both_content_columns_atomically_and_keeps_lifecycle_and_audit_metadata(su, app, ret):
    o, s = seeded(su, app)
    before_events = su.execute(
        "SELECT id, event_type, actor_id, occurred_at FROM call_set_events WHERE call_set_id=%s ORDER BY seq", (s,)
    ).fetchall()
    h = su.execute("SELECT source_content_hash FROM call_sets WHERE id=%s", (s,)).fetchone()[0]
    assert MARK in su.execute("SELECT source_vcf FROM call_sets WHERE id=%s", (s,)).fetchone()[0]
    purge(ret, o, [s], actor=o.u["adm1"])
    row = su.execute(
        "SELECT source_vcf, parsed_document, content_redacted_at, content_redacted_by, tombstoned_at, tombstoned_by, source_content_hash, parser_version, retention_days FROM call_sets WHERE id=%s",
        (s,),
    ).fetchone()
    assert row[0] is None and row[1] is None
    assert row[2] is not None and row[3] == o.u["adm1"] and row[4] is not None and row[5] == o.u["adm1"]
    assert row[6].strip() == h.strip() and row[7] == "p1" and row[8] == 1825
    rendered = su.execute("SELECT t::text FROM call_sets t WHERE id=%s", (s,)).fetchone()[0]
    assert MARK.decode() not in rendered and MARK.hex() not in rendered, "no genomic content may remain in any column"
    after = su.execute(
        "SELECT id, event_type, actor_id, occurred_at, tombstoned_at IS NOT NULL FROM call_set_events WHERE call_set_id=%s ORDER BY seq",
        (s,),
    ).fetchall()
    assert [a[:4] for a in after] == before_events and all(a[4] for a in after)
    assert state(su, o, s) == "validated"
    a = su.execute(
        "SELECT action, resource_type, resource_id, outcome, actor_role, details::text FROM audit_log WHERE org_id=%s AND resource_id=%s",
        (o.id, str(s)),
    ).fetchall()
    assert a == [("retention_purge", "call_set", str(s), "success", "System", '{"artefact_class": "call_set"}')]
    fails(
        lambda: ret.execute("UPDATE audit_log SET outcome='x' WHERE resource_id=%s", (str(s),)),
        psycopg.errors.InsufficientPrivilege,
    )
    fails(
        lambda: ret.execute("DELETE FROM audit_log WHERE resource_id=%s", (str(s),)),
        psycopg.errors.InsufficientPrivilege,
    )


def test_after_redaction_the_application_reads_nulls_and_nobody_can_restore_or_alter(su, app, ret):
    o, s = seeded(su, app)
    purge(ret, o, [s], actor=o.u["adm1"])
    assert app.execute(
        "SELECT source_vcf IS NULL, parsed_document IS NULL, octet_length(source_vcf), source_content_hash IS NOT NULL FROM call_sets WHERE id=%s",
        (s,),
    ).fetchone() == (True, True, None, True)
    sqls = (
        "UPDATE call_sets SET source_vcf='\\x41' WHERE id=%s",
        "UPDATE call_sets SET parsed_document='{}'::jsonb WHERE id=%s",
        "UPDATE call_sets SET content_redacted_at=NULL, content_redacted_by=NULL WHERE id=%s",
        "UPDATE call_sets SET tombstoned_at=NULL, tombstoned_by=NULL WHERE id=%s",
    )
    for sql in sqls:
        fails(lambda sql=sql: app.execute(sql, (s,)), psycopg.errors.InsufficientPrivilege)
    for sql in sqls:

        def retention_attempt(sql=sql):
            with ctx(ret, o.u["adm1"]):
                ret.execute(sql, (s,))

        fails(retention_attempt)
    fails(lambda: su.execute("UPDATE call_sets SET source_vcf='\\x41' WHERE id=%s", (s,)))
    fails(lambda: su.execute("UPDATE call_sets SET retention_days=1 WHERE id=%s", (s,)))
    fails(lambda: ev(app, o, s, "retracted", "app1", "Approver", reason="late"))


def test_redaction_must_be_complete_and_only_of_a_tombstoned_record(su, app, ret):
    o, s = seeded(su, app)
    cases = {
        "bytes only": "UPDATE call_sets SET source_vcf=NULL, content_redacted_at=%(n)s, content_redacted_by=%(u)s, tombstoned_at=%(n)s, tombstoned_by=%(u)s WHERE id=%(s)s",
        "document only": "UPDATE call_sets SET parsed_document=NULL, content_redacted_at=%(n)s, content_redacted_by=%(u)s, tombstoned_at=%(n)s, tombstoned_by=%(u)s WHERE id=%(s)s",
        "not tombstoned": "UPDATE call_sets SET source_vcf=NULL, parsed_document=NULL, content_redacted_at=%(n)s, content_redacted_by=%(u)s WHERE id=%(s)s",
        "no redactor": "UPDATE call_sets SET source_vcf=NULL, parsed_document=NULL, content_redacted_at=%(n)s, tombstoned_at=%(n)s, tombstoned_by=%(u)s WHERE id=%(s)s",
        "replaced, not removed": "UPDATE call_sets SET source_vcf='\\x00', parsed_document='{}'::jsonb, content_redacted_at=%(n)s, content_redacted_by=%(u)s, tombstoned_at=%(n)s, tombstoned_by=%(u)s WHERE id=%(s)s",
    }
    for name, sql in cases.items():

        def attempt(sql=sql):
            with ctx(ret, o.u["adm1"]):
                ret.execute(sql, {"n": NOW, "u": o.u["adm1"], "s": s})

        fails(attempt)
    assert su.execute(
        "SELECT source_vcf IS NOT NULL AND parsed_document IS NOT NULL AND content_redacted_at IS NULL FROM call_sets WHERE id=%s",
        (s,),
    ).fetchone()[0]


def test_redaction_by_the_application_role_is_refused(su, app):
    o, s = seeded(su, app)
    fails(
        lambda: app.execute(
            "UPDATE call_sets SET source_vcf=NULL, parsed_document=NULL, content_redacted_at=now(), content_redacted_by=%s, tombstoned_at=now(), tombstoned_by=%s WHERE id=%s",
            (o.u["adm1"], o.u["adm1"], s),
        ),
        psycopg.errors.InsufficientPrivilege,
    )


def test_redaction_without_its_audit_row_in_the_same_transaction_is_refused_and_leaves_the_content(su, app, ret):
    o, s = seeded(su, app)
    fails(lambda: purge(ret, o, [s], actor=o.u["adm1"], audit=False))
    assert su.execute(
        "SELECT source_vcf IS NOT NULL AND parsed_document IS NOT NULL AND tombstoned_at IS NULL FROM call_sets WHERE id=%s",
        (s,),
    ).fetchone()[0]
    audit_sql = (
        'INSERT INTO audit_log (org_id,user_id,"timestamp",actor_role,action,resource_type,resource_id,outcome,details) '
        "VALUES (%s,%s,now(),'System','retention_purge','call_set',%s,'success','{}')"
    )
    with ret.transaction():  # committed EARLIER, in another transaction: does not count
        ret.execute(audit_sql, (o.id, o.u["adm1"], str(s)))
    fails(lambda: purge(ret, o, [s], actor=o.u["adm1"], audit=False))
    assert su.execute("SELECT source_vcf IS NOT NULL FROM call_sets WHERE id=%s", (s,)).fetchone()[0]
    o2, s2 = seeded(su, app)  # an audit row for a DIFFERENT record does not count either
    with ret.transaction():
        ret.execute(audit_sql, (o2.id, o2.u["adm1"], str(s)))
    fails(lambda: purge(ret, o2, [s2], actor=o2.u["adm1"], audit=False))
    assert su.execute("SELECT source_vcf IS NOT NULL FROM call_sets WHERE id=%s", (s2,)).fetchone()[0]


def test_an_audit_row_in_the_same_transaction_but_for_another_record_or_action_does_not_count(su, app, ret):
    o, s = seeded(su, app)
    other = uuid.uuid4()
    fails(lambda: purge(ret, o, [s], actor=o.u["adm1"], audit_resource=other))  # same txn, wrong resource id
    assert su.execute("SELECT source_vcf IS NOT NULL FROM call_sets WHERE id=%s", (s,)).fetchone()[0]

    def wrong_action():
        with ret.transaction():
            ret.execute("SELECT set_config('clinical.actor_id', %s, true)", (str(o.u["adm1"]),))
            ret.execute(
                "UPDATE call_sets SET tombstoned_at=now(), tombstoned_by=%s, source_vcf=NULL, parsed_document=NULL, content_redacted_at=now(), content_redacted_by=%s WHERE id=%s",
                (o.u["adm1"], o.u["adm1"], s),
            )
            ret.execute(
                "INSERT INTO audit_log (org_id,user_id,\"timestamp\",actor_role,action,resource_type,resource_id,outcome,details) VALUES (%s,%s,now(),'System','some_other_action','call_set',%s,'success','{}')",
                (o.id, o.u["adm1"], str(s)),
            )

    fails(wrong_action)
    assert su.execute("SELECT source_vcf IS NOT NULL FROM call_sets WHERE id=%s", (s,)).fetchone()[0]


def test_an_audit_row_written_inside_a_savepoint_is_refused_safely_a_documented_caveat(su, app, ret):
    o, s = seeded(su, app)
    fails(lambda: purge(ret, o, [s], actor=o.u["adm1"], audit_in_savepoint=True))
    assert su.execute("SELECT source_vcf IS NOT NULL FROM call_sets WHERE id=%s", (s,)).fetchone()[0], (
        "a refusal must not lose data"
    )


@pytest.mark.parametrize("step", ["redact", "events", "audit"])
def test_a_failed_purge_at_any_step_rolls_back_everything(su, app, ret, step):
    o, s = seeded(su, app)
    fails(lambda: purge(ret, o, [s], actor=o.u["adm1"], fail_at=(step, 0)), RuntimeError)
    assert su.execute(
        "SELECT source_vcf IS NOT NULL, parsed_document IS NOT NULL, tombstoned_at IS NULL, content_redacted_at IS NULL FROM call_sets WHERE id=%s",
        (s,),
    ).fetchone() == (True, True, True, True)
    assert (
        su.execute(
            "SELECT count(*) FROM call_set_events WHERE call_set_id=%s AND tombstoned_at IS NOT NULL", (s,)
        ).fetchone()[0]
        == 0
    )
    assert su.execute("SELECT count(*) FROM audit_log WHERE resource_id=%s", (str(s),)).fetchone()[0] == 0


def test_a_batch_purge_that_fails_on_the_second_set_leaves_the_first_untouched(su, app, ret):
    o = make_org(su, app)
    a, b = make_state(app, o, "validated", marker=True), make_state(app, o, "validated", marker=True)
    fails(lambda: purge(ret, o, [a, b], actor=o.u["adm1"], fail_at=("events", 1)), RuntimeError)
    for s in (a, b):
        assert su.execute(
            "SELECT source_vcf IS NOT NULL AND tombstoned_at IS NULL FROM call_sets WHERE id=%s", (s,)
        ).fetchone()[0]
        assert su.execute("SELECT count(*) FROM audit_log WHERE resource_id=%s", (str(s),)).fetchone()[0] == 0
    purge(ret, o, [a, b], actor=o.u["adm1"])
    assert (
        su.execute(
            "SELECT count(*) FROM call_sets WHERE id = ANY(%s) AND source_vcf IS NULL AND parsed_document IS NULL",
            ([a, b],),
        ).fetchone()[0]
        == 2
    )


def test_redaction_needs_a_matching_actor_context_as_defence_in_depth(su, app, ret):
    o, s = seeded(su, app)
    fails(lambda: purge(ret, o, [s], actor=o.u["adm1"], actor_context=o.u["adm2"]))
    assert su.execute("SELECT source_vcf IS NOT NULL FROM call_sets WHERE id=%s", (s,)).fetchone()[0]


def test_redaction_is_per_record_and_other_records_keep_their_content(su, app, ret):
    o = make_org(su, app)
    a, b = make_state(app, o, "validated", marker=True), make_state(app, o, "validated", marker=True)
    purge(ret, o, [a], actor=o.u["adm1"])
    assert su.execute("SELECT source_vcf IS NULL FROM call_sets WHERE id=%s", (a,)).fetchone()[0]
    assert su.execute(
        "SELECT source_vcf IS NOT NULL AND parsed_document IS NOT NULL FROM call_sets WHERE id=%s", (b,)
    ).fetchone()[0]


def test_a_redacted_record_cannot_be_purged_again_with_different_values(su, app, ret):
    o, s = seeded(su, app)
    purge(ret, o, [s], actor=o.u["adm1"])
    fails(lambda: purge(ret, o, [s], actor=o.u["adm1"], now=datetime(2041, 1, 1, tzinfo=timezone.utc)))
    # an identical rewrite is a no-op at row level (eligibility, not the trigger, is what stops a repeat purge)
    assert su.execute("SELECT content_redacted_at FROM call_sets WHERE id=%s", (s,)).fetchone()[0] == datetime(
        2040, 1, 1, tzinfo=timezone.utc
    )


# ═══ Other unauthorised direct writes ════════════════════════════════════════


def test_unauthorised_direct_writes_as_the_application_role_are_refused(su, app):
    o = make_org(su, app)
    s = submit(app, o)
    fails(lambda: submit(app, o, lab="norole"))
    fails(lambda: submit(app, o, lab="int1"))
    fails(lambda: ev(app, o, s, "superseded", "int1", "Interpreter", related=s))
    fails(
        lambda: app.execute("UPDATE call_sets SET content_redacted_at=now() WHERE id=%s", (s,)),
        psycopg.errors.InsufficientPrivilege,
    )
    fails(lambda: app.execute("TRUNCATE call_set_events"), psycopg.errors.InsufficientPrivilege)
    fails(lambda: app.execute("SET session_replication_role = replica"), psycopg.errors.InsufficientPrivilege)
    fails(lambda: app.execute("ALTER TABLE call_set_events DISABLE TRIGGER ALL"), psycopg.errors.InsufficientPrivilege)
    fails(
        lambda: app.execute("DROP TRIGGER trg_cse_before_insert ON call_set_events"),
        psycopg.errors.InsufficientPrivilege,
    )
    fails(
        lambda: app.execute(
            "CREATE OR REPLACE FUNCTION public.call_set_state(uuid,uuid) RETURNS text LANGUAGE sql AS $$ SELECT 'x'::text $$"
        ),
        psycopg.errors.InsufficientPrivilege,
    )
    fails(
        lambda: app.execute(
            "INSERT INTO caller_approvals (id,org_id,caller_name,caller_version,record_class,event_type,actor_id,actor_role,occurred_at,basis) "
            "SELECT id,org_id,caller_name,caller_version,record_class,event_type,actor_id,actor_role,occurred_at,basis FROM caller_approvals LIMIT 1 ON CONFLICT (id) DO UPDATE SET basis='x'"
        ),
        psycopg.errors.InsufficientPrivilege,
    )
