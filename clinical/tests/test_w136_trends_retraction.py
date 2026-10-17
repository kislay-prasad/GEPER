"""
clinical/tests/test_w136_trends_retraction.py
─────────────────────────────────────────────
w136 — THE PATIENT REPORT SERIES, AND THE RECONCILIATION WITH RETRACTION.

WHAT THIS FILE IS ABOUT, STATED BEFORE THE FIRST ASSERTION, because the
surface under test is defined as much by what it refuses as by what it does.

THE HELD TRENDS WORK IS NOT WHAT IS TESTED HERE. `w127-trends` (tip 861e59f,
off master 9bc3abd, NOT in this branch's ancestry, read only via `git show`)
serves `GET /patients/{patient_id}/variant-trends` — a STAFF route, whose
dependency is `clinical.endpoints._get_session`, the staff session — carrying
a series of ACMG CLASSIFICATIONS read out of raw `interpretations.run_document`.
It was not merged, not ported and not rebuilt. §1 of this file asserts, as a
measurement, that none of it is served here.

WHAT IS SERVED IS `GET /portal/me/trends`: the authenticated patient's own
RELEASED reports as a series, in first-delivery order, with the amendment edge
between them. Its node key is a `report_id`.

WHY A REPORT AND NOT A VARIANT — THE WHOLE RECONCILIATION IN ONE SENTENCE.
Retraction, on the held `w122-retraction` (a4907d3), attaches to a report
(`report_retractions.report_id`). One interpretation carries MANY reports — an
amendment is a new `reports` row on the SAME `interpretation_id` — so an
interpretation-keyed trend point, which is what w127 produces, has no
retraction status at all. Human ruling E3's bargain is that the retraction
TRAVELS WITH THE DOCUMENT; a series structurally incapable of carrying it has
taken the benefit of that bargain without paying it.

AND NOTHING IS HIDDEN, which is the other half. E7: a retracted report must
not become UNFINDABLE. E3: retraction does not refuse at the release gate. So
the series applies NO retraction predicate and NO tombstone predicate — only
release — and §5 pins that.

w148: THE TABLE ARRIVED AND THE DECISION WAS MADE. Migration 0003 (2de2fef)
brought `report_retractions` into the schema; §5's measurement row fired,
naming the work it owed; and the human ruled DECISION 2 — "a trend point
originating from a retracted report REMAINS in the patient's longitudinal data
but carries an `is_retracted` flag. Do NOT expose `replacement_report_id` or
other internal retraction details." §7 is that decision, tested. No filter was
added anywhere. E4's optional replacement edge, which §5's row named as a
possibility, was refused.

WHAT THIS FILE DELIBERATELY DOES NOT TEST, AND WHY EACH IS ABSENT:
  * A RETRACTION CREATED THROUGH A WRITER. RE-MEASURED AT w156 AND THE CLAIM
    IS FIXED RATHER THAN LEFT STANDING (Rule 3): there IS one now —
    `DataAccess.retract_report`, written in w156 (WAVE 6) and Approver-gated.
    §7 still ARRANGES its rows by direct insert, which stays legitimate
    because every subject in §7 is a READER and `_retract` only puts a row in
    a table; changing them would re-test the writer in a file about readers.
    The writer-driven version of the same property — a retraction made
    through the verb and through `POST /admin/reports/{id}/retraction`, then
    read back over `/portal/me/trends` — lives in
    `test_w156_retraction_reachability.py`. §5 no longer asserts the verb's
    absence; it asserts the verb's existence AND its gate.
  * A VARIANT CLASSIFICATION ANYWHERE. Refused — §6 proves the refusal holds
    behaviourally, with a marker that cannot arrive by coincidence.
  * A PRODUCTION PATH THAT REACHES ANY OF THIS. `_release_report` has ZERO
    non-test callers at 3a482f1, so this route returns `[]` for every patient
    in this deployment, for ever. Every released state below is constructed
    the only legitimate way there is — by calling `_release_report` behind its
    own Approver gate, with an actor separately holding Approver. No row
    forges `state = 'released'` with an UPDATE.
"""

from __future__ import annotations

import ast
import datetime as dt
import hashlib
import inspect
import json
import os
import pathlib
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
STAFF_COOKIE = "clinical_session"
CLIENT_IP = "203.0.113.21"

# The one answer every refusal on this surface gives, in BOTH channels
# (Rule 8: an oracle can live in either).
THE_REFUSAL = (401, json.dumps({"detail": "Authentication failed."}, separators=(",", ":")).encode())

# The one SUCCESS answer that must not become an existence oracle either.
THE_EMPTY_SERIES = (200, b"[]")

T0 = dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc)

VCF_TEXT = (
    "##fileformat=VCFv4.2\n"
    "##assembly=GRCh38\n"
    "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tsample1\n"
    "chr1\t1000\t.\tA\tT\t30\tPASS\t.\tGT\t0/1\n"
)

# A MARKER THAT CANNOT ARRIVE BY COINCIDENCE, and it is a CLASSIFICATION
# rather than a gene name on purpose: what the held trends work would serve a
# patient is exactly this field, at exactly this JSON path, and nothing else in
# this repository spells the string.
CLASSIFICATION_MARKER = "w136-acmg-call-must-never-reach-a-patient"
RUN_DOCUMENT = {
    "variants": [
        {
            "chrom": "1",
            "pos": 1000,
            "ref": "A",
            "alt": "T",
            "candidate_interpretation": {"acmg": {"classification": CLASSIFICATION_MARKER}},
        }
    ],
    "model_version": CLASSIFICATION_MARKER,
    "run_complete": True,
}

A_UUID_THAT_NAMES_NOTHING = uuid.UUID("2c9f0a2e-7b41-4f3a-9d18-6e5b2c4a7f30")

