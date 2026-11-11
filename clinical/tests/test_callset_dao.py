"""
CNV/SV call-set DAO (migration 0010) against the REAL schema, as the REAL `clinical_app` and `clinical_retention`
roles (`SET ROLE`), so a missing grant or a bypassed trigger fails here and not in production.

Three connections are used on purpose: a superuser one that only BUILDS fixtures (chains, releases) and inspects
state; one acting as clinical_app that the DAO under test runs on; one acting as clinical_retention.

Every assertion reads a VALUE (a state, a column, a row's content). Where an absence is asserted it is paired with a
positive twin that the broken state would fail.
"""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from datetime import date, datetime, timezone

import pytest

from clinical.data_access import (
    AuthorizationError,
    BcryptHasher,
    CallSetConflictError,
    CallSetInputError,
    DataAccess,
    NotFoundError,
    SystemClock,
)
from clinical.genome_analysis import CALLSET_PARSER_VERSION, verify_document
from clinical.genome_endpoints import DaoCallSetSource
from clinical.retention import RetentionPrincipal
from clinical.tests.migration_db import require_dsn

pytest.importorskip("psycopg", reason="psycopg not installed")
import psycopg  # noqa: E402

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
FAR = datetime(2060, 1, 1, tzinfo=timezone.utc)
SCHEMA_SQL = None


# ─── infrastructure ──────────────────────────────────────────────────────────


def _connect(dsn: str, role: str | None = None):
    connection = psycopg.connect(dsn, autocommit=False, connect_timeout=10)
    if role:
        with connection.cursor() as cur:
            cur.execute(f"SET ROLE {role}")
        connection.commit()
    return connection


@pytest.fixture()
def su():
    dsn = require_dsn()
    import pathlib

    schema = (pathlib.Path(__file__).parent.parent / "schema.sql").read_text(encoding="utf-8")
    connection = _connect(dsn)
    with connection.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        for role in ("clinical_app", "clinical_retention"):
            cur.execute(f"DO $$ BEGIN CREATE ROLE {role}; EXCEPTION WHEN duplicate_object THEN NULL; END $$;")
        cur.execute(schema)
    connection.commit()
    yield connection
    connection.close()


@pytest.fixture()
def app_conn(su):
    connection = _connect(require_dsn(), "clinical_app")
    yield connection
    connection.close()


@pytest.fixture()
def ret_conn(su):
    connection = _connect(require_dsn(), "clinical_retention")
    yield connection
    connection.close()


@pytest.fixture()
def dao(app_conn):
    return DataAccess(app_conn, clock=SystemClock(), password_hasher=BcryptHasher(rounds=4))


def _fresh_dao():
    """A second, independent clinical_app connection and DAO (for concurrency)."""
    connection = _connect(require_dsn(), "clinical_app")
    return connection, DataAccess(connection, clock=SystemClock(), password_hasher=BcryptHasher(rounds=4))


class World:
    pass


def _mkuser(su, dao, org, email, *roles):
    user_id = dao.create_user(
        org,
        email,
        "password",
        full_name=email.split("@")[0],
        registration_number="MCI-" + email.split("@")[0],
        hospital="H",
    )
    with su.cursor() as cur:
        for role in roles:
            cur.execute(
                "INSERT INTO role_assignments (user_id, org_id, role, assigned_by, assigned_at, basis) VALUES (%s, %s, %s, %s, %s, %s)",
                (user_id, org, role, user_id, T0, "test fixture qualification basis" if role == "Approver" else None),
            )
    su.commit()
    return dao.login(email, org, "password")


def build_org(su, dao, tag="a"):
    w = World()
    w.org = dao.create_organisation(f"Org {tag}")
    mk = lambda name, *roles: _mkuser(su, dao, w.org, f"{name}@{tag}.test", *roles)  # noqa: E731
    w.admin = mk("admin", "Administrator")
    w.admin_approver = mk("adminapprover", "Administrator", "Approver")
    w.lab1 = mk("lab1", "Lab technician")
    w.lab2 = mk("lab2", "Lab technician")
    w.lab_interp = mk("labinterp", "Lab technician", "Interpreter")
    w.interp1 = mk("interp1", "Interpreter")
    w.interp2 = mk("interp2", "Interpreter")
    w.appr1 = mk("appr1", "Approver")
    w.appr2 = mk("appr2", "Approver")
    w.reader = mk("reader", "Orderer")
    w.tests = {}
    return w


def new_run(su, dao, w, assembly="GRCh38", patient=None, vcf_days=None):
    """order -> sample -> run (and optionally a VCF with a given retention) in w's organisation."""
    if assembly not in w.tests:
        test_id = uuid.uuid4()
        with su.cursor() as cur:
            cur.execute(
                "INSERT INTO tests (test_id, org_id, name, assembly, status, created_at) VALUES (%s,%s,%s,%s,'active',%s)",
                (test_id, w.org, f"Panel {assembly}", assembly, T0),
            )
        su.commit()
        w.tests[assembly] = test_id
    patient_id = patient or dao.create_patient(w.reader, "Test", date(1990, 1, 1), "M")
    consent_id = dao.record_consent(w.reader, patient_id, "testing")
    order_id = dao.create_order(
        w.reader,
        patient_id=patient_id,
        test_id=w.tests[assembly],
        consent_id=consent_id,
        required_scope="testing",
        priority="routine",
    )
    sample_id, run_id = uuid.uuid4(), uuid.uuid4()
    with su.cursor() as cur:
        cur.execute(
            "INSERT INTO samples (sample_id, order_id, org_id, type, collected_at, collected_by, qc_status) "
            "VALUES (%s,%s,%s,'blood',%s,%s,'passed')",
            (sample_id, order_id, w.org, T0, w.lab1.user_id),
        )
        cur.execute(
            "INSERT INTO sequencing_runs (org_id, id, sample_id, created_at, created_by) VALUES (%s,%s,%s,%s,%s)",
            (w.org, run_id, sample_id, T0, w.lab1.user_id),
        )
    su.commit()
    run = World()
    run.id, run.sample, run.patient, run.assembly = run_id, str(sample_id), patient_id, assembly
    if vcf_days is not None:
        add_vcf(su, w, run, vcf_days)
    return run


def add_vcf(su, w, run, days):
    vcf_id = uuid.uuid4()
    with su.cursor() as cur:
        cur.execute(
            "INSERT INTO vcfs (org_id, id, sequencing_run_id, vcf_path, content_hash, created_at, created_by, retention_days) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (w.org, vcf_id, run.id, f"/x/{vcf_id}.vcf", "0" * 64, T0, w.lab1.user_id, days),
        )
    su.commit()
    run.vcf = vcf_id
    return vcf_id


