"""
clinical/tests/test_w134_patient_status.py
──────────────────────────────────────────
w134 — patient-visible progress, and the seven values it REFUSES to invent.

WHAT THIS FILE IS ABOUT, AND IT IS TWO DIFFERENT SHAPES OF CLAIM:

  1. THE POSITIVE HALF. Every state the surface CAN emit is driven to, through
     the real writers, and read back through the real route. A state with no
     row here is a state nobody proved.

  2. THE REFUSAL HALF, and it is the half this wave exists for. COUNTED at
     0b4bcac: the workflow declares NINETEEN state values across four state
     columns; TWELVE have a writer and SEVEN have none. A status surface built
     on a writerless value reports the same thing for ever. So there are rows
     below that prove the seven are absent -- and, because Rule 4 says an
     absence assertion passes vacuously against a surface that emits nothing at
     all, every one of them is paired with a positive twin.

RULE 2a -- THE METHOD, NOT A RAW QUERY. Every progress claim is read back
through `GET /portal/me/tests` or through `DataAccess.list_my_test_orders`.
Raw SQL appears only to SET UP (the `tests` catalogue row and the role grants,
exactly as the existing fixtures do) and, in one place, to prove what the
database actually holds so that the assertion about what the ROUTE says is
about the route and not about the row. Nothing here concludes "the patient sees
X" from a SELECT.

RULE 2c -- NO SILENT TRANSITION. `orders.state` is the wrong source and is
sitting right there. TestTheWrongSourceProducesNothing drives an order to the
state where the two sources DISAGREE -- the pipeline has run, but the column
still says `sample_awaited` -- and asserts the route's body contains none of
the column's vocabulary. A later implementer folding the column in "to make the
picture complete" fails that row.

RULE 4 -- COUNTS AND ABSENCES NEED CONTENT. No row here asserts "six stages"
and stops. Each asserts the stage NAMES and the state VALUES, by content.

RULE 8 -- AN ORACLE CAN LIVE IN EITHER CHANNEL. The new field is in the
refusal set as a set of size one over `(status, content)`, with the positive
twin that a live session is not in it.

THE ISOLATION AXES ARE TESTED ON THE NEW FIELD SPECIFICALLY, not inherited:
a derived field assembled from five extra tables is exactly where an org
predicate gets dropped from one subquery and nowhere else, and the existing
w137/w138 rows read `/portal/me`, which never touches those tables.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import uuid

import pytest

# Stage 3 follow-up: a patient sees a report only when it was released to the
# patient portal, so the fixtures that stand in for "released" release to it.
from clinical.data_access import PORTAL_RELEASE_CONSUMER  # noqa: E402

DSN = os.getenv("CLINICAL_TEST_DSN")

APP_PASSWORD = "test-app-password"
RETENTION_PASSWORD = "test-retention-password"
PASSWORD = "correct horse battery staple"

PATIENT_COOKIE = "clinical_patient_session"
CLIENT_IP = "203.0.113.9"

THE_REFUSAL = (401, json.dumps({"detail": "Authentication failed."}, separators=(",", ":")).encode())

T0 = dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc)

VCF_TEXT = (
    "##fileformat=VCFv4.2\n"
    "##assembly=GRCh38\n"
    "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tsample1\n"
    "chr1\t1000\t.\tA\tT\t30\tPASS\t.\tGT\t0/1\n"
)
RUN_DOCUMENT = {"variants": [{"chrom": "1", "pos": 1000}], "run_complete": True}

# SEVEN values were COUNTED at 0b4bcac to have no writer anywhere in non-test
# Python: in_progress, reported, closed (orders.state), returned
# (reports.state), validated, rejected, consumed (fastq_sets.state). Two of
# them -- `reported` and `closed` -- occur at all, once each, on the same line,
# as a READ predicate in cancel_order.
#
# SIX of the seven are listed here and `in_progress` is NOT, deliberately. The
# derived vocabulary uses `in_progress` as a state name of its own,
# legitimately, and it is the same eight characters as orders.state's
# writerless value -- so a substring match would fail against CORRECT code.
# That one is discriminated where the discrimination is real, by driving the
# two sources apart: TestTheWrongSourceProducesNothing.
THE_MATCHABLE_WRITERLESS_VALUES = (
    "reported",
    "closed",
    "returned",
    "validated",
    "rejected",
    "consumed",
)

# `orders.state` declares seven values. FOUR of them share no spelling with
# anything the derived vocabulary legitimately emits, so their presence in a
# response body can only mean the COLUMN reached the patient. The other three
# are excluded by name and for a stated reason, not quietly:
#   `placed`     -- collides with the pre-existing `placed_at` FIELD NAME
#   `cancelled`  -- collides with `cancelled_at`, and is a derived value too
#   `in_progress`-- is a derived state name in its own right
# Substring-matching any of those three would fail against CORRECT code. The
# discrimination that actually matters is made structurally instead, in
# TestTheWrongSourceProducesNothing: the column and the derivation are driven
# until they DISAGREE, and then both are read.
THE_ORDERS_STATE_VALUES_THAT_CANNOT_COLLIDE = (
    "draft",
    "sample_awaited",
    "reported",
    "closed",
)


# ─── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture()
def clinical_dsn(monkeypatch):
    psycopg = pytest.importorskip("psycopg", reason="psycopg not installed")
    if not DSN:
        pytest.skip("CLINICAL_TEST_DSN not set")

    from clinical.bootstrap import apply_schema, ensure_roles

    connection = psycopg.connect(DSN, autocommit=False, connect_timeout=10)
    with connection.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    ensure_roles(connection, app_password=APP_PASSWORD, retention_password=RETENTION_PASSWORD)
    apply_schema(connection)
    connection.commit()
    connection.close()

    monkeypatch.setenv("CLINICAL_DSN", DSN)
    return DSN


@pytest.fixture(autouse=True)
def reset_the_login_throttle():
    from clinical.auth_endpoints import _LOGIN_THROTTLE

    _LOGIN_THROTTLE.reset()
    yield
    _LOGIN_THROTTLE.reset()


@pytest.fixture()
def conn(clinical_dsn):
    import psycopg

    connection = psycopg.connect(clinical_dsn, autocommit=False, connect_timeout=10)
    yield connection
    connection.close()


@pytest.fixture()
def dao(conn):
    from clinical.data_access import BcryptHasher, DataAccess

    return DataAccess(conn, password_hasher=BcryptHasher(rounds=4))


@pytest.fixture()
def client(clinical_dsn):
    from fastapi.testclient import TestClient

    from clinical.app import app

    return TestClient(app, client=(CLIENT_IP, 45678))


def _staff(dao, conn, org_id, email):
    """
    One account holding every role its organisation's work needs.

    The same shape `test_record_pipeline_result.py`'s fixture uses and for the
    same stated reason: this account IS the whole staff of its organisation, it
    orders, receives, runs and signs, so it holds the roles for the work it
    performs. Roles are additive; nothing here relaxes a gate.
    """
    user_id = dao.create_user(org_id, email, PASSWORD)
    with conn.cursor() as cur:
        for role in ("Administrator", "Auditor", "Orderer", "Lab technician", "Interpreter"):
            cur.execute(
                "INSERT INTO role_assignments (user_id, org_id, role, assigned_by, assigned_at) "
                "VALUES (%s, %s, %s, %s, %s)",
                (user_id, org_id, role, user_id, T0),
            )
        cur.execute(
            "INSERT INTO role_assignments (user_id, org_id, role, assigned_by, assigned_at, basis) "
            "VALUES (%s, %s, 'Approver', %s, %s, %s)",
            (user_id, org_id, user_id, T0, "w134 test fixture: signs the reports it interprets"),
        )
    conn.commit()
    return dao.login(email, org_id, PASSWORD)


def _catalogue_test(conn, org_id):
    test_id = uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tests (test_id, org_id, name, assembly, status, created_at) VALUES (%s, %s, %s, %s, %s, %s)",
            (test_id, org_id, "Panel", "GRCh38", "active", T0),
        )
    conn.commit()
    return test_id


@pytest.fixture()
def world(dao, conn):
    """
    TWO organisations. In each: one staff account, TWO enrolled patients, and a
    catalogue test.

    TWO OF EVERYTHING, for the reason w137's fixture gives -- every question
    here is "did it reach the RIGHT one", and one of anything cannot answer it.
    THE SAME PATIENT EMAIL EXISTS IN BOTH ORGANISATIONS, because
    `patient_logins_org_email_unique` is scoped to the organisation and a
    tenant test built on two distinct addresses would pass against a lookup
    keyed on the address alone.
    """
    orgs = {}
    for tag, name in (("a", "Org A"), ("b", "Org B")):
        org_id = dao.create_organisation(name)
        staff = _staff(dao, conn, org_id, f"admin@{tag}.example")
        test_id = _catalogue_test(conn, org_id)

        first = dao.create_patient(staff, f"First {tag.upper()}", dt.date(1990, 1, 1), "F")
        second = dao.create_patient(staff, f"Second {tag.upper()}", dt.date(1985, 5, 5), "M")
        first_login = dao.enrol_patient_login(staff, first, "patient@shared.example", PASSWORD)
        second_login = dao.enrol_patient_login(staff, second, f"second@{tag}.example", PASSWORD)

        orgs[tag] = {
            "org_id": org_id,
            "staff": staff,
            "test_id": test_id,
            "first_patient": first,
            "second_patient": second,
            "first_login": first_login,
            "second_login": second_login,
            "first_email": "patient@shared.example",
            "second_email": f"second@{tag}.example",
        }
    conn.commit()
    return orgs


@pytest.fixture()
def vcf(tmp_path):
    path = tmp_path / "run.vcf"
    path.write_text(VCF_TEXT, encoding="utf-8")
    return str(path), hashlib.sha256(path.read_bytes()).hexdigest()


# ─── driving the workflow, through the real writers only ─────────────────────


def _order(dao, org, patient_id):
    """A draft order. create_order is the only writer of orders.state='draft'."""
    consent_id = dao.record_consent(org["staff"], patient_id, "testing")
    return dao.create_order(
        org["staff"],
        patient_id=patient_id,
        test_id=org["test_id"],
        required_scope="testing",
        consent_id=consent_id,
        priority="routine",
        clinical_indication="w134",
    )


def _advance(dao, org, patient_id, upto, vcf=None):
    """
    Drive ONE order as far as `upto`, through the REAL writers and no others.

    The stop points are exactly the observable transitions counted in the
    audit: there is no stop point between `analysis` and `review` because
    `record_pipeline_result` commits the sequencing run, the VCF, the
    interpretation AND the draft report in one transaction, which is the
    finding that made `analysis` one stage instead of two.
    """
    staff = org["staff"]
    order_id = _order(dao, org, patient_id)
    if upto == "draft":
        return order_id

    dao.place_order(staff, order_id)
    if upto == "placed":
        return order_id
    if upto == "cancelled":
        dao.cancel_order(staff, order_id)
        return order_id

    sample_id = dao.receive_sample(staff, order_id, "blood")
    if upto == "sample":
        return order_id
    if upto == "qc_failed":
        dao.record_qc(staff, sample_id, "failed")
        return order_id

    dao.record_qc(staff, sample_id, "passed")
    if upto == "qc_passed":
        return order_id

    vcf_path, vcf_hash = vcf
    system = dao._create_system_session(org["org_id"])
    result = dao.record_pipeline_result(system, sample_id, vcf_path, vcf_hash, RUN_DOCUMENT)
    if upto == "analysis":
        return order_id

    dao.record_sole_signatory_claim(
        staff,
        interpretation_id=result.interpretation_id,
        report_id=result.report_id,
        reason="w134 fixture: one signatory concurs",
    )
    dao.submit_for_review(staff, result.report_id)
    if upto == "under_review":
        return order_id

    dao.approve_report(staff, result.report_id)
    if upto == "approved":
        return order_id

    dao._release_report(staff, result.report_id, consumer=PORTAL_RELEASE_CONSUMER, actor_id=staff.user_id)
    if upto == "released":
        return order_id

    raise AssertionError(f"unknown stop point {upto!r}")


def _portal_login(client, org_id, email, password=PASSWORD):
    from clinical.auth_endpoints import _LOGIN_THROTTLE

    _LOGIN_THROTTLE.reset()
    response = client.post("/portal/login", json={"email": email, "org_id": str(org_id), "password": password})
    assert response.status_code == 204, response.text
    header = response.headers.get_list("set-cookie")[0]
    return header.split("=", 1)[1].split(";", 1)[0]


def _staff_login(client, org_id, email):
    from clinical.auth_endpoints import _LOGIN_THROTTLE

    _LOGIN_THROTTLE.reset()
    response = client.post("/auth/login", json={"email": email, "org_id": str(org_id), "password": PASSWORD})
    assert response.status_code == 204, response.text
    header = response.headers.get_list("set-cookie")[0]
    return header.split("=", 1)[1].split(";", 1)[0]


def _stages_over_http(client, cookie):
    """
    {order_id: {stage: state}} as the ROUTE reports it.

    Through the real app, because the subject of this file is what a patient
    is told, and an in-process call to the data layer is not that.
    """
    response = client.get("/portal/me/tests", cookies={PATIENT_COOKIE: cookie})
    assert response.status_code == 200, response.text
    return {o["order_id"]: {s["stage"]: s["state"] for s in o["stages"]} for o in response.json()}


# ─── 1. every exposed state, driven to and read back ─────────────────────────


class TestEveryExposedStateIsReachedByARealWriter:
    """
    ONE ROW PER EXPOSED STATE. Each drives the workflow through the real
    methods to exactly that point and asserts the whole six-stage picture by
    CONTENT -- not "the order stage advanced", but every stage's value, so a
    change that advances one stage by accidentally advancing another fails.
    """

    def test_a_draft_order_is_order_in_progress_and_nothing_else_has_started(self, client, dao, conn, world):
        order_id = _advance(dao, world["a"], world["a"]["first_patient"], "draft")
        conn.commit()
        cookie = _portal_login(client, world["a"]["org_id"], world["a"]["first_email"])
        assert _stages_over_http(client, cookie)[str(order_id)] == {
            "order": "in_progress",
            "sample": "not_started",
            "qc": "not_started",
            "analysis": "not_started",
            "review": "not_started",
            "report": "not_started",
        }

    def test_a_placed_order_completes_the_order_stage_only(self, client, dao, conn, world):
        order_id = _advance(dao, world["a"], world["a"]["first_patient"], "placed")
        conn.commit()
        cookie = _portal_login(client, world["a"]["org_id"], world["a"]["first_email"])
        assert _stages_over_http(client, cookie)[str(order_id)] == {
            "order": "completed",
            "sample": "not_started",
            "qc": "not_started",
            "analysis": "not_started",
            "review": "not_started",
            "report": "not_started",
        }

    def test_a_cancelled_order_says_cancelled_and_does_not_say_completed(self, client, dao, conn, world):
        order_id = _advance(dao, world["a"], world["a"]["first_patient"], "cancelled")
        conn.commit()
        cookie = _portal_login(client, world["a"]["org_id"], world["a"]["first_email"])
        stages = _stages_over_http(client, cookie)[str(order_id)]
        assert stages["order"] == "cancelled", stages
        # The order WAS placed before it was cancelled, so a derivation that
        # checked placed_at first would say `completed` here. Cancellation
        # dominating is the assertion.
        assert stages["order"] != "completed", stages

    def test_a_received_sample_completes_the_sample_stage_and_starts_qc(self, client, dao, conn, world):
        order_id = _advance(dao, world["a"], world["a"]["first_patient"], "sample")
        conn.commit()
        cookie = _portal_login(client, world["a"]["org_id"], world["a"]["first_email"])
        assert _stages_over_http(client, cookie)[str(order_id)] == {
            "order": "completed",
            "sample": "completed",
            "qc": "in_progress",
            "analysis": "not_started",
            "review": "not_started",
            "report": "not_started",
        }

    def test_a_passed_qc_completes_the_qc_stage(self, client, dao, conn, world):
        order_id = _advance(dao, world["a"], world["a"]["first_patient"], "qc_passed")
        conn.commit()
        cookie = _portal_login(client, world["a"]["org_id"], world["a"]["first_email"])
        stages = _stages_over_http(client, cookie)[str(order_id)]
        assert stages["qc"] == "completed", stages
        assert stages["analysis"] == "not_started", stages

    def test_a_failed_qc_says_failed_and_never_completed(self, client, dao, conn, world):
        """
        THE ROW WHERE A TRANSPOSED ARGUMENT WOULD KILL SOMEBODY'S AFTERNOON.
        `record_qc` validates its argument to exactly {passed, failed}; both
        have writers; both must be distinguishable at the route.
        """
        order_id = _advance(dao, world["a"], world["a"]["first_patient"], "qc_failed")
        conn.commit()
        cookie = _portal_login(client, world["a"]["org_id"], world["a"]["first_email"])
        stages = _stages_over_http(client, cookie)[str(order_id)]
        assert stages["qc"] == "failed", stages
        assert stages["qc"] != "completed", stages

    def test_a_recorded_pipeline_result_completes_analysis_and_starts_review(self, client, dao, conn, world, vcf):
        """
        ONE TRANSACTION, THREE STAGES MOVE. This is the finding that shaped the
        whole surface: the sequencing run, the VCF, the interpretation and the
        draft report are one commit, so `analysis` completes and `review`
        starts in the same observable step and there is no state in between.
        """
        order_id = _advance(dao, world["a"], world["a"]["first_patient"], "analysis", vcf=vcf)
        conn.commit()
        cookie = _portal_login(client, world["a"]["org_id"], world["a"]["first_email"])
        assert _stages_over_http(client, cookie)[str(order_id)] == {
            "order": "completed",
            "sample": "completed",
            "qc": "completed",
            "analysis": "completed",
            "review": "in_progress",
            "report": "not_started",
        }

    def test_a_submitted_report_is_still_review_in_progress(self, client, dao, conn, world, vcf):
        """
        draft and under_review are BOTH `in_progress`, deliberately. This row
        pins that collapse so it is a decision somebody has to reverse on
        purpose, not a gap.
        """
        order_id = _advance(dao, world["a"], world["a"]["first_patient"], "under_review", vcf=vcf)
        conn.commit()
        cookie = _portal_login(client, world["a"]["org_id"], world["a"]["first_email"])
        stages = _stages_over_http(client, cookie)[str(order_id)]
        assert stages["review"] == "in_progress", stages
        assert stages["report"] == "not_started", stages

    def test_an_approved_report_completes_review_and_does_NOT_complete_report(self, client, dao, conn, world, vcf):
        """
        THE SAFETY ROW. Approved is not released. A derivation that treated
        approval as issue would tell a patient a report had been issued when no
        release event exists -- and `require_release`/`_release_report` are the
        controls that decide that, not this surface.
        """
        order_id = _advance(dao, world["a"], world["a"]["first_patient"], "approved", vcf=vcf)
        conn.commit()
        cookie = _portal_login(client, world["a"]["org_id"], world["a"]["first_email"])
        stages = _stages_over_http(client, cookie)[str(order_id)]
        assert stages["review"] == "completed", stages
        assert stages["report"] == "not_started", stages

    def test_a_released_report_completes_the_report_stage(self, client, dao, conn, world, vcf):
        """
        THE FULL PICTURE, and the positive twin for every `not_started` above.
        Without this row a derivation that returned `not_started` for
        everything would satisfy most of this class.
        """
        order_id = _advance(dao, world["a"], world["a"]["first_patient"], "released", vcf=vcf)
        conn.commit()
        cookie = _portal_login(client, world["a"]["org_id"], world["a"]["first_email"])
        assert _stages_over_http(client, cookie)[str(order_id)] == {
            "order": "completed",
            "sample": "completed",
            "qc": "completed",
            "analysis": "completed",
            "review": "completed",
            "report": "completed",
        }

    def test_the_six_stages_are_named_and_ordered(self, client, dao, conn, world):
        """
        Asserted by CONTENT and in ORDER, because a patient reads the list as a
        timeline. A set comparison would pass against a shuffled list.
        """
        from clinical.data_access import PORTAL_STAGES

        _advance(dao, world["a"], world["a"]["first_patient"], "draft")
        conn.commit()
        cookie = _portal_login(client, world["a"]["org_id"], world["a"]["first_email"])
        response = client.get("/portal/me/tests", cookies={PATIENT_COOKIE: cookie})
        names = [s["stage"] for s in response.json()[0]["stages"]]
        assert names == list(PORTAL_STAGES)
        assert names == ["order", "sample", "qc", "analysis", "review", "report"]


# ─── 2. the refusals: the seven writerless values, and the wrong source ──────


class TestNoWriterlessValueIsEverEmitted:
    """
    COUNTED at 0b4bcac: seven declared values have no writer. None may appear
    in a patient's response body at any point in the workflow.

    WHY `in_progress` IS NOT CHECKED AS A BARE SUBSTRING. The derived
    vocabulary uses `in_progress` as a STATE NAME of its own, legitimately, and
    the collision is real: `orders.state`'s writerless `in_progress` and the
    derived `in_progress` are the same eight characters. Substring-matching it
    would make this row fail against correct code, so the discrimination is
    made where it is actually meaningful -- TestTheWrongSourceProducesNothing
    proves the derived value is NOT the column's, by driving the two apart.
    """

    def test_no_writerless_value_appears_at_any_point_in_the_workflow(self, client, dao, conn, world, vcf):
        """
        Every stop point, one body each, all six checked. An absence row --
        paired with the positive twin below, which the broken state fails.
        """
        stops = ("draft", "placed", "cancelled", "sample", "qc_failed", "qc_passed", "analysis", "released")
        assert len(stops) == 8, "the stop list shrank -- this row would prove less than it claims"

        for stop in stops:
            _advance(dao, world["a"], world["a"]["first_patient"], stop, vcf=vcf)
        conn.commit()

        cookie = _portal_login(client, world["a"]["org_id"], world["a"]["first_email"])
        body = client.get("/portal/me/tests", cookies={PATIENT_COOKIE: cookie}).content.decode()

        assert len(THE_MATCHABLE_WRITERLESS_VALUES) == 6, "six of the seven; see the constant's note"
        for value in THE_MATCHABLE_WRITERLESS_VALUES:
            assert value not in body, (value, body)

    def test_the_positive_twin_the_body_is_not_empty_and_carries_real_values(self, client, dao, conn, world, vcf):
        """
        THE ROW THAT DOES THE OPPOSITE JOB. "None of the seven appears" is
        satisfied perfectly by a route that returns `[]`, which is the broken
        state. This asserts the body carries eight orders and the terminal
        picture for the released one.
        """
        order_ids = {}
        for stop in ("draft", "placed", "cancelled", "sample", "qc_failed", "qc_passed", "analysis", "released"):
            order_ids[stop] = str(_advance(dao, world["a"], world["a"]["first_patient"], stop, vcf=vcf))
        conn.commit()

        cookie = _portal_login(client, world["a"]["org_id"], world["a"]["first_email"])
        stages = _stages_over_http(client, cookie)
        assert len(stages) == 8, stages
        assert stages[order_ids["released"]]["report"] == "completed", stages
        assert stages[order_ids["qc_failed"]]["qc"] == "failed", stages
        assert stages[order_ids["cancelled"]]["order"] == "cancelled", stages

    def test_every_emitted_value_is_in_the_declared_vocabulary(self, client, dao, conn, world, vcf):
        """
        RULE 4, the structural half: not a count, a SUBSET claim over content.
        A value invented in a later edit and not added to PORTAL_STAGE_STATES
        fails here, wherever in the workflow it is emitted.
        """
        from clinical.data_access import PORTAL_STAGES, PORTAL_STAGE_STATES

        for stop in ("draft", "placed", "cancelled", "sample", "qc_failed", "qc_passed", "analysis", "released"):
            _advance(dao, world["a"], world["a"]["first_patient"], stop, vcf=vcf)
        conn.commit()

        cookie = _portal_login(client, world["a"]["org_id"], world["a"]["first_email"])
        emitted_states, emitted_stages = set(), set()
        for per_order in _stages_over_http(client, cookie).values():
            emitted_stages |= set(per_order)
            emitted_states |= set(per_order.values())

        assert emitted_stages == set(PORTAL_STAGES), emitted_stages
        assert emitted_states <= set(PORTAL_STAGE_STATES), emitted_states
        # The positive half: this run drives every one of them, so a subset
        # claim alone cannot pass vacuously.
        assert emitted_states == {"not_started", "in_progress", "completed", "failed", "cancelled"}, emitted_states

    def test_the_sample_and_analysis_stages_are_NEVER_in_progress(self, client, dao, conn, world, vcf):
        """
        THE TWO REFUSALS THE BRIEF ASKED FOR BY NAME. `sample` has no
        in-progress because receive_sample writes collected_at and received_at
        in the same INSERT; `analysis` has none because record_pipeline_result
        commits four rows in one transaction. Neither is synthesised, at any
        point in the workflow.
        """
        for stop in ("draft", "placed", "cancelled", "sample", "qc_failed", "qc_passed", "analysis", "released"):
            _advance(dao, world["a"], world["a"]["first_patient"], stop, vcf=vcf)
        conn.commit()

        cookie = _portal_login(client, world["a"]["org_id"], world["a"]["first_email"])
        observed = _stages_over_http(client, cookie)
        assert observed, "vacuous: no orders came back"
        for order_id, per_order in observed.items():
            assert per_order["sample"] != "in_progress", (order_id, per_order)
            assert per_order["analysis"] != "in_progress", (order_id, per_order)
        # The positive twin: `qc` and `review`, which DO support it, are
        # observed in progress in the same run.
        assert any(p["qc"] == "in_progress" for p in observed.values()), observed
        assert any(p["review"] == "in_progress" for p in observed.values()), observed


class TestTheWrongSourceProducesNothing:
    """
    RULE 2c. `orders.state` is the other source a later implementer would
    reach for, and it is wrong: three of its values have no writer and
    `sample_awaited` is written AFTER the specimen arrives.

    THE DISCRIMINATING SETUP: an order whose pipeline has RUN. Its
    `orders.state` column still reads `sample_awaited` -- proved below out of
    the table, so the disagreement is established and not assumed -- while the
    derived picture says the analysis is complete. The two sources DISAGREE
    here, which is the only kind of state where this row proves anything.
    """

    def test_the_column_really_does_still_say_sample_awaited(self, dao, conn, world, vcf):
        """
        The premise, established rather than assumed. If a later wave gives
        `in_progress` a writer this row fails FIRST, and its message says the
        next row's premise is gone.
        """
        order_id = _advance(dao, world["a"], world["a"]["first_patient"], "released", vcf=vcf)
        conn.commit()
        with conn.cursor() as cur:
            cur.execute("SELECT state FROM orders WHERE order_id = %s", (order_id,))
            state = cur.fetchone()[0]
        conn.commit()
        assert state == "sample_awaited", (
            f"orders.state is {state!r}, not 'sample_awaited'. If a writer was added for a later "
            "value, this file's premise has changed and the derivation must be re-counted."
        )

    def test_the_column_that_is_actually_set_never_reaches_the_patient(self, client, dao, conn, world, vcf):
        """
        `sample_awaited` is the value the column ACTUALLY HOLDS for this order
        (proved by the row above), so it is the one string whose appearance in
        the body could only mean the column was transliterated. The other three
        are there to catch a transliteration of a DIFFERENT order's column in
        the same list.
        """
        order_id = _advance(dao, world["a"], world["a"]["first_patient"], "released", vcf=vcf)
        conn.commit()
        cookie = _portal_login(client, world["a"]["org_id"], world["a"]["first_email"])
        response = client.get("/portal/me/tests", cookies={PATIENT_COOKIE: cookie})
        body = response.content.decode()

        assert len(THE_ORDERS_STATE_VALUES_THAT_CANNOT_COLLIDE) == 4, "see the constant's note"
        for value in THE_ORDERS_STATE_VALUES_THAT_CANNOT_COLLIDE:
            assert value not in body, (value, body)

        # No order-level status field under ANY name. Asserted on the KEYS of
        # the order object rather than on the body text, because `state` is a
        # legitimate key INSIDE a stage and a text search cannot tell them
        # apart.
        order = response.json()[0]
        assert set(order) == {"order_id", "created_at", "placed_at", "cancelled_at", "stages"}, order

        # THE POSITIVE TWIN, in the same row because the absence above is
        # otherwise satisfied by an empty body: the derived picture says the
        # OPPOSITE of what the column says. The column says the laboratory is
        # awaiting a sample; the derivation says the sample arrived, the
        # analysis finished and the report was released.
        stages = {s["stage"]: s["state"] for s in order["stages"]}
        assert order["order_id"] == str(order_id)
        assert stages["sample"] == "completed", stages
        assert stages["analysis"] == "completed", stages
        assert stages["report"] == "completed", stages


# ─── 3. isolation, on the NEW field specifically ─────────────────────────────


class TestThePatientSeesOnlyTheirOwnProgress:
    def test_two_patients_in_one_organisation_get_two_different_pictures(self, client, dao, conn, world, vcf):
        """
        THE CONTROL IS A REAL PATIENT IN THE SAME ORGANISATION, at a DIFFERENT
        point in the workflow. Asserted on CONTENT both ways: each sees their
        own order id and their own stage values, and neither sees the other's.
        A cross-ORGANISATION control would prove nothing here -- the org
        predicate refuses that in the broken version too.
        """
        org = world["a"]
        mine = _advance(dao, org, org["first_patient"], "released", vcf=vcf)
        theirs = _advance(dao, org, org["second_patient"], "draft")
        conn.commit()

        first = _stages_over_http(client, _portal_login(client, org["org_id"], org["first_email"]))
        second = _stages_over_http(client, _portal_login(client, org["org_id"], org["second_email"]))

        assert set(first) == {str(mine)}, first
        assert set(second) == {str(theirs)}, second
        assert first[str(mine)]["report"] == "completed", first
        assert second[str(theirs)]["report"] == "not_started", second
        assert str(theirs) not in first, first
        assert str(mine) not in second, second

    def test_a_patient_id_supplied_in_any_channel_cannot_change_whose_progress_comes_back(
        self, client, dao, conn, world, vcf
    ):
        """
        The w130 question, asked of the NEW field. Every channel, naming a REAL
        same-organisation patient whose picture is DIFFERENT -- so an
        implementation that honoured the supplied id would return a visibly
        different answer rather than an error.
        """
        org = world["a"]
        mine = str(_advance(dao, org, org["first_patient"], "released", vcf=vcf))
        theirs = str(_advance(dao, org, org["second_patient"], "draft"))
        conn.commit()
        cookie = _portal_login(client, org["org_id"], org["first_email"])

        attempts = {
            "query patient_id": {"params": {"patient_id": str(org["second_patient"])}},
            "query patient": {"params": {"patient": str(org["second_patient"])}},
            "query order_id": {"params": {"order_id": theirs}},
            "header": {"headers": {"X-Patient-Id": str(org["second_patient"])}},
            "body": {"content": json.dumps({"patient_id": str(org["second_patient"])})},
        }
        assert len(attempts) == 5, "the channel list shrank"

        for name, kwargs in attempts.items():
            # `client.request`, not `client.get`, because one of the channels
            # is a BODY on a GET -- which `get()` will not send, and which a
            # naive handler might still read.
            response = client.request("GET", "/portal/me/tests", cookies={PATIENT_COOKIE: cookie}, **kwargs)
            assert response.status_code == 200, (name, response.text)
            got = {o["order_id"]: {s["stage"]: s["state"] for s in o["stages"]} for o in response.json()}
            assert set(got) == {mine}, (name, got)
            assert got[mine]["report"] == "completed", (name, got)

    def test_a_patient_from_another_organisation_sees_only_their_own(self, client, dao, conn, world, vcf):
        """
        THE SAME EMAIL IN BOTH ORGANISATIONS. A lookup keyed on the address
        alone, or a subquery that lost its org predicate, returns the other
        organisation's order here and passes every same-org row above.
        """
        a_order = str(_advance(dao, world["a"], world["a"]["first_patient"], "released", vcf=vcf))
        b_order = str(_advance(dao, world["b"], world["b"]["first_patient"], "placed"))
        conn.commit()

        in_a = _stages_over_http(client, _portal_login(client, world["a"]["org_id"], "patient@shared.example"))
        in_b = _stages_over_http(client, _portal_login(client, world["b"]["org_id"], "patient@shared.example"))

        assert set(in_a) == {a_order}, in_a
        assert set(in_b) == {b_order}, in_b
        assert in_a[a_order]["analysis"] == "completed", in_a
        assert in_b[b_order]["analysis"] == "not_started", in_b

    def test_progress_is_not_an_existence_oracle_as_a_set_of_size_one(self, client, dao, conn, world, vcf):
        """
        THE ORACLE ROW, as a SET OF SIZE ONE over `(status, content)` and not
        as a case-by-case equality -- Rule 8's form, applied to the SUCCESS
        channel rather than the refusal one, because that is where an oracle
        would live here.

        THE QUESTION: can a patient tell the difference between work that
        belongs to somebody else and work that does not exist? Three shapes of
        the same request, from a patient with no orders of her own:

          * naming ANOTHER REAL PATIENT'S real, released order
          * naming an order id that exists NOWHERE
          * naming an order belonging to ANOTHER ORGANISATION entirely
          * naming nothing at all

        All four must be ONE answer. They are, and the reason is structural
        rather than defensive -- the route has no channel in which to name an
        order -- but the test is what proves the structure was not quietly
        given one.

        THE POSITIVE TWIN is in the same row, because a set of size one is
        satisfied perfectly by a route that returns the same thing to
        EVERYBODY: the patient who does own that order gets a visibly
        different answer, asserted on content.
        """
        org = world["a"]
        theirs = str(_advance(dao, org, org["second_patient"], "released", vcf=vcf))
        elsewhere = str(_advance(dao, world["b"], world["b"]["first_patient"], "released", vcf=vcf))
        conn.commit()

        cookie = _portal_login(client, org["org_id"], org["first_email"])
        cases = {
            "another real patient's real released order": {"order_id": theirs},
            "an order id that exists nowhere": {"order_id": str(uuid.uuid4())},
            "an order in another organisation": {"order_id": elsewhere},
            "naming nothing at all": {},
        }
        assert len(cases) == 4, "the case list shrank -- this row would prove less than it claims"

        observed = {}
        for name, params in cases.items():
            response = client.get("/portal/me/tests", cookies={PATIENT_COOKIE: cookie}, params=params)
            observed[name] = (response.status_code, response.content)

        assert len(set(observed.values())) == 1, observed
        assert set(observed.values()) == {(200, b"[]")}, observed

        # THE POSITIVE TWIN. Without it, a route answering `[]` to everyone --
        # including the owner -- satisfies the set above completely.
        twin = client.get(
            "/portal/me/tests",
            cookies={PATIENT_COOKIE: _portal_login(client, org["org_id"], org["second_email"])},
        )
        assert (twin.status_code, twin.content) not in set(observed.values()), twin.content
        assert [o["order_id"] for o in twin.json()] == [theirs], twin.text
        assert {s["stage"]: s["state"] for s in twin.json()[0]["stages"]}["report"] == "completed"


class TestTheProgressRouteRefusesEveryBadCredentialIdentically:
    def test_every_refusal_on_the_progress_route_is_one_pair(self, client, dao, conn, world, vcf):
        """
        RULE 8, a SET OF SIZE ONE over `(status, content)`, on the route that
        now carries the new field. w138 put this route in a set for four
        credential states; this adds the session-lifecycle ones and the
        revoked credential, and it matters more now: the handler reads five
        more tables, and a failure in any of them must not produce a
        distinguishable answer.
        """
        org = world["a"]
        _advance(dao, org, org["first_patient"], "released", vcf=vcf)
        conn.commit()

        revoked = _portal_login(client, org["org_id"], org["first_email"])
        logged_out = _portal_login(client, org["org_id"], org["second_email"])
        client.post("/portal/logout", cookies={PATIENT_COOKIE: logged_out})
        staff_cookie = _staff_login(client, org["org_id"], "admin@a.example")
        other_org = _portal_login(client, world["b"]["org_id"], "patient@shared.example")
        client.post("/portal/logout", cookies={PATIENT_COOKIE: other_org})
        dao.set_patient_login_disabled(org["staff"], org["first_login"], True)
        conn.commit()

        cases = {
            "no credential at all": {},
            "a malformed id": {PATIENT_COOKIE: "not-a-uuid"},
            "the empty string": {PATIENT_COOKIE: ""},
            "a well-formed id that names nothing": {PATIENT_COOKIE: str(uuid.uuid4())},
            "a genuine STAFF session id": {PATIENT_COOKIE: staff_cookie},
            "a session ended by logout": {PATIENT_COOKIE: logged_out},
            "another organisation's ended session": {PATIENT_COOKIE: other_org},
            "a session ended by revocation": {PATIENT_COOKIE: revoked},
        }
        assert len(cases) == 8, "the case list shrank -- this row would prove less than it claims"

        observed = {}
        for name, cookies in cases.items():
            response = client.get("/portal/me/tests", cookies=cookies)
            observed[name] = (response.status_code, response.content)

        assert len(set(observed.values())) == 1, observed
        assert set(observed.values()) == {THE_REFUSAL}, observed

    def test_an_unrelated_patients_session_survives_the_revocation_and_still_reports_progress(
        self, client, dao, conn, world, vcf
    ):
        """
        THE BYSTANDER ROW, and it is a different claim from the twin below.

        A revocation that terminated every patient session in the organisation
        satisfies the set-of-size-one row above COMPLETELY -- that is the
        broken state, and only this row catches it. Asserted on CONTENT: the
        bystander's own order id and her own six stages, after her
        organisation-mate's credential was revoked through the real
        Administrator-gated method.
        """
        org = world["a"]
        mine = str(_advance(dao, org, org["first_patient"], "draft"))
        theirs = str(_advance(dao, org, org["second_patient"], "released", vcf=vcf))
        conn.commit()

        revoked = _portal_login(client, org["org_id"], org["first_email"])
        bystander = _portal_login(client, org["org_id"], org["second_email"])
        assert revoked != bystander

        dao.set_patient_login_disabled(org["staff"], org["first_login"], True)
        conn.commit()

        gone = client.get("/portal/me/tests", cookies={PATIENT_COOKIE: revoked})
        assert (gone.status_code, gone.content) == THE_REFUSAL, gone.content

        still = client.get("/portal/me/tests", cookies={PATIENT_COOKIE: bystander})
        assert still.status_code == 200, still.text
        assert [o["order_id"] for o in still.json()] == [theirs], still.text
        assert mine not in still.content.decode(), still.text
        assert {s["stage"]: s["state"] for s in still.json()[0]["stages"]} == {
            "order": "completed",
            "sample": "completed",
            "qc": "completed",
            "analysis": "completed",
            "review": "completed",
            "report": "completed",
        }

    def test_the_positive_twin_a_live_session_is_not_in_that_set(self, client, dao, conn, world, vcf):
        """Without this, a route that refused everything would satisfy the row
        above perfectly -- and would also emit no progress at all."""
        org = world["a"]
        order_id = str(_advance(dao, org, org["first_patient"], "released", vcf=vcf))
        conn.commit()
        cookie = _portal_login(client, org["org_id"], org["first_email"])
        response = client.get("/portal/me/tests", cookies={PATIENT_COOKIE: cookie})
        assert (response.status_code, response.content) != THE_REFUSAL
        assert response.status_code == 200, response.text
        assert {s["stage"]: s["state"] for s in response.json()[0]["stages"]}["report"] == "completed"
        assert response.json()[0]["order_id"] == order_id


# ─── 4. the data-layer method itself, and the derivation as a function ───────


class TestTheDataLayerMethodIsTheOneThatEnforcesIt:
    """
    RULE 2a at the layer below the route. `list_my_test_orders` is where the
    predicates live, so it is asserted directly too -- a route test alone
    cannot tell a correct method from a correct dependency in front of a
    permissive one.
    """

    def test_a_staff_session_is_still_refused_by_the_progress_reader(self, dao, world, vcf):
        from clinical.data_access import AuthorizationError

        _advance(dao, world["a"], world["a"]["first_patient"], "released", vcf=vcf)
        with pytest.raises(AuthorizationError):
            dao.list_my_test_orders(world["a"]["staff"])

    def test_the_method_returns_one_row_per_order_not_one_per_sample(self, dao, conn, world, vcf):
        """
        THE FAN-OUT ROW. Two samples on one order. A plain LEFT JOIN to
        `samples` would return that order TWICE, and the patient would see one
        test listed twice with the same id. The lateral aggregate is what stops
        it and this is the only row that can tell.
        """
        org = world["a"]
        order_id = _order(dao, org, org["first_patient"])
        dao.place_order(org["staff"], order_id)
        first_sample = dao.receive_sample(org["staff"], order_id, "blood")
        second_sample = dao.receive_sample(org["staff"], order_id, "saliva")
        assert first_sample != second_sample
        dao.record_qc(org["staff"], first_sample, "passed")
        conn.commit()

        session = dao.patient_login(org["first_email"], org["org_id"], PASSWORD)
        rows = dao.list_my_test_orders(session)
        assert [r["order_id"] for r in rows] == [order_id], rows

    def test_a_failed_sibling_sample_dominates_a_passed_one(self, dao, conn, world):
        """
        The precedence, asserted where it has a consequence. One sample passed,
        one failed, one order. `failed` must win: hiding a failed specimen
        behind a passed sibling is the status surface lying in the one place it
        must not.
        """
        org = world["a"]
        order_id = _order(dao, org, org["first_patient"])
        dao.place_order(org["staff"], order_id)
        good = dao.receive_sample(org["staff"], order_id, "blood")
        bad = dao.receive_sample(org["staff"], order_id, "saliva")
        dao.record_qc(org["staff"], good, "passed")
        dao.record_qc(org["staff"], bad, "failed")
        conn.commit()

        session = dao.patient_login(org["first_email"], org["org_id"], PASSWORD)
        stages = {s["stage"]: s["state"] for s in dao.list_my_test_orders(session)[0]["stages"]}
        assert stages["qc"] == "failed", stages

    def test_the_positive_twin_two_passed_samples_complete_qc(self, dao, conn, world):
        org = world["a"]
        order_id = _order(dao, org, org["first_patient"])
        dao.place_order(org["staff"], order_id)
        for kind in ("blood", "saliva"):
            dao.record_qc(org["staff"], dao.receive_sample(org["staff"], order_id, kind), "passed")
        conn.commit()

        session = dao.patient_login(org["first_email"], org["org_id"], PASSWORD)
        stages = {s["stage"]: s["state"] for s in dao.list_my_test_orders(session)[0]["stages"]}
        assert stages["qc"] == "completed", stages


class TestTheDerivationIsAPureFunction:
    """
    The derivation has no session, no connection and no clock, and these rows
    are what keeps it that way: they call it with plain integers. A later edit
    that reaches for the database from inside it fails to import here.

    These are a SUPPLEMENT to the behavioural rows above, never a substitute --
    a pure-function test cannot tell whether the counts handed to it came from
    the right patient.
    """

    def _derive(self, **overrides):
        from clinical.data_access import derive_order_stages

        args = dict(
            placed_at=None,
            cancelled_at=None,
            sample_count=0,
            qc_pending=0,
            qc_passed=0,
            qc_failed=0,
            interpretation_count=0,
            report_count=0,
            reports_signed_off=0,
            reports_released=0,
        )
        args.update(overrides)
        return {s["stage"]: s["state"] for s in derive_order_stages(**args)}

    def test_nothing_at_all_is_an_order_in_progress_and_five_not_started(self):
        assert self._derive() == {
            "order": "in_progress",
            "sample": "not_started",
            "qc": "not_started",
            "analysis": "not_started",
            "review": "not_started",
            "report": "not_started",
        }

    def test_cancellation_beats_placement(self):
        assert self._derive(placed_at=T0, cancelled_at=T0)["order"] == "cancelled"
        assert self._derive(placed_at=T0)["order"] == "completed"

    def test_a_released_report_is_also_a_signed_off_one(self):
        derived = self._derive(report_count=1, reports_signed_off=1, reports_released=1)
        assert derived["review"] == "completed"
        assert derived["report"] == "completed"

    def test_an_approved_report_does_not_release_itself(self):
        derived = self._derive(report_count=1, reports_signed_off=1, reports_released=0)
        assert derived["review"] == "completed"
        assert derived["report"] == "not_started"

    def test_it_takes_no_session_and_touches_no_connection(self):
        """
        Structural, and asserted on the SIGNATURE rather than on a name: a
        parameter called `session` or `dao` is how this stops being a pure
        derivation and starts being a second place that reads the database.
        """
        import inspect

        from clinical.data_access import derive_order_stages

        parameters = inspect.signature(derive_order_stages).parameters
        assert "session" not in parameters
        assert "dao" not in parameters
        assert "self" not in parameters
        assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in parameters.values()), parameters