# A RETRACTION REASON THAT CANNOT ARRIVE BY COINCIDENCE — the same device as
# CLASSIFICATION_MARKER above, for the datum w148's human decision 2 refuses to
# this surface by name.
RETRACTION_REASON_MARKER = "w148-retraction-reason-must-never-reach-a-patient"


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
    """Ten attempts per source per minute is the shipped limit and several rows
    here log in as four different principals. A 429 is not the refusal any row
    in this file is about; the limiter has its own file."""
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
    """One account holding every role its organisation's work needs — the same
    shape and the same stated reason as w134's and w135's fixtures. Roles are
    additive; nothing here relaxes a gate, and the Approver grant is what makes
    `_release_report` and `create_amendment` reachable at all."""
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
            (user_id, org_id, user_id, T0, "w136 test fixture: signs, releases and amends"),
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
    TWO organisations; in each, one staff account, TWO enrolled patients and a
    catalogue test.

    TWO OF EVERYTHING, for w137's reason: every question here is "did it reach
    the RIGHT one", and one of anything cannot answer that. THE SAME PATIENT
    EMAIL EXISTS IN BOTH ORGANISATIONS, because `patient_logins_org_email_unique`
    is scoped to the organisation and a tenant test built on two distinct
    addresses would pass against a lookup keyed on the address alone.
    """
    orgs = {}
    for tag, name in (("a", "Org A"), ("b", "Org B")):
        org_id = dao.create_organisation(name)
        staff = _staff(dao, conn, org_id, f"admin@{tag}.example")
        test_id = _catalogue_test(conn, org_id)

        first = dao.create_patient(staff, f"First {tag.upper()}", dt.date(1990, 1, 1), "F")
        second = dao.create_patient(staff, f"Second {tag.upper()}", dt.date(1985, 5, 5), "M")
        dao.enrol_patient_login(staff, first, "patient@shared.example", PASSWORD)
        dao.enrol_patient_login(staff, second, f"second@{tag}.example", PASSWORD)

        orgs[tag] = {
            "org_id": org_id,
            "staff": staff,
            "staff_email": f"admin@{tag}.example",
            "test_id": test_id,
            "first_patient": first,
            "second_patient": second,
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


def _pipeline(dao, org, patient_id, vcf, upto, tag="w136"):
    """
    Drive ONE order for `patient_id` as far as `upto`, THROUGH THE REAL WRITERS
    AND NO OTHERS, and return a dict of the ids it produced.

    `released` is reached by calling `_release_report` itself, behind its own
    `_require_role(session, "Approver")` gate and with an actor who separately
    holds Approver. THAT is what makes this a legitimate construction of a
    state no production path in this repository reaches: the state is built by
    the method that owns it, not forged by an UPDATE. (Rule 2a, from the other
    side: the setup uses the methods too, so a broken writer fails here rather
    than producing data a broken reader then agrees with.)
    """
    staff = org["staff"]
    consent_id = dao.record_consent(staff, patient_id, "testing")
    order_id = dao.create_order(
        staff,
        patient_id=patient_id,
        test_id=org["test_id"],
        required_scope="testing",
        consent_id=consent_id,
        priority="routine",
        clinical_indication=tag,
    )
    dao.place_order(staff, order_id)
    sample_id = dao.receive_sample(staff, order_id, "blood")
    dao.record_qc(staff, sample_id, "passed")

    vcf_path, vcf_hash = vcf
    system = dao._create_system_session(org["org_id"])
    result = dao.record_pipeline_result(system, sample_id, vcf_path, vcf_hash, RUN_DOCUMENT)
    out = {
        "order_id": order_id,
        "sample_id": sample_id,
        "interpretation_id": result.interpretation_id,
        "report_id": result.report_id,
    }
    if upto == "draft":
        return out

    _sign_and_submit(dao, staff, result.interpretation_id, result.report_id)
    if upto == "under_review":
        return out

    dao.approve_report(staff, result.report_id)
    if upto == "approved":
        return out

    dao._release_report(staff, result.report_id, consumer=PORTAL_RELEASE_CONSUMER, actor_id=staff.user_id)
    if upto == "released":
        return out

    raise AssertionError(f"unknown stop point {upto!r}")


def _sign_and_submit(dao, staff, interpretation_id, report_id):
    dao.record_sole_signatory_claim(
        staff,
        interpretation_id=interpretation_id,
        report_id=report_id,
        reason="w136 fixture: one signatory concurs",
    )
    dao.submit_for_review(staff, report_id)


def _amend(dao, org, run, reason="w136 fixture: the original needed correcting", release=True):
    """
    Create an amendment of `run["report_id"]` THROUGH `create_amendment`, and
    carry it to `released` through the same real writers.

    An amendment is a NEW `reports` row on the SAME `interpretation_id` — which
    is the structural fact the supersession edge rests on — so it is signed,
    submitted, approved and released exactly as the original was.
    """
    staff = org["staff"]
    amendment_report_id, _notification_id = dao.create_amendment(staff, run["report_id"], reason)
    if not release:
        return amendment_report_id
    _sign_and_submit(dao, staff, run["interpretation_id"], amendment_report_id)
    dao.approve_report(staff, amendment_report_id)
    dao._release_report(staff, amendment_report_id, consumer=PORTAL_RELEASE_CONSUMER, actor_id=staff.user_id)
    return amendment_report_id


def _tombstone(conn, org_id, report_id):
    """
    Stamp a report and its delivery records as PURGED.

    RAW SQL DELIBERATELY, AND IT IS NOT A RULE-2a BREACH. `clinical_app` is
    REVOKED from writing tombstoned_at/tombstoned_by at the database (only
    `clinical_retention` may, clinical/retention.py), so there is no method on
    DataAccess that could do this and there must never be one. This is setup;
    the assertion that follows it still reads through the route.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT user_id FROM users WHERE org_id = %s LIMIT 1", (org_id,))
        actor = cur.fetchone()[0]
        cur.execute(
            "UPDATE reports SET tombstoned_at = %s, tombstoned_by = %s WHERE org_id = %s AND id = %s",
            (T0, actor, org_id, report_id),
        )
        cur.execute(
            "UPDATE release_events SET tombstoned_at = %s, tombstoned_by = %s WHERE org_id = %s AND report_id = %s",
            (T0, actor, org_id, report_id),
        )
    conn.commit()