def release(su, w, run, released_at):
    interp, report = uuid.uuid4(), uuid.uuid4()
    with su.cursor() as cur:
        cur.execute(
            "INSERT INTO interpretations (org_id,id,vcf_id,run_document,submission_key,created_at,created_by,retention_days) "
            "VALUES (%s,%s,%s,'{}'::jsonb,%s,%s,%s,1825)",
            (w.org, interp, run.vcf, uuid.uuid4().hex, T0, w.lab1.user_id),
        )
        cur.execute(
            "INSERT INTO reports (org_id,id,interpretation_id,created_at,created_by) VALUES (%s,%s,%s,%s,%s)",
            (w.org, report, interp, T0, w.lab1.user_id),
        )
        cur.execute(
            "INSERT INTO release_events (org_id,report_id,consumer,released_at,released_by,content_hash) VALUES (%s,%s,'clinician',%s,%s,%s)",
            (w.org, report, released_at, w.lab1.user_id, "1" * 64),
        )
    su.commit()


def vcf_text(sample, assembly="GRCh38", record_class="sv", n=1, base=1000, two_samples=False):
    length = 248956422 if assembly == "GRCh38" else 249250621
    cols = f"{sample}\tOTHER" if two_samples else sample
    out = f"##fileformat=VCFv4.2\n##contig=<ID=1,length={length}>\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\t{cols}\n"
    for i in range(n):
        pos = base + i * 10_000
        if record_class == "sv":
            out += f"1\t{pos}\tsv{i}\tN\t<DEL>\t.\tPASS\tSVTYPE=DEL;END={pos + 3000}\tGT\t{'0/1' if not two_samples else '0/1\t0/0'}\n"
        else:
            out += f"1\t{pos}\tc{i}\tN\t<DUP>\t.\tPASS\tSVTYPE=DUP;END={pos + 3000};CN=3\tGT:CN\t{'0/1:3' if not two_samples else '0/1:3\t0/0:2'}\n"
    return out.encode()


def approve_caller(dao, w, name="caller-x", version="1.0", record_class="sv"):
    dao.propose_caller_version(w.admin, name, version, record_class, "reviewed validation report")
    dao.approve_caller_version(w.appr1, name, version, record_class, "independent review complete")


def submit(
    dao,
    w,
    run,
    *,
    session=None,
    data=None,
    record_class="sv",
    assembly=None,
    name="caller-x",
    version="1.0",
    supersedes=None,
    sample=None,
):
    return dao.submit_call_set(
        session or w.lab1,
        run.id,
        record_class=record_class,
        assembly=assembly or run.assembly,
        caller_name=name,
        caller_version=version,
        vcf_sample_name=sample or run.sample,
        source_filename="calls.vcf",
        source_vcf=data if data is not None else vcf_text(run.sample, run.assembly, record_class),
        supersedes_id=supersedes,
    )


def system_principal(dao, org):
    dao._create_system_session(org)
    dao._DataAccess__connection.commit()


def tamper(su, sql, params=(), table="call_sets", trigger="trg_call_sets_immutable"):
    """Change a stored row the way only a table owner could: trigger off, update, trigger back on, each committed."""
    with su.cursor() as cur:
        cur.execute(f"ALTER TABLE {table} DISABLE TRIGGER {trigger}")
    su.commit()
    try:
        with su.cursor() as cur:
            cur.execute(sql, params)
        su.commit()
    except BaseException:
        su.rollback()  # a refused edit must not leave the connection in an aborted transaction
        raise
    finally:
        with su.cursor() as cur:
            cur.execute(f"ALTER TABLE {table} ENABLE TRIGGER {trigger}")
        su.commit()


def same_sample_run(su, w, run):
    """A SECOND sequencing run of the same sample (so the same bytes are a legitimate resubmission candidate)."""
    run_id = uuid.uuid4()
    with su.cursor() as cur:
        cur.execute(
            "INSERT INTO sequencing_runs (org_id, id, sample_id, created_at, created_by) VALUES (%s,%s,%s,%s,%s)",
            (w.org, run_id, run.sample, T0, w.lab1.user_id),
        )
    su.commit()
    other = World()
    other.id, other.sample, other.patient, other.assembly = run_id, run.sample, run.patient, run.assembly
    return other


def state(su, call_set_id):
    with su.cursor() as cur:
        cur.execute("SELECT public.call_set_state(org_id, id) FROM call_sets WHERE id = %s", (call_set_id,))
        row = cur.fetchone()
    su.commit()
    return row[0]


def row(su, sql, params=()):
    with su.cursor() as cur:
        cur.execute(sql, params)
        out = cur.fetchall()
    su.commit()
    return out


def events(su, call_set_id):
    return [
        r[0]
        for r in row(su, "SELECT event_type FROM call_set_events WHERE call_set_id = %s ORDER BY seq", (call_set_id,))
    ]


def audits(su, action):
    return row(
        su,
        "SELECT outcome, resource_type, resource_id, details, actor_role FROM audit_log WHERE action = %s ORDER BY log_id",
        (action,),
    )


@pytest.fixture()
def world(su, dao):
    w = build_org(su, dao, "a")
    w.run = new_run(su, dao, w, vcf_days=1825)
    approve_caller(dao, w)
    return w


def validated(su, dao, w, run=None, **kw):
    run = run or w.run
    cs = submit(dao, w, run, **kw)
    dao.validate_call_set(w.interp1, cs)
    assert state(su, cs) == "validated"
    return cs


# ─── caller versions ─────────────────────────────────────────────────────────


class TestCallerVersions:
    def test_administrator_proposes_a_different_approver_approves_the_exact_version(self, su, dao):
        w = build_org(su, dao)
        dao.propose_caller_version(w.admin, "manta", "1.6.0", "sv", "validation report 12")
        states = {
            (c["caller_name"], c["caller_version"], c["record_class"]): c["state"]
            for c in dao.list_caller_versions(w.lab1)
        }
        assert states == {("manta", "1.6.0", "sv"): "proposed"}
        dao.approve_caller_version(w.appr1, "manta", "1.6.0", "sv", "reviewed")
        assert {(c["caller_name"], c["caller_version"]): c["state"] for c in dao.list_caller_versions(w.lab1)} == {
            ("manta", "1.6.0"): "approved"
        }
        # a different VERSION or CLASS is not covered by that approval
        with pytest.raises(CallSetConflictError):
            dao.approve_caller_version(w.appr1, "manta", "1.6.1", "sv", "x")
        with pytest.raises(CallSetConflictError):
            dao.approve_caller_version(w.appr1, "manta", "1.6.0", "cnv", "x")

    def test_the_proposer_cannot_approve_their_own_proposal(self, su, dao):
        w = build_org(su, dao)
        dao.propose_caller_version(w.admin_approver, "gridss", "2.0", "sv", "basis")
        with pytest.raises(CallSetConflictError, match="proposer"):
            dao.approve_caller_version(w.admin_approver, "gridss", "2.0", "sv", "basis")
        assert row(su, "SELECT event_type FROM caller_approvals ORDER BY seq") == [("proposed",)]

    def test_roles_are_enforced_for_each_caller_act(self, su, dao):
        w = build_org(su, dao)
        with pytest.raises(AuthorizationError):
            dao.propose_caller_version(w.appr1, "c", "1", "sv", "b")
        dao.propose_caller_version(w.admin, "c", "1", "sv", "b")
        with pytest.raises(AuthorizationError):
            dao.approve_caller_version(w.admin, "c", "1", "sv", "b")
        dao.approve_caller_version(w.appr1, "c", "1", "sv", "b")
        with pytest.raises(AuthorizationError):
            dao.retire_caller_version(w.interp1, "c", "1", "sv", "b")
        dao.retire_caller_version(w.appr2, "c", "1", "sv", "no longer supported")
        assert [c["state"] for c in dao.list_caller_versions(w.admin)] == ["retired"]
        with pytest.raises(AuthorizationError):
            dao.list_caller_versions(w.reader)

    def test_a_retired_version_blocks_validation_of_a_pending_call_set(self, su, dao, world):
        cs = submit(dao, world, world.run)
        dao.retire_caller_version(world.appr1, "caller-x", "1.0", "sv", "withdrawn from service")
        with pytest.raises(CallSetConflictError, match="not currently approved"):
            dao.validate_call_set(world.interp1, cs)
        assert state(su, cs) == "pending_validation"
        # positive twin: re-proposing and approving a retired version makes validation possible again
        approve_caller(dao, world)
        dao.validate_call_set(world.interp1, cs)
        assert state(su, cs) == "validated"


# ─── submit ──────────────────────────────────────────────────────────────────