def _retract(conn, org_id, report_id, reason, replacement_report_id=None, from_state="released"):
    """
    Record a retraction of `report_id` BY DIRECT INSERT — ARRANGEMENT, not the
    bypass pattern, and the distinction is stated rather than left to be
    reconstructed.

    The bypass pattern is a test that builds its EXPECTATION with raw SQL and
    therefore passes while the method under test is broken. The subject of
    every row that calls this is `list_my_report_trend`, a READER, and every
    expectation below is read back through `GET /portal/me/trends`. The raw
    SQL only puts a row in a table.

    IT WAS RAW SQL BECAUSE THERE WAS NO WRITER, AND IT STAYS RAW SQL NOW
    THAT THERE IS ONE. w156 (WAVE 6) wrote `DataAccess.retract_report`, so
    the original sentence here ("`def retract_report` does not exist") is
    false and is corrected rather than left to decay. These calls are NOT
    converted to it, for a reason: the subject of every row that uses this
    helper is a READER, and `_retract` is ARRANGEMENT. Driving the arrangement
    through the writer would also import the writer's OWN preconditions —
    the Approver gate, ruling E1's state rule, E5's already-retracted refusal
    — into rows that are about none of them, and `from_state="approved"` on
    an approved-but-never-released report is exactly the arrangement E1's
    refusal would make harder to state. The writer-driven proof is a separate
    file (`test_w156_retraction_reachability.py`), which is where a writer
    belongs.

    `retracted_from_state` is CHECKed to ('approved', 'released') by ruling E1.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT user_id FROM users WHERE org_id = %s LIMIT 1", (org_id,))
        actor = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO report_retractions "
            "(org_id, report_id, retracted_at, retracted_by, reason, retracted_from_state, "
            " replacement_report_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (org_id, report_id, T0, actor, reason, from_state, replacement_report_id),
        )
    conn.commit()
    return actor


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


def _series(client, cookie, **kwargs):
    """The patient's series AS THE ROUTE REPORTS IT. An in-process call to the
    data layer is not what a patient is told (Rule 2a)."""
    response = client.get("/portal/me/trends", cookies={PATIENT_COOKIE: cookie}, **kwargs)
    assert response.status_code == 200, response.text
    return response.json()


def _served_leaves():
    """Every path the application actually serves, walking INTO the wrapper
    objects `include_router` leaves in `app.routes` (Standing Rule 9 as
    corrected: neither the decorator count nor the naive `app.routes` count is
    the answer).

    ── THIS FUNCTION DID NOT DO WHAT THIS DOCSTRING SAID. FIXED IN w152. ────
    It descended `route.routes` only. On this stack (FastAPI 0.141.1 /
    starlette 1.6.0) `include_router` leaves an `_IncludedRouter` wrapper
    whose sub-routes hang off `original_router`, NOT off `routes` — so this
    walk returned exactly TWO leaves, `/healthz` and `/readyz`, and the rows
    built on it have been vacuous since they were written: "no served path
    names a patient" was true of a surface that contained no patient route at
    all. MEASURED at `e1be7ab` before the change: `len(_served_leaves())` was
    2 on an application serving 14.

    It was found because w152 asserted that a specific administrative path
    WAS among the leaves, which is the positive-membership check Standing
    Rule 4 asks for and which this file's absence rows never had. The three
    assertions below are that check made permanent: refuse the empty answer,
    refuse the wrapper-shaped answer, and require the landmark routes from
    all three mounted routers to be present.
    """
    from clinical.app import app

    found = []

    def walk(routes):
        for route in routes:
            wrapped = getattr(route, "original_router", None)
            if wrapped is not None:
                walk(getattr(wrapped, "routes", []))
                continue
            inner = getattr(route, "routes", None)
            if inner:
                walk(inner)
                continue
            path = getattr(route, "path", None)
            methods = getattr(route, "methods", None)
            if path and methods:
                found.append((path, tuple(sorted(methods - {"HEAD", "OPTIONS"}))))

    walk(app.routes)

    assert found, "the route walk found nothing"
    assert all(path and path.startswith("/") for path, _ in found), found
    assert all(methods for _, methods in found), found
    landmarks = {"/portal/me", "/exceptions/worklist", "/auth/login", "/healthz"}
    assert landmarks <= {path for path, _ in found}, (
        f"the walk returned {sorted(path for path, _ in found)} -- it is not descending into the "
        "routers the app includes, and every absence row built on it is vacuous"
    )
    return found


# ─── 1. what the held trends branch contains, and that none of it is served ──


class TestTheHeldTrendsWorkIsNotServedHere:
    """
    THE MEASUREMENT, AS EXECUTABLE ROWS. The queue's Wave 8 warns that trends
    previously took a patient id FROM THE URL. That is true of the held
    branch's route — and the principal there is STAFF, so it is not a breach
    of the patient-identity ruling until a patient session reaches it. These
    rows assert that no patient session can.
    """

    def test_no_served_path_names_a_patient(self):
        """
        Catches: `GET /patients/{patient_id}/variant-trends` being mounted
        as-is — the exact shape Wave 8 was warned about, and the shape
        `clinical/portal_endpoints.py`'s header names by name.

        Vacuous against: a patient identifier arriving in a QUERY STRING or a
        BODY — which §3's three-channel row covers instead.

        THE EXEMPTION, w152. This class's own docstring states the property:
        a staff route naming a patient "is not a breach of the
        patient-identity ruling until a patient session reaches it. These
        rows assert that no patient session can." Until w152 no staff route
        named anything patient-shaped, so the ban could be blanket and was.

        `POST /admin/patient-logins/{patient_login_id}/disabled` names a
        CREDENTIAL — the distinction w133 made load-bearing — on the STAFF
        router, behind the STAFF session dependency. The exemption is proved
        rather than asserted: the path must actually be served, and the
        behavioural half (a live patient session id is refused on it) is
        `test_w152_admin_surface.py::TestTheUnauthenticatedCallIsRefused::
        test_a_patient_session_id_is_not_a_staff_credential`.
        """
        leaves = _served_leaves()
        assert leaves, "the route walk found nothing — this row would pass vacuously"
        assert all(path.startswith("/") for path, _ in leaves), leaves

        exempt = {
            "/admin/patient-logins/{patient_login_id}/disabled",
            # w155 (WAVE 5). The SECOND exemption, and it is the harder one to
            # justify because it names a PATIENT rather than a credential. It
            # is exempt on the same property and only on that property: it is
            # a STAFF route on the STAFF router, behind
            # `clinical.endpoints._get_session`, so no patient session can
            # reach it -- and the identifier's ORGANISATION comes from
            # `Session.org_id`, never from the request, so naming it selects a
            # row inside the caller's own tenant and can select nothing else.
            # The behavioural halves are
            # `test_w155_patient_administration.py::TestAdministratorOnlyEnrolment::
            # test_a_live_patient_session_is_not_a_staff_credential` and
            # `::TestCrossOrganisationEnrolmentIsRefused`.
            "/admin/patients/{patient_id}/logins",
            # w162. THE THIRD EXEMPTION, on exactly the same property and on
            # nothing else. `POST /clinic/patients/{patient_id}/consents` is a
            # STAFF route on the clinic router, behind
            # `clinical.endpoints._get_session`, and `record_consent` binds
            # `WHERE patient_id = %s AND org_id = %s` with `org_id` from
            # `Session.org_id` -- so naming the identifier selects a row inside
            # the caller's own tenant and can select nothing else, and a real
            # patient in another organisation is byte-identically not found.
            # The behavioural halves are in
            # `test_w162_registration_and_consent.py`:
            # `TestOnlyTheClinicianMayRecordConsent` (a patient identifier
            # tells a non-Orderer nothing) and
            # `TestTheOtherOrganisationIsNotAddressable` (the collision is
            # constructed and refused).
            "/clinic/patients/{patient_id}/consents",
        }
        served = {path for path, _ in leaves}
        assert exempt <= served, (
            f"the exemption names {sorted(exempt - served)}, which this app does not serve — "
            "a stale exemption silently widens the filter below"
        )

        offenders = [path for path, _ in leaves if "{patient" in path and path not in exempt]
        assert not offenders, f"served route(s) naming a patient in the path: {offenders}"

    def test_none_of_the_held_trends_modules_is_importable(self):
        """
        The other half: the route could not be mounted because the modules are
        not here. Three modules, named exactly as the held branch names them.

        Non-vacuity: `clinical.portal_endpoints` IS asserted importable by the
        same mechanism, so a broken import helper fails here instead of
        reporting every module absent.

        Catches: w127 being merged or cherry-picked into this branch without
        anybody revisiting the patient surface — at which point the series in
        this file and the series on that branch both exist and somebody must
        decide which one a patient sees.
        """
        import importlib.util

        assert importlib.util.find_spec("clinical.portal_endpoints") is not None, (
            "the probe is broken — clinical.portal_endpoints should exist"
        )
        for module in ("clinical.patient_endpoints", "clinical.variant_trends", "clinical.run_document_shape"):
            assert importlib.util.find_spec(module) is None, (
                f"{module} now exists. w127-trends serves a CLASSIFICATION series keyed on "
                "interpretations, from a STAFF route taking a patient id from the URL. Neither "
                "property is safe on the patient surface: see test_w136_trends_retraction.py's header."
            )

    def test_the_portal_router_still_takes_no_patient_identifier_anywhere(self):
        """
        Source-level, over the whole router rather than the one new handler —
        the failure this catches is a route nobody remembered to probe.

        Catches: any portal handler growing a `patient_id` parameter in any
        signature. Vacuous against: a handler that takes no parameter and
        leaks anyway, which every behavioural row in §3 is for.
        """
        import clinical.portal_endpoints

        tree = ast.parse(pathlib.Path(inspect.getfile(clinical.portal_endpoints)).read_text(encoding="utf-8"))
        handlers = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_")
        ]
        assert handlers, "no portal handlers found — this row would pass vacuously"
        offenders = []
        for node in handlers:
            args = node.args
            names = [a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)]
            offenders += [f"{node.name}({name})" for name in names if "patient" in name and name != "patient_session"]
        assert not offenders, f"portal handler(s) accepting a patient identifier: {offenders}"


# ─── 2. the correct patient: the series, and what it is made of ──────────────


class TestTheOwnersOwnSeries:
    def test_a_released_report_of_the_patients_own_is_a_point(self, client, dao, conn, world, vcf):
        """
        THE POSITIVE TWIN FOR THE WHOLE FILE, and it is not optional: measured
        at 3a482f1, a surface returning `[]` for everybody is what this
        deployment does in production, and it satisfies every isolation row
        here. This is the row that fails against it.

        Catches: a series that is always empty; a route that is not mounted.
        Vacuous against: every isolation leak.
        """
        org = world["a"]
        run = _pipeline(dao, org, org["first_patient"], vcf, "released")
        cookie = _portal_login(client, org["org_id"], org["first_email"])

        series = _series(client, cookie)
        assert len(series) == 1, series
        point = series[0]
        assert point["report_id"] == str(run["report_id"])
        assert point["order_id"] == str(run["order_id"])
        assert point["released_at"], point
        assert point["amends_report_id"] is None, point
        assert point["superseded_by_report_id"] is None, point

    @pytest.mark.parametrize("state", ["draft", "under_review", "approved"])
    def test_an_unreleased_report_of_the_patients_own_is_not_a_point(self, client, dao, conn, world, vcf, state):
        """
        Catches: NO RELEASE FILTER AT ALL — a draft or under_review report of
        the patient's own reaching them through the series.

        Vacuous against `approved`, AND THAT IS MEASURED RATHER THAN ASSUMED:
        w135 §11.4 found that an approved report has no `release_events` row
        either, so the lateral delivery requirement excludes it whatever the
        state predicate says. The `approved` row here is therefore honest
        coverage of the OUTCOME and no coverage of the PREDICATE; the row that
        separates the two halves is
        `test_a_state_column_forged_to_released_without_a_delivery_record_is_not_a_point`.
        """
        org = world["a"]
        _pipeline(dao, org, org["first_patient"], vcf, state)
        cookie = _portal_login(client, org["org_id"], org["first_email"])

        assert _series(client, cookie) == []

    def test_a_state_column_forged_to_released_without_a_delivery_record_is_not_a_point(
        self, client, dao, conn, world, vcf
    ):
        """
        THE ROW THAT SEPARATES THE TWO HALVES OF THE RELEASE PREDICATE, the
        only way this platform can: by driving the corrupt state the
        application's own grant really permits.

        `clinical_app` holds `GRANT UPDATE (state, approver_id, approved_at,
        content_hash) ON reports` (schema.sql:1995), so a bug — or a second
        writer — could move `state` to 'released' with no delivery ever
        recorded. A series trusting the summary column alone would show that
        report as delivered.

        Catches: the `release_events` requirement being dropped, which is the
        cheap version of this query. Vacuous against: every isolation question.
        """
        org = world["a"]
        run = _pipeline(dao, org, org["first_patient"], vcf, "approved")
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE reports SET state = 'released' WHERE org_id = %s AND id = %s",
                (org["org_id"], run["report_id"]),
            )
        conn.commit()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM release_events WHERE org_id = %s AND report_id = %s",
                (org["org_id"], run["report_id"]),
            )
            assert cur.fetchone()[0] == 0, "the forgery is broken — a delivery record exists"

        cookie = _portal_login(client, org["org_id"], org["first_email"])
        assert _series(client, cookie) == []

    def test_a_report_released_twice_is_one_point_dated_from_the_first_delivery(self, client, dao, conn, world, vcf):
        """
        Catches: THE `release_events` FAN-OUT — a plain JOIN instead of the
        lateral aggregate, which would show the patient their one report once
        per consumer — and `released_at` taken as `max`, which would move the
        date a patient was told their report was issued every time somebody
        else received a copy.

        Vacuous against: every isolation question.
        """
        org = world["a"]
        run = _pipeline(dao, org, org["first_patient"], vcf, "released")
        cookie = _portal_login(client, org["org_id"], org["first_email"])
        first_answer = _series(client, cookie)
        assert len(first_answer) == 1, first_answer
        first_date = first_answer[0]["released_at"]

        dao._release_report(org["staff"], run["report_id"], consumer="a-second-consumer", actor_id=org["staff"].user_id)
        conn.commit()

        second_answer = _series(client, cookie)
        assert len(second_answer) == 1, second_answer
        assert second_answer[0]["released_at"] == first_date, (first_answer, second_answer)


class TestTheSupersessionEdge:
    """
    THE ONE DATUM THIS WAVE ADDS. Both ends of every edge are reports the same
    response already lists, so the edge exposes no new class of information —
    and the rows below are what hold that property in place.
    """

    def test_an_amendment_and_its_original_are_linked_in_both_directions(self, client, dao, conn, world, vcf):
        """
        THE POSITIVE TWIN FOR THE EDGE. Without it every edge row below is
        satisfied by an implementation that never draws an edge at all.

        Catches: `amends_report_id` never populated; the inverse derivation
        never run. Vacuous against: an edge drawn to a report the caller may
        not see, which the next two rows are for.
        """
        org = world["a"]
        run = _pipeline(dao, org, org["first_patient"], vcf, "released")
        amendment_id = _amend(dao, org, run)
        conn.commit()

        cookie = _portal_login(client, org["org_id"], org["first_email"])
        series = _series(client, cookie)
        assert len(series) == 2, series
        by_id = {point["report_id"]: point for point in series}

        original = by_id[str(run["report_id"])]
        amendment = by_id[str(amendment_id)]
        assert amendment["amends_report_id"] == str(run["report_id"]), series
        assert original["superseded_by_report_id"] == str(amendment_id), series
        assert original["amends_report_id"] is None, series
        assert amendment["superseded_by_report_id"] is None, series

    def test_an_amendment_of_a_NEVER_RELEASED_original_names_nothing(self, client, dao, conn, world, vcf):
        """
        THE EDGE'S OWN EXISTENCE ORACLE, AND THE ROW THAT CLOSES IT.

        `create_amendment` accepts an original in state `approved` OR
        `released`. So a patient can hold a released amendment whose original
        was approved and NEVER SENT TO THEM. Naming that original's id in the
        response would confirm that a report id the release gate exists to
        withhold is real — the same leak as an unreleased report appearing in
        the list, arriving through a different field.

        Catches: an edge built on `amendments` alone, without re-applying the
        release predicate to the other end. That is the obvious
        implementation. Vacuous against: the edge never being drawn at all —
        which the twin above refuses.
        """
        org = world["a"]
        run = _pipeline(dao, org, org["first_patient"], vcf, "approved")
        amendment_id = _amend(dao, org, run)
        conn.commit()
        with conn.cursor() as cur:
            cur.execute("SELECT state FROM reports WHERE org_id = %s AND id = %s", (org["org_id"], run["report_id"]))
            assert cur.fetchone()[0] == "approved", "the premise is broken — the original is not still `approved`"

        cookie = _portal_login(client, org["org_id"], org["first_email"])
        series = _series(client, cookie)
        assert len(series) == 1, series
        assert series[0]["report_id"] == str(amendment_id), series
        assert series[0]["amends_report_id"] is None, series
        assert json.dumps(series).find(str(run["report_id"])) == -1, series

    def test_the_positive_twin_releasing_that_original_makes_the_edge_appear(self, client, dao, conn, world, vcf):
        """
        THE TWIN FOR THE ROW ABOVE, and it is what stops that row being
        satisfied by an edge that is never drawn. Same fixture, same patient,
        one extra legitimate act: the original is released too.

        Catches: an implementation that answers `None` unconditionally.
        """
        org = world["a"]
        run = _pipeline(dao, org, org["first_patient"], vcf, "approved")
        amendment_id = _amend(dao, org, run)
        dao._release_report(
            org["staff"], run["report_id"], consumer=PORTAL_RELEASE_CONSUMER, actor_id=org["staff"].user_id
        )
        conn.commit()

        cookie = _portal_login(client, org["org_id"], org["first_email"])
        series = _series(client, cookie)
        assert len(series) == 2, series
        by_id = {point["report_id"]: point for point in series}
        assert by_id[str(amendment_id)]["amends_report_id"] == str(run["report_id"]), series

    def test_the_edge_cannot_leave_the_patients_own_interpretation(self):
        """
        THE STRUCTURAL COMPANION. The behavioural rows above see one patient's
        two reports; this sees the predicate that makes a cross-patient edge
        impossible rather than merely untested.

        `create_amendment` copies the original's `interpretation_id` onto the
        amendment, so the two always share one interpretation — which already
        sits under this patient's own order via the five hops of the shared
        join. The SQL requires that equality explicitly.

        Catches: the `orig.interpretation_id = rep.interpretation_id` predicate
        being dropped as "redundant". Vacuous against: everything behavioural.
        """
        from clinical.data_access import DataAccess

        shared = DataAccess._MY_RELEASED_REPORTS_SQL
        assert "orig.interpretation_id = rep.interpretation_id" in shared, shared
        assert "orig.state = 'released'" in shared, shared
        assert "FROM release_events re2" in shared, shared
        # LATERAL ... LIMIT 1, not a plain join: `amendments` has no unique
        # constraint on amendment_report_id, and a plain join would fan out.
        assert "LEFT JOIN LATERAL" in shared, shared
        assert "LIMIT 1" in shared, shared


# ─── 3. patient isolation and tenant isolation, separately ───────────────────


class TestTheTwoIsolationAxesAreTestedSeparately:
    def test_two_patients_in_one_organisation_get_two_different_series(self, client, dao, conn, world, vcf):
        """
        THE PATIENT AXIS ALONE. Both patients are in the SAME organisation, so
        an organisation predicate cannot distinguish them.

        Catches: THE PATIENT PREDICATE DROPPED from the anchor — which leaves
        a perfectly tenant-isolated surface that shows every patient in the
        organisation everybody else's reports. Vacuous against:
        cross-organisation leaks.
        """
        org = world["a"]
        first = _pipeline(dao, org, org["first_patient"], vcf, "released", tag="w136-first")
        second = _pipeline(dao, org, org["second_patient"], vcf, "released", tag="w136-second")
        conn.commit()

        first_cookie = _portal_login(client, org["org_id"], org["first_email"])
        second_cookie = _portal_login(client, org["org_id"], org["second_email"])

        first_series = _series(client, first_cookie)
        second_series = _series(client, second_cookie)
        assert [p["report_id"] for p in first_series] == [str(first["report_id"])], first_series
        assert [p["report_id"] for p in second_series] == [str(second["report_id"])], second_series
        assert str(second["report_id"]) not in json.dumps(first_series)
        assert str(first["report_id"]) not in json.dumps(second_series)

    def test_the_same_email_in_two_organisations_reaches_two_different_series(self, client, dao, conn, world, vcf):
        """
        THE TENANT AXIS ALONE, and the address is deliberately THE SAME in both
        organisations: a tenant test built on two distinct addresses passes
        against a lookup keyed on the address alone.

        Catches: an org predicate dropped from the anchor or from any of the
        seven joins. Vacuous against: patient-axis leaks inside one
        organisation.
        """
        a, b = world["a"], world["b"]
        run_a = _pipeline(dao, a, a["first_patient"], vcf, "released", tag="w136-a")
        run_b = _pipeline(dao, b, b["first_patient"], vcf, "released", tag="w136-b")
        conn.commit()
        assert a["first_email"] == b["first_email"], "the premise is broken — the addresses differ"

        series_a = _series(client, _portal_login(client, a["org_id"], a["first_email"]))
        series_b = _series(client, _portal_login(client, b["org_id"], b["first_email"]))
        assert [p["report_id"] for p in series_a] == [str(run_a["report_id"])], series_a
        assert [p["report_id"] for p in series_b] == [str(run_b["report_id"])], series_b
        assert str(run_b["report_id"]) not in json.dumps(series_a)
        assert str(run_a["report_id"]) not in json.dumps(series_b)

    def test_a_patient_id_supplied_in_any_channel_cannot_change_whose_series_comes_back(
        self, client, dao, conn, world, vcf
    ):
        """
        THE URL-EDITING ATTACK, IN EVERY CHANNEL THE ROUTE HAS. The held
        trends route takes the patient from the path; this one takes it from
        nowhere, and these rows prove the difference rather than asserting it.

        Catches: a handler that grew a `patient_id` parameter. Vacuous
        against: a leak that needs no parameter — §3's first two rows.
        """
        org = world["a"]
        mine = _pipeline(dao, org, org["first_patient"], vcf, "released", tag="w136-mine")
        theirs = _pipeline(dao, org, org["second_patient"], vcf, "released", tag="w136-theirs")
        conn.commit()

        cookie = _portal_login(client, org["org_id"], org["first_email"])
        answers = {
            "no channel": _series(client, cookie),
            "query string": _series(client, cookie, params={"patient_id": str(org["second_patient"])}),
            "header": _series(client, cookie, headers={"X-Patient-Id": str(org["second_patient"])}),
        }
        body_response = client.request(
            "GET",
            "/portal/me/trends",
            cookies={PATIENT_COOKIE: cookie},
            json={"patient_id": str(org["second_patient"])},
        )
        assert body_response.status_code == 200, body_response.text
        answers["body"] = body_response.json()

        for channel, series in answers.items():
            assert [p["report_id"] for p in series] == [str(mine["report_id"])], (channel, series)
            assert str(theirs["report_id"]) not in json.dumps(series), (channel, series)

    def test_a_staff_session_is_refused_by_the_method_and_a_patient_session_is_not(self, dao, conn, world, vcf):
        """
        DECISION 8 at the data layer: patient access is EXPLICIT. A staff
        session does not become a patient session by being handed to a portal
        method.

        Catches: duck-typing — a staff session satisfying the org predicate and
        failing only on a missing attribute, which is an AttributeError rather
        than a refusal and is not the same thing. Vacuous against: every
        HTTP-level question.
        """
        from clinical.data_access import AuthorizationError

        org = world["a"]
        _pipeline(dao, org, org["first_patient"], vcf, "released")
        conn.commit()

        with pytest.raises(AuthorizationError):
            dao.list_my_report_trend(org["staff"])

        patient_session = dao.patient_login(org["first_email"], org["org_id"], PASSWORD)
        assert len(dao.list_my_report_trend(patient_session)) == 1


# ─── 4. the refusal, as a set of size one, in BOTH channels ──────────────────


class TestEveryRefusalOnTheTrendsRouteIsOnePair:
    """
    RULE 8. A per-case equality passes against a leak in a case nobody listed,
    so the assertion is a SET, over `(status_code, response.content)`, and it
    includes the ANONYMOUS case — which is what makes it a statement about
    what an unauthenticated prober can learn.
    """

    def _cases(self, client, dao, conn, world):
        a = world["a"]
        staff_cookie = _staff_login(client, a["org_id"], a["staff_email"])
        terminated = _portal_login(client, a["org_id"], a["second_email"])
        client.post("/portal/logout", cookies={PATIENT_COOKIE: terminated})

        return {
            # `cookies={}` rather than omitting the argument, the way w135
            # builds the same set: every case is explicit about what it
            # presents, so no case can silently inherit a credential.
            "anonymous": client.get("/portal/me/trends", cookies={}),
            "malformed session credential": client.get("/portal/me/trends", cookies={PATIENT_COOKIE: "not-a-uuid"}),
            "well-formed credential naming nothing": client.get(
                "/portal/me/trends", cookies={PATIENT_COOKIE: str(A_UUID_THAT_NAMES_NOTHING)}
            ),
            "a genuine STAFF session id in the PATIENT cookie": client.get(
                "/portal/me/trends", cookies={PATIENT_COOKIE: staff_cookie}
            ),
            "a genuine STAFF session under the STAFF cookie name": client.get(
                "/portal/me/trends", cookies={STAFF_COOKIE: staff_cookie}
            ),
            "a terminated patient session": client.get("/portal/me/trends", cookies={PATIENT_COOKIE: terminated}),
            # The bearer channel, which `_presented_patient_session_id` reads
            # as well as the cookie. A refusal set built only over cookies
            # would say nothing about it.
            "a bearer header naming nothing": client.get(
                "/portal/me/trends",
                cookies={},
                headers={"Authorization": f"Bearer {A_UUID_THAT_NAMES_NOTHING}"},
            ),
            "a genuine STAFF session id as a bearer token": client.get(
                "/portal/me/trends", cookies={}, headers={"Authorization": f"Bearer {staff_cookie}"}
            ),
            "a malformed bearer token": client.get(
                "/portal/me/trends", cookies={}, headers={"Authorization": "Bearer not-a-uuid"}
            ),
        }

    def test_nine_cases_on_the_trends_route_are_one_pair(self, client, dao, conn, world):
        """
        Catches: ANY divergence at all between the nine, in EITHER channel — a
        404 here and a 401 there, or a body that names what was presented. The
        `STAFF cookie name` case is the one a hand-written per-case test omits.

        Vacuous against: a route that refuses everybody, which is exactly what
        the twin below is for.
        """
        cases = self._cases(client, dao, conn, world)
        assert len(cases) == 9, "the case list shrank — this row would prove less than it claims"
        observed = {name: (response.status_code, response.content) for name, response in cases.items()}
        assert len(set(observed.values())) == 1, {k: (s, body[:120]) for k, (s, body) in observed.items()}
        assert set(observed.values()) == {THE_REFUSAL}, observed

    def test_no_refusal_names_any_identifier_it_was_given(self, client, dao, conn, world):
        """
        THE BODY CHANNEL ON ITS OWN. A set of size one is already violated by
        bodies that DIFFER; this row exists against a future change that leaks
        the same shape of identifier into ALL NINE and so keeps the set at one.

        Catches: `f"Session {session_id} not found"`. Vacuous against: a
        divergence with no identifier in it.
        """
        needles = (str(A_UUID_THAT_NAMES_NOTHING), "not-a-uuid")
        for name, response in self._cases(client, dao, conn, world).items():
            body = response.content.decode("utf-8", "replace")
            for needle in needles:
                assert needle not in body, f"{name}: the refusal echoes {needle!r}: {body}"

    def test_the_positive_twin_the_owner_of_a_released_report_is_not_in_that_set(self, client, dao, conn, world, vcf):
        """
        Catches: a route that refuses everyone — which satisfies the set row
        above completely. Also asserts the 200 carries the caller's OWN report
        id, because a 200 carrying the neighbour's is worse than a refusal.
        """
        org = world["a"]
        mine = _pipeline(dao, org, org["first_patient"], vcf, "released", tag="w136-mine")
        theirs = _pipeline(dao, org, org["second_patient"], vcf, "released", tag="w136-theirs")
        conn.commit()

        cookie = _portal_login(client, org["org_id"], org["first_email"])
        response = client.get("/portal/me/trends", cookies={PATIENT_COOKIE: cookie})
        assert (response.status_code, response.content) != THE_REFUSAL
        assert str(mine["report_id"]) in response.text, response.text
        assert str(theirs["report_id"]) not in response.text, response.text


class TestTheSuccessAnswerIsNotAnExistenceOracleEither:
    """
    THIS ROUTE HAS NO IDENTIFIER CHANNEL, so its oracle would be the SUCCESS
    answer: a caller must not be able to tell "I have no released reports" from
    "somebody else does" or from "this organisation is not the one".
    """

    def test_three_request_shapes_that_must_all_be_the_empty_series(self, client, dao, conn, world, vcf):
        """
        Catches: the list route distinguishing "your neighbour has a report"
        from "nobody does"; an invented `patient_id` changing the answer; a
        cross-organisation caller learning that the other tenant has data.

        Vacuous against: a route that lists nothing — the twin below.
        """
        a, b = world["a"], world["b"]
        _pipeline(dao, a, a["second_patient"], vcf, "released", tag="w136-neighbour")
        _pipeline(dao, b, b["first_patient"], vcf, "released", tag="w136-other-org")
        _pipeline(dao, a, a["first_patient"], vcf, "draft", tag="w136-mine-draft")
        conn.commit()

        mine = _portal_login(client, a["org_id"], a["first_email"])
        other_org = _portal_login(client, b["org_id"], b["second_email"])
        observed = {
            "my own draft only": client.get("/portal/me/trends", cookies={PATIENT_COOKIE: mine}),
            "the same request with an invented patient_id": client.get(
                "/portal/me/trends",
                cookies={PATIENT_COOKIE: mine},
                params={"patient_id": str(a["second_patient"])},
            ),
            "a patient in the other organisation": client.get("/portal/me/trends", cookies={PATIENT_COOKIE: other_org}),
        }
        pairs = {name: (r.status_code, r.content) for name, r in observed.items()}
        assert set(pairs.values()) == {THE_EMPTY_SERIES}, pairs

    def test_the_positive_twin_a_released_report_is_not_the_empty_answer(self, client, dao, conn, world, vcf):
        """Catches exactly that: a surface for which every answer is `[]`."""
        org = world["a"]
        _pipeline(dao, org, org["first_patient"], vcf, "released")
        conn.commit()
        cookie = _portal_login(client, org["org_id"], org["first_email"])
        response = client.get("/portal/me/trends", cookies={PATIENT_COOKIE: cookie})
        assert (response.status_code, response.content) != THE_EMPTY_SERIES, response.text


# ─── 5. retraction: the measurement, and the shape it forces ─────────────────


class TestTheRetractionVerdict:
    """
    THE MEASUREMENT THAT STOOD HERE HAS BEEN ANSWERED. Until 2de2fef
    `report_retractions` did not exist and the first row said so, naming the
    work its arrival would owe: an `is_retracted` field PER POINT, and no
    filter in `_MY_RELEASED_REPORTS_SQL`. The table arrived, the row fired, the
    human decided, and w148 did exactly that — see
    `TestTheRetractedTrendPointIsShownAndFlagged` below for the behavioural
    answer. E4's optional `replacement_report_id`, which that row also named as
    a possibility, was REFUSED by the same decision.

    THE SECOND HALF HAS NOW MOVED TOO. w156 (WAVE 6) wrote `retract_report`,
    which is what the row below said "needs revisiting" if it ever appeared.
    It is RE-AUTHORED INTO THE POSITIVE ROW the behaviour now needs, not
    deleted and not skipped: the verb must exist, must be Approver-gated at
    its first statement, and the two state-walk-back verbs must still not
    exist. What has NOT moved, and is unchanged below, is the reader
    property: release is still the only thing that keeps a report out of the
    series, because rulings E3/E7 forbid a retraction filter.
    """

    def test_the_clinical_layer_retracts_by_a_gated_verb_and_the_series_still_has_no_filter(self):
        """
        RE-AUTHORED AT w156 FROM `..._still_has_no_operation_that_retracts_an_approval`.

        BY SYNTAX TREE, NOT BY SUBSTRING, and that is the difference that
        matters here rather than a preference. The original row searched the
        file text for `"def retract_report"`. w156 added roughly a hundred
        lines of DOCSTRING naming that verb, and a substring row of the
        opposite polarity — "the verb is present" — would have been satisfied
        by the prose alone. Wave 5 found a live guard on this floor firing on
        a docstring; this walks `DataAccess`'s own body and looks at
        `FunctionDef` nodes.

        Catches: the verb deleted; its Approver gate removed; its gate moved
        below the report lookup (which would make an unauthorised caller's
        refusal depend on whether the report exists); a walk-back verb added.
        Vacuous against: nothing — both polarities are asserted, and the
        helper's own non-vacuity twin lives in
        `test_w135_patient_report_access.py`.
        """
        import clinical.data_access

        from clinical.tests.test_w156_retraction_reachability import _first_role_gate

        source = pathlib.Path(inspect.getfile(clinical.data_access)).read_text(encoding="utf-8")
        klass = next(n for n in ast.parse(source).body if isinstance(n, ast.ClassDef) and n.name == "DataAccess")
        defined = {n.name: n for n in klass.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}

        for present in ("create_amendment", "_release_report", "retract_report"):
            assert present in defined, f"DataAccess.{present} is not defined — the tree walk is reading nothing"
        for absent in ("unapprove_report", "rescind_report"):
            assert absent not in defined, f"DataAccess.{absent} now exists — w136's retraction verdict needs revisiting"

        assert _first_role_gate(defined["retract_report"]) == "Approver", ast.dump(defined["retract_report"].body[1])

    def test_the_only_thing_that_keeps_a_report_out_of_the_series_is_release(self):
        """
        THE FORWARD-SAFE SHAPE, PINNED — and this is the wave's actual
        obligation, because the policy is not implementable at this SHA: build
        no filter that would have to be removed when w122 lands.

        Catches: a later implementer adding `AND NOT EXISTS (SELECT 1 FROM
        report_retractions ...)` or a tombstone filter, either of which
        contradicts E7. Checked on the shared SQL AND on the series method's
        own source, because the method could grow a Python-side filter that the
        SQL check cannot see.

        THE METHOD'S DOCSTRING IS BLANKED BEFORE SCANNING, and this row FAILED
        AGAINST CORRECT CODE until it was: `list_my_report_trend` EXPLAINS, in
        its own docstring, that it applies no retraction predicate. Prose
        saying a thing is absent must not be what makes the guard think it is
        present. Exactly the shape w135 hit in
        `test_no_portal_handler_reaches_raw_result_data`; the fix was to the
        test, and it is recorded here rather than quietly repaired.

        ─── THIS IS A GUARD AND w148 RE-AUTHORED IT. SAID PLAINLY. ───

        WHAT IT PROTECTED, in its own words above: "a later implementer adding
        `AND NOT EXISTS (SELECT 1 FROM report_retractions ...)` or a tombstone
        filter, either of which contradicts E7", checked on the shared SQL AND
        on the method's own Python "because the method could grow a
        Python-side filter that the SQL check cannot see". Both halves are
        about REMOVING POINTS FROM THE SERIES. The old text spelled that as
        `"retract" not in ...`, which stated the property exactly only while
        retraction could not legitimately appear at all.

        WHY IT COULD NOT STAY LITERAL: the human's decision 2 puts
        `is_retracted` on every point, and it must be derived in the ONE shared
        statement — `test_the_two_methods_share_one_sql_text` pins that
        constant's caller count at four and
        `test_every_portal_statement_binds_the_patient_from_the_session`
        requires any second statement to bind the session's patient itself. So
        the word now appears in both texts.

        THE PROPERTY STILL HOLDS, AND BOTH HALVES ARE STILL ASSERTED.
          * SQL: the statement is split at its own `FROM`; retraction must be
            PRESENT in the projection and ABSENT from every JOIN, LATERAL and
            the WHERE. A projection is computed per row already chosen by the
            FROM/WHERE — it cannot add or drop one.
          * PYTHON: instead of banning the word, every construct that could
            REMOVE AN ELEMENT is enumerated from the AST — `if`/`while` tests,
            conditional expressions, comprehension conditions, and calls to
            remove/pop/discard/filter — and none of them may mention
            retraction. A `del` statement is banned outright. That is the
            property the old line was approximating, stated directly.
        Non-vacuity for the Python half: the method is asserted to contain at
        least one `if` test, so the scan is looking at something.

        THE BEHAVIOURAL PROOF is `TestTheRetractedTrendPointIsShownAndFlagged`
        below: a really-retracted point is asserted to be still IN the series,
        in its place, flagged. Any filter re-introduced anywhere fails those.

        Vacuous against: everything about who may read what.
        """
        import clinical.data_access
        from clinical.data_access import DataAccess

        shared = DataAccess._MY_RELEASED_REPORTS_SQL
        assert "rep.state = 'released'" in shared, shared
        assert "tombston" not in shared.lower(), shared

        select_list, separator, row_source = shared.partition("FROM patients p ")
        assert separator, "the split is broken — this row would compare nothing to nothing"
        assert "rep.state = 'released'" in row_source, row_source
        assert "retract" not in row_source.lower(), (
            "retraction has reached the FROM/JOIN/WHERE of the shared statement, where it can decide "
            "which rows come back — E7 forbids exactly that. The flag belongs in the projection.\n"
            f"{row_source}"
        )
        assert "AS is_retracted" in select_list, select_list
        assert "rr.org_id = rep.org_id" in select_list, select_list
        assert "rr.report_id = rep.id" in select_list, select_list

        source = pathlib.Path(inspect.getfile(clinical.data_access)).read_text(encoding="utf-8")
        tree = ast.parse(source)
        method = next(
            node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "list_my_report_trend"
        )
        first = method.body[0] if method.body else None
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
            first.value.value = ""
        body = ast.unparse(method)
        assert "_MY_RELEASED_REPORTS_SQL" in body, (
            "the AST lookup or the docstring strip removed too much — this row would be vacuous"
        )
        assert "superseded_by_report_id" in body, "the docstring strip removed executable code"
        assert "tombston" not in body.lower(), body
        assert "is_retracted" in body, "the flag is not carried per point — decision 2 is not implemented"

        # Every construct in the method that could REMOVE a point, enumerated.
        removing = []
        for node in ast.walk(method):
            if isinstance(node, (ast.If, ast.IfExp, ast.While)):
                removing.append(ast.unparse(node.test))
            elif isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
                removing.extend(ast.unparse(cond) for gen in node.generators for cond in gen.ifs)
            elif isinstance(node, ast.Call) and getattr(node.func, "attr", None) in ("remove", "pop", "discard"):
                removing.append(ast.unparse(node))
            elif isinstance(node, ast.Call) and getattr(node.func, "id", None) == "filter":
                removing.append(ast.unparse(node))
        assert removing, "no element-removing construct found at all — the scan is broken, not the method"
        for construct in removing:
            assert "retract" not in construct.lower(), (
                f"a Python-side retraction filter has appeared in list_my_report_trend: {construct}. "
                "E7 and the human's decision 2 both say the point REMAINS in the series."
            )
        assert not [node for node in ast.walk(method) if isinstance(node, ast.Delete)], (
            "list_my_report_trend now deletes something — a point removed after the fact is still a point removed"
        )

    def test_every_node_of_the_series_is_a_report_id_which_is_what_retraction_attaches_to(
        self, client, dao, conn, world, vcf
    ):
        """
        THE RECONCILIATION ITSELF, ASSERTED BEHAVIOURALLY RATHER THAN ARGUED.
        Every id the series returns is resolvable in `reports` for the caller's
        own organisation — so `report_retractions.report_id` joins to it
        directly, and an `is_retracted` flag has somewhere to go.

        Catches: the node key being changed to an interpretation id, which is
        what the held w127 series uses and what cannot carry a retraction at
        all (one interpretation, many reports). Vacuous against: every
        isolation question.
        """
        org = world["a"]
        run = _pipeline(dao, org, org["first_patient"], vcf, "released")
        amendment_id = _amend(dao, org, run)
        conn.commit()

        cookie = _portal_login(client, org["org_id"], org["first_email"])
        series = _series(client, cookie)
        assert len(series) == 2, series

        with conn.cursor() as cur:
            for point in series:
                cur.execute(
                    "SELECT interpretation_id FROM reports WHERE org_id = %s AND id = %s",
                    (org["org_id"], uuid.UUID(point["report_id"])),
                )
                row = cur.fetchone()
                assert row is not None, f"{point['report_id']} does not name a report in this organisation"

        # AND THE TWO NODES SHARE ONE INTERPRETATION, which is the whole reason
        # an interpretation-keyed series cannot carry a per-report fact.
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(DISTINCT interpretation_id) FROM reports WHERE org_id = %s AND id = ANY(%s)",
                (org["org_id"], [run["report_id"], amendment_id]),
            )
            assert cur.fetchone()[0] == 1, (
                "the premise is broken — an amendment should carry its original's interpretation_id"
            )

    def test_a_tombstoned_report_still_appears_in_the_series_and_that_is_an_open_ruling(
        self, client, dao, conn, world, vcf
    ):
        """
        RECORDED BEHAVIOUR, NOT AN ENDORSEMENT — and recorded as a test
        precisely so that it is visible rather than accidental.

        MEASURED at 3a482f1: `tombstoned_at IS NULL` appears ZERO times in
        clinical/data_access.py. No reader in that file filters it. The HELD
        trends branch DOES filter it — three times, inside
        `find_variant_trends_for_patient` — so the two bodies of work disagree,
        and ruling D4 settles neither: it says explicitly that "what, if
        anything, gets redacted inside a tombstoned row" is "a narrower,
        still-open question".

        This surface therefore follows the convention in its own file rather
        than inventing a second answer, and pins the behaviour here. If the
        ruling is made the other way, THIS ROW is what has to change, and its
        failure message says so. Carded as W136-C4; retention owns it.

        Catches: a tombstone filter arriving here silently. Vacuous against:
        every isolation question — nothing crosses a boundary in this row.
        """
        org = world["a"]
        run = _pipeline(dao, org, org["first_patient"], vcf, "released")
        conn.commit()
        _tombstone(conn, org["org_id"], run["report_id"])

        cookie = _portal_login(client, org["org_id"], org["first_email"])
        series = _series(client, cookie)
        assert [p["report_id"] for p in series] == [str(run["report_id"])], (
            "a tombstoned report no longer appears to its own patient. That may well be right — but it "
            "is retention ruling D4's open question, not this surface's to answer silently. See W136-C4."
        )


# ─── 6. the refusal: no classification reaches a patient ─────────────────────


class TestNoClassificationReachesThePatientThroughTheSeries:
    """
    THE REFUSAL w135 MADE, RE-PROVED ON THE NEW ROUTE. The held trends work's
    entire product is a classification series read out of raw `run_document`.
    This wave did not reverse the refusal, and these rows are what hold it.
    """

    def test_no_run_document_content_reaches_the_patient_on_a_released_report(self, client, dao, conn, world, vcf):
        """
        The marker is an ACMG CLASSIFICATION stored at the exact JSON path the
        held trends work reads — `variants[].candidate_interpretation.acmg.
        classification`. A body containing it was assembled from raw pipeline
        output.

        Catches: any widening that reaches through `interpretation_id` to
        "show the patient their results". Vacuous against: a leak of report
        metadata, which §3 covers.
        """
        org = world["a"]
        _pipeline(dao, org, org["first_patient"], vcf, "released")
        conn.commit()
        cookie = _portal_login(client, org["org_id"], org["first_email"])

        response = client.get("/portal/me/trends", cookies={PATIENT_COOKIE: cookie})
        assert response.status_code == 200, response.text
        assert response.json(), "the premise is broken — nothing was returned to inspect"
        assert CLASSIFICATION_MARKER not in response.text, response.text

    def test_the_marker_really_is_in_the_raw_interpretation(self, dao, conn, world, vcf):
        """
        THE BROKEN-PREMISE GUARD. Without it the row above passes because the
        marker was never stored — the classic way an absence assertion goes
        wrong (Rule 4).
        """
        org = world["a"]
        run = _pipeline(dao, org, org["first_patient"], vcf, "released")
        conn.commit()
        raw = dao.get_interpretation(org["staff"], run["interpretation_id"])
        assert raw is not None, "the premise is broken -- the interpretation was not stored"
        assert CLASSIFICATION_MARKER in json.dumps(raw["run_document"]), "the marker is not in the stored document"

    def test_the_trend_point_model_carries_no_classification_and_no_interpretation_id(self):
        """
        THE SHAPE, AS AN EXPLICIT SET. A SEVENTH field on this model is a
        thing somebody must look at — it was five until w148 added the one
        field the human approved, and the count moved here rather than the set
        being loosened.

        THE INTERNAL RETRACTION COLUMNS ARE NAMED IN THE FORBIDDEN LIST, not
        merely absent from the set, so the failure message says what was
        exposed. But this row is NOT the leakage test: a model field list is
        vacuous against a handler that puts the datum somewhere else, and
        against a model that gains a field later. The leakage row asserts over
        the serialised body, in
        TestTheRetractedTrendPointIsShownAndFlagged.

        Catches: a "helpful" widening of `PortalTrendPoint` — the cheapest way
        for the held work to arrive here one field at a time. Vacuous against:
        a leak of run_document contents without its id, which the behavioural
        row above is for.
        """
        from clinical.portal_endpoints import PortalTrendPoint

        fields = set(PortalTrendPoint.model_fields)
        assert fields == {
            "report_id",
            "order_id",
            "released_at",
            "amends_report_id",
            "superseded_by_report_id",
            "is_retracted",
        }, fields
        for forbidden in (
            "interpretation_id",
            "classification",
            "variant_key",
            "content_hash",
            "approver_id",
            "replacement_report_id",
            "retraction_reason",
            "retracted_by",
            "retracted_at",
            "retracted_from_state",
        ):
            assert forbidden not in fields, f"PortalTrendPoint now carries {forbidden!r}"


# ─── 7. retraction: the flag the patient sees, and nothing else (w148) ───────


class TestTheRetractedTrendPointIsShownAndFlagged:
    """
    THE HUMAN'S DECISION, IMPLEMENTED (w148, PHASE 3): "A trend point
    originating from a retracted report remains in the patient's longitudinal
    data but carries an `is_retracted` flag. Do NOT expose
    `replacement_report_id` or other internal retraction details."

    THIS CLASS REPLACES THE MEASUREMENT THAT USED TO STAND HERE, whose failure
    message named exactly this work: "must gain an `is_retracted` field per
    POINT ... Do NOT add a filter to DataAccess._MY_RELEASED_REPORTS_SQL." The
    table arrived at 2de2fef; no filter was added; the flag is a projection.

    E4's OPTIONAL `replacement_report_id` IS NOT BUILT. The old row named it as
    a possibility; the human's decision names it as a refusal. The refusal is
    the one implemented.

    HOW A RETRACTION IS CREATED HERE: `_retract` above, by direct insert,
    because there is no retract verb in this repository and this wave did not
    bring one across. That is ARRANGEMENT for a subject that is a READER, not
    the bypass pattern -- every expectation below is still read back through
    `GET /portal/me/trends`.
    """

    def test_a_normal_point_carries_the_flag_and_it_is_false(self, client, dao, conn, world, vcf):
        """
        (A) THE SIXTH FIELD, AND IT IS FALSE, on a series with a real
        supersession edge in it so the row also proves the existing shape
        survived: two points, in first-delivery order, with `amends_report_id`
        and `superseded_by_report_id` still wired.

        Catches: the field missing, the field hard-coded `true`, and the
        ordering or the edge being lost to the new column. Vacuous against:
        wrong-version F, the flag hard-coded `false`.
        """
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.report_retractions')")
            assert cur.fetchone()[0] is not None, "the premise is broken — migration 0003 did not run"

        org = world["a"]
        run = _pipeline(dao, org, org["first_patient"], vcf, "released")
        amendment_id = _amend(dao, org, run)
        conn.commit()

        series = _series(client, _portal_login(client, org["org_id"], org["first_email"]))
        assert [p["report_id"] for p in series] == [str(run["report_id"]), str(amendment_id)], series
        assert [p["is_retracted"] for p in series] == [False, False], series
        assert series[0]["superseded_by_report_id"] == str(amendment_id), series
        assert series[1]["amends_report_id"] == str(run["report_id"]), series

    def test_a_retracted_point_stays_in_the_series_and_says_true(self, client, dao, conn, world, vcf):
        """
        (B) THE ROW THE WAVE EXISTS FOR. The retracted report is the FIRST of
        two points, so the row proves three things at once: it is still there
        (E7 — never unfindable), it is flagged, and its NEIGHBOUR is not.

        Catches: wrong-version F, the flag hard-coded `false`, which every
        absence and isolation row passes; wrong-version E, the retracted point
        filtered out instead of flagged, which would leave a one-point series;
        and a derivation that flags the whole answer rather than the row.
        Vacuous against: whose retraction set it — the two rows below.
        """
        org = world["a"]
        run = _pipeline(dao, org, org["first_patient"], vcf, "released")
        amendment_id = _amend(dao, org, run)
        conn.commit()
        _retract(conn, org["org_id"], run["report_id"], RETRACTION_REASON_MARKER)

        series = _series(client, _portal_login(client, org["org_id"], org["first_email"]))
        assert [p["report_id"] for p in series] == [str(run["report_id"]), str(amendment_id)], (
            "a retracted point left the series — rulings E3 and E7 say it is SHOWN AND FLAGGED, and "
            "the decision this wave implements says it REMAINS in the longitudinal data"
        )
        assert [p["is_retracted"] for p in series] == [True, False], series
        # The existing semantics are untouched by the new column.
        assert series[0]["superseded_by_report_id"] == str(amendment_id), series
        assert series[1]["amends_report_id"] == str(run["report_id"]), series

    def test_a_neighbours_retraction_in_the_same_organisation_does_not_flag_my_point(
        self, client, dao, conn, world, vcf
    ):
        """
        (C) THE PATIENT AXIS. Two patients in ONE organisation; theirs is
        retracted, mine is not.

        Catches: wrong-version B — a derivation bound to the organisation but
        not to the report row it describes. Also catches the neighbour's point
        appearing in my series. Vacuous against: the cross-tenant form.
        """
        org = world["a"]
        mine = _pipeline(dao, org, org["first_patient"], vcf, "released")
        theirs = _pipeline(dao, org, org["second_patient"], vcf, "released", tag="w148-neighbour")
        conn.commit()
        _retract(conn, org["org_id"], theirs["report_id"], RETRACTION_REASON_MARKER)

        series = _series(client, _portal_login(client, org["org_id"], org["first_email"]))
        assert [p["report_id"] for p in series] == [str(mine["report_id"])], series
        assert series[0]["is_retracted"] is False, (
            "another patient's retraction, in the same organisation, flagged this patient's point"
        )

        # The positive twin: the neighbour really does see it flagged, so
        # "false" above is scoping and not a dead derivation.
        neighbour = _series(client, _portal_login(client, org["org_id"], org["second_email"]))
        assert [(p["report_id"], p["is_retracted"]) for p in neighbour] == [(str(theirs["report_id"]), True)]

    def test_a_retraction_in_another_organisation_does_not_flag_my_point(self, client, dao, conn, world, vcf):
        """
        (D) THE TENANT AXIS, SEPARATELY — and the same email address exists in
        both organisations in this fixture, so a derivation that lost the org
        predicate has somewhere to go wrong.

        Catches: wrong-version C — the join not org-bound. Vacuous against:
        the same-tenant form, which is the row above.
        """
        a, b = world["a"], world["b"]
        mine = _pipeline(dao, a, a["first_patient"], vcf, "released")
        theirs = _pipeline(dao, b, b["first_patient"], vcf, "released", tag="w148-other-org")
        conn.commit()
        _retract(conn, b["org_id"], theirs["report_id"], RETRACTION_REASON_MARKER)

        series = _series(client, _portal_login(client, a["org_id"], a["first_email"]))
        assert [p["report_id"] for p in series] == [str(mine["report_id"])], series
        assert series[0]["is_retracted"] is False, "a retraction in ANOTHER ORGANISATION flagged this patient's point"

        other = _series(client, _portal_login(client, b["org_id"], b["first_email"]))
        assert [(p["report_id"], p["is_retracted"]) for p in other] == [(str(theirs["report_id"]), True)]

    def test_no_replacement_identifier_or_internal_detail_reaches_the_series_body(self, client, dao, conn, world, vcf):
        """
        (E) THE LEAKAGE ROW, OVER THE SERIALISED BODY. Asserting on
        `PortalTrendPoint.model_fields` instead would be vacuous against a
        model that gains a field later and against a handler that put the datum
        somewhere else; the shape row further down does that job and says so.

        THE REPLACEMENT IS A REAL REPORT THAT WAS NEVER RELEASED, so its id has
        no legitimate reason to be anywhere in this response — finding it is a
        leak and not a coincidence. `retracted_by` is a real user id; the reason
        is a marker nothing else in this repository spells.

        Catches: wrong-version D — `replacement_report_id` included "because
        the data is there", which is exactly what E4 makes available and what
        the human's decision refuses. Vacuous against: a leak by another route.
        """
        org = world["a"]
        run = _pipeline(dao, org, org["first_patient"], vcf, "released")
        never_released = _amend(dao, org, run, reason="w148: an amendment that is never issued", release=False)
        conn.commit()
        actor = _retract(
            conn,
            org["org_id"],
            run["report_id"],
            RETRACTION_REASON_MARKER,
            replacement_report_id=never_released,
        )

        raw = client.get(
            "/portal/me/trends", cookies={PATIENT_COOKIE: _portal_login(client, org["org_id"], org["first_email"])}
        ).content
        assert b'"is_retracted":true' in raw.replace(b" ", b""), (
            "the positive half failed — this row would be vacuous against an empty series"
        )
        assert str(never_released).encode() not in raw, raw
        assert str(actor).encode() not in raw, raw
        assert RETRACTION_REASON_MARKER.encode() not in raw, raw
        for forbidden in (b"replacement", b"retracted_by", b"retracted_at", b"retracted_from_state", b"reason"):
            assert forbidden not in raw.lower(), (forbidden, raw)

    def test_the_ordering_and_the_empty_and_isolated_answers_are_unchanged_by_the_new_column(
        self, client, dao, conn, world, vcf
    ):
        """
        (F) THE EXISTING SEMANTICS, RE-PROVED WITH RETRACTIONS PRESENT — the
        half of the human's coverage list that is not about the flag at all:
        "existing ordering and semantics intact".

        Three released reports for one patient, the MIDDLE one retracted: the
        order is still first-delivery order and the retraction does not move
        it. And the other organisation's patient still gets the empty series
        byte-for-byte, with a retraction sitting in the database — so the new
        column did not turn `[]` into `[null]` or into an error.

        Catches: a retraction-aware ORDER BY, and a derivation that makes the
        empty answer stop being THE_EMPTY_SERIES. Vacuous against: the flag's
        value, covered above.
        """
        a, b = world["a"], world["b"]
        first = _pipeline(dao, a, a["first_patient"], vcf, "released", tag="w148-one")
        second = _pipeline(dao, a, a["first_patient"], vcf, "released", tag="w148-two")
        third = _pipeline(dao, a, a["first_patient"], vcf, "released", tag="w148-three")
        conn.commit()
        _retract(conn, a["org_id"], second["report_id"], RETRACTION_REASON_MARKER)

        series = _series(client, _portal_login(client, a["org_id"], a["first_email"]))
        assert [p["report_id"] for p in series] == [
            str(first["report_id"]),
            str(second["report_id"]),
            str(third["report_id"]),
        ], series
        assert [p["is_retracted"] for p in series] == [False, True, False], series
        released_at = [p["released_at"] for p in series]
        assert released_at == sorted(released_at), series

        other = client.get(
            "/portal/me/trends",
            cookies={PATIENT_COOKIE: _portal_login(client, b["org_id"], b["first_email"])},
        )
        assert (other.status_code, other.content) == THE_EMPTY_SERIES, other.content