class TestSubmit:
    def test_stores_immutable_evidence_with_hash_parser_version_snapshot_and_a_submitted_event(self, su, dao, world):
        data = vcf_text(world.run.sample, n=3)
        cs = submit(dao, world, world.run, data=data)
        (r,) = row(
            su,
            "SELECT source_content_hash, parser_version, retention_days, created_by, sequencing_run_id, assembly, record_class, "
            "caller_approved_at_submission, octet_length(source_vcf), parsed_document->'counts' FROM call_sets WHERE id = %s",
            (cs,),
        )
        assert r[0] == hashlib.sha256(data).hexdigest()
        assert r[1] == CALLSET_PARSER_VERSION
        assert r[2] == 1825, "inherits the run's VCF retention"
        assert (r[3], r[4], r[5], r[6]) == (world.lab1.user_id, world.run.id, "GRCh38", "sv")
        assert r[7] is True, "the caller was approved at submission (informational snapshot)"
        assert r[8] == len(data)
        assert r[9]["records"] == 3 and r[9]["detected"] == 3
        assert events(su, cs) == ["submitted"]
        assert state(su, cs) == "pending_validation"
        doc = row(su, "SELECT parsed_document FROM call_sets WHERE id = %s", (cs,))[0][0]
        assert verify_document(data, doc)
        (ok,) = audits(su, "call_set_submitted")
        assert ok[0] == "success" and ok[1] == "call_set" and ok[2] == str(cs) and ok[4] == "Lab technician"
        assert "source" not in json.dumps(ok[3]) and "1\\t1000" not in json.dumps(ok[3]), (
            "no genomic content in the audit row"
        )

    def test_retention_without_a_vcf_is_resolved_by_the_platform_rule_not_invented(self, su, dao, world):
        bare = new_run(su, dao, world)
        cs = submit(dao, world, bare)
        expected = dao._resolve_vcf_retention_days(world.lab1, bare.id)
        assert row(su, "SELECT retention_days FROM call_sets WHERE id = %s", (cs,))[0][0] == expected > 0

    def test_an_unapproved_caller_is_accepted_into_quarantine_but_never_validated(self, su, dao, world):
        cs = submit(dao, world, world.run, name="unknown-caller", version="9")
        assert row(su, "SELECT caller_approved_at_submission FROM call_sets WHERE id = %s", (cs,))[0][0] is False
        with pytest.raises(CallSetConflictError, match="not currently approved"):
            dao.validate_call_set(world.interp1, cs)
        assert state(su, cs) == "pending_validation"

    @pytest.mark.parametrize("role_holder", ["interp1", "appr1", "admin", "reader"])
    def test_only_a_lab_technician_may_submit_and_the_refusal_is_audited(self, su, dao, world, role_holder):
        with pytest.raises(AuthorizationError):
            submit(dao, world, world.run, session=getattr(world, role_holder))
        assert row(su, "SELECT count(*) FROM call_sets") == [(0,)]
        denied = audits(su, "call_set_submitted")
        assert [d[0] for d in denied] == ["denied"]

    def test_another_organisations_run_is_the_same_not_found_as_a_missing_one(self, su, dao, world):
        other = build_org(su, dao, "b")
        with pytest.raises(NotFoundError) as cross:
            dao.submit_call_set(
                other.lab1,
                world.run.id,
                record_class="sv",
                assembly="GRCh38",
                caller_name="caller-x",
                caller_version="1.0",
                vcf_sample_name=world.run.sample,
                source_filename="c.vcf",
                source_vcf=vcf_text(world.run.sample),
            )
        with pytest.raises(NotFoundError) as missing:
            dao.submit_call_set(
                other.lab1,
                uuid.uuid4(),
                record_class="sv",
                assembly="GRCh38",
                caller_name="caller-x",
                caller_version="1.0",
                vcf_sample_name=world.run.sample,
                source_filename="c.vcf",
                source_vcf=vcf_text(world.run.sample),
            )
        assert str(cross.value) == str(missing.value)
        assert row(su, "SELECT count(*) FROM call_sets") == [(0,)]

    def test_sample_identity_assembly_and_header_are_verified_and_nothing_is_stored_on_refusal(self, su, dao, world):
        with pytest.raises(CallSetConflictError, match="cannot accept"):
            stranger = str(
                uuid.uuid4()
            )  # a file that is consistent with itself but names a sample this run does not have
            submit(dao, world, world.run, sample=stranger, data=vcf_text(stranger))
        with pytest.raises(CallSetInputError, match="sample"):  # right declared name, wrong column in the file
            submit(dao, world, world.run, data=vcf_text("someone-else"))
        with pytest.raises(CallSetInputError, match="exactly one sample"):
            submit(dao, world, world.run, data=vcf_text(world.run.sample, two_samples=True))
        with pytest.raises(CallSetConflictError, match="cannot accept"):  # a GRCh37 call set against a GRCh38 test
            submit(dao, world, world.run, assembly="GRCh37", data=vcf_text(world.run.sample, "GRCh37"))
        with pytest.raises(CallSetInputError):  # a GRCh38 header declared as GRCh38 but file is GRCh37 lengths
            submit(dao, world, world.run, data=vcf_text(world.run.sample, "GRCh37"))
        with pytest.raises(CallSetInputError, match="liftover"):
            submit(dao, world, world.run, assembly="hg18")
        with pytest.raises(CallSetInputError, match="UTF-8"):
            submit(dao, world, world.run, data=b"\xff\xfe not text")
        assert row(su, "SELECT count(*) FROM call_sets") == [(0,)]
        assert row(su, "SELECT count(*) FROM call_set_events") == [(0,)]
        # positive twin: the same run accepts a correct file
        submit(dao, world, world.run)
        assert row(su, "SELECT count(*) FROM call_sets") == [(1,)]

    def test_size_and_record_limits_hold_and_only_the_database_byte_check_is_a_code_ceiling(
        self, su, dao, world, monkeypatch
    ):
        monkeypatch.setenv("CLINICAL_CALLSET_MAX_RECORDS", "3")
        with pytest.raises(CallSetInputError, match="limit"):
            submit(dao, world, world.run, data=vcf_text(world.run.sample, n=4))
        submit(dao, world, world.run, data=vcf_text(world.run.sample, n=3))
        monkeypatch.setenv("CLINICAL_CALLSET_MAX_BYTES", "100")
        with pytest.raises(CallSetInputError, match="bytes"):
            submit(dao, world, world.run, data=vcf_text(world.run.sample, n=2))
        monkeypatch.setenv("CLINICAL_CALLSET_MAX_BYTES", str(10**12))
        monkeypatch.setenv("CLINICAL_CALLSET_MAX_RECORDS", str(10**12))
        # bytes: clamped to the schema's 64 MiB CHECK (the one real bound); records: configuration, NOT clamped
        assert DataAccess._callset_limits() == (64 * 1024 * 1024, 10**12)
        monkeypatch.setenv("CLINICAL_CALLSET_MAX_RECORDS", "not-a-number")
        assert DataAccess._callset_limits()[1] == DataAccess.CALLSET_DEFAULT_MAX_RECORDS

    def test_the_limits_are_enforced_before_any_decoding_or_parsing(self, su, dao, world, monkeypatch):
        import clinical.genome_analysis as ga

        def must_not_run(*args, **kwargs):
            raise AssertionError("the parser was reached for an oversized submission")

        monkeypatch.setitem(ga.PARSER_RULESETS, CALLSET_PARSER_VERSION, must_not_run)
        monkeypatch.setenv("CLINICAL_CALLSET_MAX_RECORDS", "2")
        with pytest.raises(CallSetInputError, match="3 records"):
            submit(dao, world, world.run, data=vcf_text(world.run.sample, n=3))
        # even bytes that are not text at all are refused on size first, never decoded
        monkeypatch.setenv("CLINICAL_CALLSET_MAX_BYTES", "4")
        with pytest.raises(CallSetInputError, match="bytes"):
            submit(dao, world, world.run, data=b"\xff" * 50)
        assert row(su, "SELECT count(*) FROM call_sets") == [(0,)]

    def test_an_identical_retry_on_the_same_run_is_idempotent_and_adds_no_events(self, su, dao, world):
        data = vcf_text(world.run.sample)
        first = submit(dao, world, world.run, data=data)
        again = submit(dao, world, world.run, data=data)
        assert again == first
        assert row(su, "SELECT count(*) FROM call_sets") == [(1,)]
        assert events(su, first) == ["submitted"], "a retry must not add a lifecycle event"
        dao.validate_call_set(world.interp1, first)
        assert submit(dao, world, world.run, data=data) == first, "still the same call set once it is validated"
        assert events(su, first) == ["submitted", "validated"]

    def test_the_same_bytes_with_other_parameters_or_by_another_submitter_are_a_conflict_not_a_retry(
        self, su, dao, world
    ):
        data = vcf_text(world.run.sample)
        first = submit(dao, world, world.run, data=data)
        with pytest.raises(CallSetConflictError, match="conflicts with an existing"):
            submit(dao, world, world.run, data=data, session=world.lab2)
        with pytest.raises(CallSetConflictError, match="conflicts with an existing"):
            submit(dao, world, world.run, data=data, version="2.0")
        assert row(su, "SELECT count(*) FROM call_sets") == [(1,)]
        assert events(su, first) == ["submitted"]

    def test_concurrent_identical_retries_become_one_call_set(self, su, dao, world):
        data, results, barrier = vcf_text(world.run.sample), [], threading.Barrier(2)

        def attempt():
            connection, local = _fresh_dao()
            try:
                barrier.wait(timeout=10)
                results.append(submit(local, world, world.run, data=data))
            finally:
                connection.close()

        threads = [threading.Thread(target=attempt) for _ in range(2)]
        [t.start() for t in threads]
        [t.join(timeout=30) for t in threads]
        assert len(results) == 2 and results[0] == results[1]
        assert row(su, "SELECT count(*) FROM call_sets") == [(1,)]
        assert row(su, "SELECT count(*) FROM call_set_events") == [(1,)]

    def test_the_same_bytes_on_another_run_are_refused_in_every_state_until_retracted_for_wrong_run_placement(
        self, su, dao, world
    ):
        data = vcf_text(world.run.sample)
        other = same_sample_run(su, world, world.run)
        original = submit(dao, world, world.run, data=data)
        with pytest.raises(CallSetConflictError, match="conflicts with an existing"):
            submit(dao, world, other, data=data)  # pending
        dao.validate_call_set(world.interp1, original)
        with pytest.raises(CallSetConflictError, match="conflicts with an existing"):
            submit(dao, world, other, data=data)  # validated
        dao.retract_call_set(world.appr1, original, "data error")  # retracted, but NOT for wrong-run placement
        with pytest.raises(CallSetConflictError, match="conflicts with an existing"):
            submit(dao, world, other, data=data)
        assert row(su, "SELECT count(*) FROM call_sets") == [(1,)]

    def test_a_wrong_run_retraction_allows_a_NEW_call_set_that_is_checked_and_validated_afresh(self, su, dao, world):
        data = vcf_text(world.run.sample)
        other = same_sample_run(su, world, world.run)
        original = validated(su, dao, world, data=data)
        dao.retract_call_set(world.appr1, original, "placed on the wrong run", wrong_run_placement=True)
        assert row(
            su, "SELECT checks FROM call_set_events WHERE call_set_id = %s AND event_type = 'retracted'", (original,)
        ) == [({"reason_code": "wrong_run_placement"},)]
        fresh = submit(dao, world, other, data=data)
        assert fresh != original and state(su, fresh) == "pending_validation"
        assert events(su, fresh) == ["submitted"], "no lifecycle is carried over"
        assert state(su, original) == "retracted"
        assert dao.list_patient_validated_call_sets(world.reader, world.run.patient) == [], (
            "not eligible until validated"
        )
        # fresh checks apply to the new call set: a QC failure on its sample blocks validation
        with su.cursor() as cur:
            cur.execute("UPDATE samples SET qc_status = 'failed' WHERE sample_id = %s", (world.run.sample,))
        su.commit()
        with pytest.raises(CallSetConflictError, match="QC"):
            dao.validate_call_set(world.interp2, fresh)
        with su.cursor() as cur:
            cur.execute("UPDATE samples SET qc_status = 'passed' WHERE sample_id = %s", (world.run.sample,))
        su.commit()
        dao.validate_call_set(world.interp2, fresh)
        assert [r["call_set_id"] for r in dao.list_patient_validated_call_sets(world.reader, world.run.patient)] == [
            fresh
        ]

    def test_the_wrong_run_code_is_only_meaningful_on_a_retraction(self, su, dao, world):
        cs = submit(dao, world, world.run)
        with pytest.raises(psycopg.errors.CheckViolation):
            with su.cursor() as cur:
                cur.execute("SELECT set_config('clinical.actor_id', %s, true)", (str(world.interp1.user_id),))
                cur.execute(
                    "INSERT INTO call_set_events (org_id, call_set_id, event_type, actor_id, actor_role, occurred_at, reason, checks) "
                    "VALUES (%s, %s, 'rejected', %s, 'Interpreter', now(), 'x', %s::jsonb)",
                    (world.org, cs, world.interp1.user_id, json.dumps({"reason_code": "wrong_run_placement"})),
                )

    def test_a_failure_after_the_row_is_written_but_before_the_event_leaves_nothing(self, su, dao, world, monkeypatch):
        def boom(*args, **kwargs):
            raise RuntimeError("injected failure between the two inserts")

        monkeypatch.setattr(DataAccess, "_insert_call_set_event", boom)
        with pytest.raises(RuntimeError):
            submit(dao, world, world.run)
        assert row(su, "SELECT count(*) FROM call_sets") == [(0,)]
        assert row(su, "SELECT count(*) FROM call_set_events") == [(0,)]


# ─── validation, rejection, withdrawal ───────────────────────────────────────


class TestDecisions:
    def test_validation_records_the_exact_checks_and_the_current_approval(self, su, dao, world):
        cs = submit(dao, world, world.run)
        event_id = dao.validate_call_set(world.interp1, cs)
        (r,) = row(
            su,
            "SELECT e.checks, e.actor_id, e.actor_role, e.caller_approval_id, a.event_type, a.caller_name, a.caller_version "
            "FROM call_set_events e JOIN caller_approvals a ON a.id = e.caller_approval_id WHERE e.id = %s",
            (event_id,),
        )
        assert r[0] == {"sample_identity": True, "assembly": True, "caller": True}
        assert (r[1], r[2], r[4], r[5], r[6]) == (world.interp1.user_id, "Interpreter", "approved", "caller-x", "1.0")
        assert events(su, cs) == ["submitted", "validated"]

    def test_the_submitter_cannot_validate_even_when_they_hold_the_interpreter_role(self, su, dao, world):
        cs = submit(dao, world, world.run, session=world.lab_interp)
        with pytest.raises(AuthorizationError, match="submitter"):
            dao.validate_call_set(world.lab_interp, cs)
        assert state(su, cs) == "pending_validation"
        dao.validate_call_set(world.interp1, cs)
        assert state(su, cs) == "validated"

    def test_a_non_interpreter_cannot_validate_or_reject(self, su, dao, world):
        cs = submit(dao, world, world.run)
        for who in (world.lab2, world.appr1, world.admin):
            with pytest.raises(AuthorizationError):
                dao.validate_call_set(who, cs)
            with pytest.raises(AuthorizationError):
                dao.reject_call_set(who, cs, "no")
        assert state(su, cs) == "pending_validation"

    def test_validation_refuses_when_the_stored_document_no_longer_matches_the_bytes(self, su, dao, world):
        cs = submit(dao, world, world.run)
        tamper(
            su,
            "UPDATE call_sets SET parsed_document = jsonb_set(parsed_document, '{records,0,status}', '\"uncertain\"') WHERE id = %s",
            (cs,),
        )
        with pytest.raises(CallSetConflictError, match="Integrity"):
            dao.validate_call_set(world.interp1, cs)
        assert state(su, cs) == "pending_validation"

    def test_validation_rechecks_identity_and_assembly_at_decision_time(self, su, dao, world):
        """The database now refuses these edits while a call set stands on them; the owner-level bypass (trigger off)
        proves the DAO does not rely on that and re-checks anyway."""
        cs = submit(dao, world, world.run)
        tamper(
            su,
            "UPDATE tests SET assembly = 'GRCh37' WHERE test_id = %s",
            (world.tests["GRCh38"],),
            table="tests",
            trigger="trg_callset_identity_tests",
        )
        with pytest.raises(CallSetConflictError, match="Assembly mismatch.*GRCh38.*GRCh37"):
            dao.validate_call_set(world.interp1, cs)
        tamper(
            su,
            "UPDATE tests SET assembly = 'GRCh38' WHERE test_id = %s",
            (world.tests["GRCh38"],),
            table="tests",
            trigger="trg_callset_identity_tests",
        )
        dao.validate_call_set(world.interp1, cs)
        assert state(su, cs) == "validated"

    def test_reject_needs_a_reason_and_a_different_actor_and_is_final(self, su, dao, world):
        cs = submit(dao, world, world.run, session=world.lab_interp)
        with pytest.raises(CallSetInputError):
            dao.reject_call_set(world.interp1, cs, "   ")
        with pytest.raises(AuthorizationError):
            dao.reject_call_set(world.lab_interp, cs, "my own file")
        dao.reject_call_set(world.interp1, cs, "wrong sample ordering")
        assert state(su, cs) == "rejected"
        with pytest.raises(CallSetConflictError):
            dao.validate_call_set(world.interp2, cs)
        assert row(
            su, "SELECT reason FROM call_set_events WHERE call_set_id = %s AND event_type = 'rejected'", (cs,)
        ) == [("wrong sample ordering",)]

    def test_withdrawal_is_the_submitters_alone_and_only_while_pending(self, su, dao, world):
        cs = submit(dao, world, world.run)
        with pytest.raises(AuthorizationError):
            dao.withdraw_call_set(world.lab2, cs, "not mine")
        with pytest.raises(CallSetInputError):
            dao.withdraw_call_set(world.lab1, cs, "")
        dao.withdraw_call_set(world.lab1, cs, "uploaded the wrong file")
        assert state(su, cs) == "withdrawn"
        with pytest.raises(CallSetConflictError):
            dao.validate_call_set(world.interp1, cs)
        done = submit(dao, world, world.run, data=vcf_text(world.run.sample, n=2))
        dao.validate_call_set(world.interp1, done)
        with pytest.raises(CallSetConflictError):
            dao.withdraw_call_set(world.lab1, done, "too late")

    def test_every_refusal_is_a_denied_or_error_audit_row_never_a_success(self, su, dao, world):
        cs = submit(dao, world, world.run, session=world.lab_interp)
        with pytest.raises(AuthorizationError):
            dao.validate_call_set(world.lab_interp, cs)
        with pytest.raises(CallSetConflictError):
            dao.decline_call_set_retraction(world.appr1, cs, "nothing to decline")
        assert [a[0] for a in audits(su, "call_set_validated")] == ["denied"]
        assert [a[0] for a in audits(su, "call_set_retraction_declined")] == ["error"]


# ─── replacement ─────────────────────────────────────────────────────────────


class TestReplacement:
    def test_validating_a_replacement_atomically_supersedes_its_predecessor(self, su, dao, world):
        first = validated(su, dao, world)
        second = submit(dao, world, world.run, data=vcf_text(world.run.sample, n=2), supersedes=first)
        assert state(su, first) == "validated", "the predecessor stays live until the replacement is validated"
        dao.validate_call_set(world.interp2, second)
        assert (state(su, first), state(su, second)) == ("superseded", "validated")
        assert events(su, first) == ["submitted", "validated", "superseded"]
        reader_view = dao.list_patient_validated_call_sets(world.reader, world.run.patient)
        assert [r["call_set_id"] for r in reader_view] == [second]

    def test_a_replacement_must_name_a_live_validated_call_set_of_the_same_run_and_class(self, su, dao, world):
        pending = submit(dao, world, world.run)
        with pytest.raises(CallSetConflictError):
            submit(dao, world, world.run, data=vcf_text(world.run.sample, n=2), supersedes=pending)
        with pytest.raises(NotFoundError):
            submit(dao, world, world.run, data=vcf_text(world.run.sample, n=3), supersedes=uuid.uuid4())
        dao.validate_call_set(world.interp1, pending)
        other_run = new_run(su, dao, world, patient=world.run.patient)
        with pytest.raises(CallSetConflictError):
            submit(dao, world, other_run, supersedes=pending)
        approve_caller(dao, world, record_class="cnv")
        with pytest.raises(CallSetConflictError):
            submit(dao, world, world.run, record_class="cnv", supersedes=pending)

    def test_two_concurrent_replacements_of_one_call_set_exactly_one_wins(self, su, dao, world):
        first = validated(su, dao, world)
        outcomes, barrier = [], threading.Barrier(2)

        def attempt(n):
            connection, local = _fresh_dao()
            try:
                barrier.wait(timeout=10)
                local.submit_call_set(
                    world.lab1,
                    world.run.id,
                    record_class="sv",
                    assembly="GRCh38",
                    caller_name="caller-x",
                    caller_version="1.0",
                    vcf_sample_name=world.run.sample,
                    source_filename="c.vcf",
                    source_vcf=vcf_text(world.run.sample, n=n),
                    supersedes_id=first,
                )
                outcomes.append("ok")
            except CallSetConflictError:
                outcomes.append("conflict")
            finally:
                connection.close()

        threads = [threading.Thread(target=attempt, args=(n,)) for n in (2, 3)]
        [t.start() for t in threads]
        [t.join(timeout=30) for t in threads]
        assert sorted(outcomes) == ["conflict", "ok"]
        assert row(su, "SELECT count(*) FROM call_sets WHERE supersedes_id = %s", (first,)) == [(1,)]


# ─── retraction ──────────────────────────────────────────────────────────────


class TestRetraction:
    def test_request_decline_and_retract_follow_the_role_matrix(self, su, dao, world):
        cs = validated(su, dao, world)
        with pytest.raises(AuthorizationError):
            dao.retract_call_set(world.interp1, cs, "interpreters only request")
        with pytest.raises(AuthorizationError):
            dao.request_call_set_retraction(world.appr1, cs, "approvers retract directly")
        dao.request_call_set_retraction(world.interp1, cs, "caller flagged a defect")
        with pytest.raises(CallSetConflictError, match="already open"):
            dao.request_call_set_retraction(world.interp2, cs, "second request")
        dao.decline_call_set_retraction(world.appr1, cs, "defect does not affect these records")
        assert state(su, cs) == "validated"
        assert len(dao.list_patient_validated_call_sets(world.reader, world.run.patient)) == 1
        dao.request_call_set_retraction(world.interp1, cs, "new evidence")
        dao.retract_call_set(world.appr2, cs, "confirmed defect")
        assert state(su, cs) == "retracted"
        assert events(su, cs) == [
            "submitted",
            "validated",
            "retraction_requested",
            "retraction_declined",
            "retraction_requested",
            "retracted",
        ]
        assert dao.list_patient_validated_call_sets(world.reader, world.run.patient) == []
        answered = row(
            su,
            "SELECT r.responds_to = q.id FROM call_set_events r JOIN call_set_events q ON q.call_set_id = r.call_set_id AND q.event_type = 'retraction_requested' AND q.seq = (SELECT max(seq) FROM call_set_events WHERE call_set_id = r.call_set_id AND event_type = 'retraction_requested') WHERE r.event_type = 'retracted' AND r.call_set_id = %s",
            (cs,),
        )
        assert answered == [(True,)]

    def test_a_pending_or_retracted_call_set_cannot_be_retracted_and_a_decline_needs_an_open_request(
        self, su, dao, world
    ):
        pending = submit(dao, world, world.run)
        with pytest.raises(CallSetConflictError):
            dao.retract_call_set(world.appr1, pending, "not validated")
        with pytest.raises(CallSetConflictError):
            dao.request_call_set_retraction(world.interp1, pending, "not validated")
        dao.validate_call_set(world.interp1, pending)
        with pytest.raises(CallSetConflictError, match="no open"):
            dao.decline_call_set_retraction(world.appr1, pending, "nothing to decline")
        dao.retract_call_set(world.appr1, pending, "direct approver retraction")
        with pytest.raises(CallSetConflictError):
            dao.retract_call_set(world.appr2, pending, "twice")

    def test_two_concurrent_retractions_exactly_one_wins(self, su, dao, world):
        cs = validated(su, dao, world)
        outcomes, barrier = [], threading.Barrier(2)

        def attempt(session):
            connection, local = _fresh_dao()
            try:
                barrier.wait(timeout=10)
                local.retract_call_set(session, cs, "concurrent")
                outcomes.append("ok")
            except CallSetConflictError:
                outcomes.append("conflict")
            finally:
                connection.close()

        threads = [threading.Thread(target=attempt, args=(s,)) for s in (world.appr1, world.appr2)]
        [t.start() for t in threads]
        [t.join(timeout=30) for t in threads]
        assert sorted(outcomes) == ["conflict", "ok"]
        assert events(su, cs).count("retracted") == 1


# ─── reads and the clinical map ──────────────────────────────────────────────


class TestReads:
    def test_only_validated_live_eligible_evidence_reaches_the_clinical_map(self, su, dao, world):
        patient = world.run.patient
        good = validated(su, dao, world)
        pending = submit(dao, world, world.run, data=vcf_text(world.run.sample, n=2), record_class="sv", version="1.0")
        rejected_run = new_run(su, dao, world, patient=patient)
        rej = submit(dao, world, rejected_run)
        dao.reject_call_set(world.interp1, rej, "bad")
        withdrawn_run = new_run(su, dao, world, patient=patient)
        wd = submit(dao, world, withdrawn_run)
        dao.withdraw_call_set(world.lab1, wd, "oops")
        retracted_run = new_run(su, dao, world, patient=patient)
        rt = submit(dao, world, retracted_run)
        dao.validate_call_set(world.interp1, rt)
        dao.retract_call_set(world.appr1, rt, "defect")
        shown = dao.list_patient_validated_call_sets(world.reader, patient)
        assert [r["call_set_id"] for r in shown] == [good]
        assert {pending, rej, wd, rt}.isdisjoint({r["call_set_id"] for r in shown})
        r = shown[0]
        assert (r["record_class"], r["assembly"], r["caller_name"], r["caller_version"]) == (
            "sv",
            "GRCh38",
            "caller-x",
            "1.0",
        )
        assert r["call_set"].counts()["records"] == 1

    def test_assembly_incompatible_evidence_is_not_returned(self, su, dao, world):
        validated(su, dao, world)
        assert len(dao.list_patient_validated_call_sets(world.reader, world.run.patient)) == 1
        tamper(
            su,
            "UPDATE tests SET assembly = 'GRCh37' WHERE test_id = %s",
            (world.tests["GRCh38"],),
            table="tests",
            trigger="trg_callset_identity_tests",
        )
        assert dao.list_patient_validated_call_sets(world.reader, world.run.patient) == []

    def test_the_patient_is_bound_to_the_session_organisation_and_the_role_gate_comes_first(self, su, dao, world):
        validated(su, dao, world)
        other = build_org(su, dao, "b")
        with pytest.raises(NotFoundError) as cross:
            dao.list_patient_validated_call_sets(other.reader, world.run.patient)
        with pytest.raises(NotFoundError) as missing:
            dao.list_patient_validated_call_sets(other.reader, uuid.uuid4())
        assert str(cross.value) == str(missing.value)
        for who in (world.lab1, world.admin):
            with pytest.raises(AuthorizationError):
                dao.list_patient_validated_call_sets(who, world.run.patient)
        with pytest.raises(AuthorizationError):  # the gate answers before existence is consulted
            dao.list_patient_validated_call_sets(world.lab1, uuid.uuid4())

    def test_the_map_read_is_audited_as_genome_read_with_no_payload(self, su, dao, world):
        validated(su, dao, world)
        dao.list_patient_validated_call_sets(world.reader, world.run.patient)
        (a,) = audits(su, "genome_read")
        assert a[0] == "success" and a[1] == "patient" and a[2] == str(world.run.patient)
        assert a[3] in ({}, None), "nothing about the genome is written to an append-only log"
        assert audits(su, "genome_callset_read") == [], "the approved design keeps map reads under genome_read"

    def test_the_endpoint_source_rebuilds_validated_call_sets_and_drops_and_audits_a_corrupted_one(
        self, su, dao, world
    ):
        good = validated(su, dao, world)
        bad_run = new_run(su, dao, world, patient=world.run.patient)
        bad = validated(su, dao, world, run=bad_run)
        source = DaoCallSetSource(dao)
        got = source.call_sets_for(world.reader, world.run.patient)
        assert sorted(cs.call_set_id for cs in got) == sorted([str(good), str(bad)])
        assert all(cs.assembly == "GRCh38" and cs.record_class == "sv" and cs.counts()["detected"] == 1 for cs in got)
        assert audits(su, "call_set_integrity_failure") == []
        tamper(
            su,
            "UPDATE call_sets SET parsed_document = jsonb_set(parsed_document, '{records,0,record,breakpoints,0,pos}', '999999999999') WHERE id = %s",
            (bad,),
        )
        got = source.call_sets_for(world.reader, world.run.patient)
        assert [cs.call_set_id for cs in got] == [str(good)]
        (incident,) = audits(su, "call_set_integrity_failure")
        assert (incident[0], incident[1], incident[2]) == ("error", "call_set", str(bad))
        assert incident[3] in ({}, None), "the incident row names the id and carries no genomic content"

    def test_run_listing_and_review_return_no_bytes_and_respect_organisation_and_role(self, su, dao, world):
        cs = validated(su, dao, world)
        listing = dao.list_call_sets_for_run(world.interp1, world.run.id)
        assert [(x["id"], x["state"]) for x in listing] == [(cs, "validated")]
        assert "source_vcf" not in listing[0]
        review = dao.get_call_set_for_review(world.interp1, cs)
        assert "source_vcf" not in review and review["document"]["counts"]["records"] == 1
        assert [e["event_type"] for e in review["events"]] == ["submitted", "validated"]
        other = build_org(su, dao, "b")
        with pytest.raises(NotFoundError):
            dao.list_call_sets_for_run(other.interp1, world.run.id)
        with pytest.raises(NotFoundError):
            dao.get_call_set_for_review(other.interp1, cs)
        with pytest.raises(AuthorizationError):
            dao.get_call_set_for_review(world.reader, cs)


# ─── retention and redaction ─────────────────────────────────────────────────


class TestRetention:
    def _setup_purgeable(self, su, dao, world):
        cs = validated(su, dao, world)
        release(su, world, world.run, datetime(2030, 1, 1, tzinfo=timezone.utc))
        system_principal(dao, world.org)
        su.commit()
        return cs

    def test_the_snapshot_is_taken_once_and_never_follows_later_policy(self, su, dao, world):
        cs = submit(dao, world, world.run)
        assert row(su, "SELECT retention_days FROM call_sets WHERE id = %s", (cs,)) == [(1825,)]
        with su.cursor() as cur:
            cur.execute("UPDATE vcfs SET retention_days = 30 WHERE id = %s", (world.run.vcf,))
        su.commit()
        assert row(su, "SELECT retention_days FROM call_sets WHERE id = %s", (cs,)) == [(1825,)]
        later = submit(dao, world, world.run, data=vcf_text(world.run.sample, n=2))
        assert row(su, "SELECT retention_days FROM call_sets WHERE id = %s", (later,)) == [(30,)], (
            "a NEW call set inherits the run's current VCF value"
        )

    def test_expired_evidence_is_redacted_atomically_and_metadata_survives(self, su, dao, world, ret_conn):
        cs = self._setup_purgeable(su, dao, world)
        before = row(
            su,
            "SELECT source_content_hash, sequencing_run_id, caller_name, parser_version, created_by FROM call_sets WHERE id = %s",
            (cs,),
        )
        principal = RetentionPrincipal(ret_conn)
        early = principal.purge_expired(world.org, "call_set", now=datetime(2034, 12, 30, tzinfo=timezone.utc))
        ret_conn.commit()
        assert early.tombstoned_ids == [] and row(
            su, "SELECT source_vcf IS NOT NULL FROM call_sets WHERE id = %s", (cs,)
        ) == [(True,)]
        result = principal.purge_expired(world.org, "call_set", now=datetime(2035, 1, 2, tzinfo=timezone.utc))
        ret_conn.commit()
        assert result.tombstoned_ids == [cs]
        (r,) = row(
            su,
            "SELECT source_vcf IS NULL, parsed_document IS NULL, tombstoned_at IS NOT NULL, content_redacted_at IS NOT NULL, tombstoned_by = content_redacted_by FROM call_sets WHERE id = %s",
            (cs,),
        )
        assert r == (True, True, True, True, True)
        assert (
            row(
                su,
                "SELECT source_content_hash, sequencing_run_id, caller_name, parser_version, created_by FROM call_sets WHERE id = %s",
                (cs,),
            )
            == before
        )
        assert row(
            su,
            "SELECT bool_and(tombstoned_at IS NOT NULL), count(*) FROM call_set_events WHERE call_set_id = %s",
            (cs,),
        ) == [(True, 2)]
        assert events(su, cs) == ["submitted", "validated"], "the lifecycle is preserved"
        (a,) = audits(su, "retention_purge")
        assert (a[0], a[1], a[2], a[3], a[4]) == (
            "success",
            "call_set",
            str(cs),
            {"artefact_class": "call_set"},
            "System",
        )

    def test_a_redacted_call_set_takes_no_further_act_and_leaves_the_map(self, su, dao, world, ret_conn):
        cs = self._setup_purgeable(su, dao, world)
        RetentionPrincipal(ret_conn).purge_expired(world.org, "call_set", now=FAR)
        ret_conn.commit()
        assert dao.list_patient_validated_call_sets(world.reader, world.run.patient) == []
        with pytest.raises(CallSetConflictError):
            dao.retract_call_set(world.appr1, cs, "too late")
        with pytest.raises(CallSetConflictError):
            dao.request_call_set_retraction(world.interp1, cs, "too late")
        review = dao.get_call_set_for_review(world.interp1, cs)
        assert review["document"] is None and review["content_redacted_at"] is not None

    def test_a_live_successor_blocks_the_purge_of_its_predecessor(self, su, dao, world, ret_conn):
        first = validated(su, dao, world)
        second = submit(dao, world, world.run, data=vcf_text(world.run.sample, n=2), supersedes=first)
        dao.validate_call_set(world.interp2, second)
        release(su, world, world.run, datetime(2030, 1, 1, tzinfo=timezone.utc))
        system_principal(dao, world.org)
        su.commit()
        result = RetentionPrincipal(ret_conn).purge_expired(world.org, "call_set", now=FAR)
        ret_conn.commit()
        assert result.tombstoned_ids == [second], (
            "the successor (no live successor of its own) goes; the predecessor was blocked while it lived"
        )
        again = RetentionPrincipal(ret_conn).purge_expired(world.org, "call_set", now=FAR)
        ret_conn.commit()
        assert again.tombstoned_ids == [first], (
            "once the successor is tombstoned the predecessor is released from the block"
        )

    def test_runs_without_a_vcf_or_a_release_go_to_the_review_queue_and_are_never_purged(
        self, su, dao, world, ret_conn
    ):
        no_vcf = new_run(su, dao, world)
        orphan = submit(dao, world, no_vcf)
        no_release = world.run
        waiting = submit(dao, world, no_release)
        system_principal(dao, world.org)
        su.commit()
        principal = RetentionPrincipal(ret_conn)
        result = principal.purge_expired(world.org, "call_set", now=FAR)
        ret_conn.commit()
        assert result.tombstoned_ids == []
        assert sorted(result.unresolved_ids) == sorted([orphan, waiting])
        queue = {q["call_set_id"]: q["reason"] for q in principal.unresolved_call_sets(world.org)}
        assert queue == {orphan: "no_vcf", waiting: "no_release"}
        assert row(su, "SELECT count(*) FROM call_sets WHERE tombstoned_at IS NOT NULL OR source_vcf IS NULL") == [(0,)]
        # positive twin: a release moves it out of the queue and into eligibility
        release(su, world, no_release, datetime(2030, 1, 1, tzinfo=timezone.utc))
        after = principal.purge_expired(world.org, "call_set", now=FAR)
        ret_conn.commit()
        assert after.tombstoned_ids == [waiting] and after.unresolved_ids == [orphan]

    def test_a_failure_after_the_redaction_rolls_everything_back(self, su, dao, world, ret_conn, monkeypatch):
        cs = self._setup_purgeable(su, dao, world)
        principal = RetentionPrincipal(ret_conn)

        def boom(*args, **kwargs):
            raise RuntimeError("injected failure writing the purge audit row")

        monkeypatch.setattr(RetentionPrincipal, "_write_purge_audit_entry", boom)
        with pytest.raises(RuntimeError):
            principal.purge_expired(world.org, "call_set", now=FAR)
        ret_conn.rollback()
        assert row(
            su,
            "SELECT source_vcf IS NOT NULL, parsed_document IS NOT NULL, tombstoned_at IS NULL, content_redacted_at IS NULL FROM call_sets WHERE id = %s",
            (cs,),
        ) == [(True, True, True, True)]
        assert row(su, "SELECT bool_and(tombstoned_at IS NULL) FROM call_set_events WHERE call_set_id = %s", (cs,)) == [
            (True,)
        ]

    def test_a_redaction_without_its_audit_row_is_refused_at_commit(self, su, dao, world, ret_conn, monkeypatch):
        cs = self._setup_purgeable(su, dao, world)
        monkeypatch.setattr(RetentionPrincipal, "_write_purge_audit_entry", lambda *a, **k: None)
        principal = RetentionPrincipal(ret_conn)
        principal.purge_expired(world.org, "call_set", now=FAR)
        with pytest.raises(psycopg.Error, match="no retention_purge audit row"):
            ret_conn.commit()
        ret_conn.rollback()
        assert row(
            su, "SELECT source_vcf IS NOT NULL, content_redacted_at IS NULL FROM call_sets WHERE id = %s", (cs,)
        ) == [(True, True)]

    def test_the_application_role_cannot_redact_or_tombstone(self, su, dao, world, app_conn):
        cs = validated(su, dao, world)
        with app_conn.cursor() as cur:
            for sql in (
                "UPDATE call_sets SET source_vcf = NULL WHERE id = %s",
                "UPDATE call_sets SET tombstoned_at = now() WHERE id = %s",
                "DELETE FROM call_sets WHERE id = %s",
            ):
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    cur.execute(sql, (cs,))
                app_conn.rollback()
        assert row(su, "SELECT source_vcf IS NOT NULL FROM call_sets WHERE id = %s", (cs,)) == [(True,)]
