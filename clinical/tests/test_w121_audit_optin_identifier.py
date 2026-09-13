"""
clinical/tests/test_w121_audit_optin_identifier.py
──────────────────────────────────────────────────

THE EXPOSURE THIS CLOSES. `@auditable` wrote the decorated method's RETURN
VALUE into the audit row's `resource_id`:

    identifier = bound_params.get(resource_id_param) if resource_id_param else result
    resource_id=str(identifier) if identifier else "<none>"

`enrol_totp` returns `List[str]` -- the eight TOTP backup codes, in plaintext.
Its own docstring says that return value "is the only moment they exist in
readable form". It was not: the decorator stringified the list straight into

    action='totp_enrolled'  resource_type='totp'
    resource_id="['a1b2c3d4e5', 'f6...', ... ]"

and `audit_log` is APPEND-ONLY BY GRANT (schema.sql: GRANT SELECT, INSERT ...
REVOKE UPDATE, DELETE). Nothing written there can be deleted or redacted by
the application. So every TOTP enrolment ever performed left a permanent,
unremovable copy of that user's second-factor recovery codes in the audit
table, next to the user id they belong to.

test_identity.py::test_backup_codes_are_not_stored_readably already asserted
the codes are not stored readably -- but it looked only in `totp_backup_codes`
(which correctly stores bcrypt hashes). It never looked in `audit_log`.

THE SHAPE OF THE FIX -- a default change, not a special case. The same
mechanism -- "write whatever came back" -- produced this exposure, produced
w120's '<none>' approvals, and produces the stringified result sets of the
read/query methods. Special-casing enrol_totp would leave the mechanism in
place for the next method someone writes. So the decorator now writes a
resource_id ONLY when it was explicitly told where the id comes from:

    resource_id_param="order_id"          -- a named PARAMETER, or
    resource_id_from_result=True          -- the return value IS the id, or
    resource_id_from_result=lambda r: ... -- one field of the return value

Undeclared means no id is recorded ('<none>'). A method returning a secret can
no longer leak it by default; it can only leak it by someone typing
resource_id_from_result=True over a secret.
"""

from __future__ import annotations

import ast
import datetime
import pathlib
import uuid
from datetime import timezone

import pytest

from clinical.data_access import (
    BcryptHasher,
    DataAccess,
    SystemClock,
    auditable,
)

DSN = __import__("os").getenv("CLINICAL_TEST_DSN")
SCHEMA_PATH = pathlib.Path(__file__).resolve().parent.parent / "schema.sql"

requires_db = pytest.mark.skipif(not DSN, reason="CLINICAL_TEST_DSN not set -- skip is not pass")


class ReversibleCipher:
    """Stand-in for the production secret cipher. Reversible, never secure."""

    def encrypt(self, plaintext: str) -> str:
        return "enc:" + plaintext

    def decrypt(self, ciphertext: str) -> str:
        assert ciphertext.startswith("enc:")
        return ciphertext[4:]


@pytest.fixture()
def conn():
    psycopg = pytest.importorskip("psycopg", reason="psycopg not installed")
    if not DSN:
        pytest.skip("CLINICAL_TEST_DSN not set")
    connection = psycopg.connect(DSN, autocommit=False, connect_timeout=10)
    with connection.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        cur.execute("DO $$ BEGIN CREATE ROLE clinical_app; EXCEPTION WHEN duplicate_object THEN NULL; END $$;")
        cur.execute("DO $$ BEGIN CREATE ROLE clinical_retention; EXCEPTION WHEN duplicate_object THEN NULL; END $$;")
        cur.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
    connection.commit()
    yield connection
    connection.close()


@pytest.fixture
def dao(conn):
    return DataAccess(
        conn,
        clock=SystemClock(),
        password_hasher=BcryptHasher(rounds=4),
        secret_cipher=ReversibleCipher(),
    )


@pytest.fixture
def org_a(dao):
    return dao.create_organisation("Org A")


@pytest.fixture
def admin_session(dao, conn, org_a):
    user_id = dao.create_user(org_a, "admin@org-a.test", "a sufficiently long password")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO role_assignments (user_id, org_id, role, assigned_by, assigned_at) "
            "VALUES (%s, %s, 'Administrator', %s, %s)",
            (user_id, org_a, user_id, datetime.datetime(2026, 9, 1, tzinfo=timezone.utc)),
        )
    conn.commit()
    return dao.login("admin@org-a.test", org_a, "a sufficiently long password")


def _resource_ids(conn, action):
    """The RECORDED resource ids for an action -- the content, not the count."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT resource_id FROM audit_log WHERE action = %s AND outcome = 'success' ORDER BY log_id",
            (action,),
        )
        rows = [r[0] for r in cur.fetchall()]
    conn.commit()
    return rows


def _all_audit_text(conn):
    """Every resource_id and every details blob in the whole log, as one string."""
    with conn.cursor() as cur:
        cur.execute("SELECT resource_id, details FROM audit_log ORDER BY log_id")
        rows = cur.fetchall()
    conn.commit()
    return "\n".join(f"{r[0]!r} {r[1]!r}" for r in rows)


# ───────────────────────────────────────────────────────────────────────────
# The headline: the backup codes must not reach the append-only audit table
# ───────────────────────────────────────────────────────────────────────────


@requires_db
class TestBackupCodesAreNotWrittenToTheAuditLog:
    def test_totp_enrolment_audit_row_does_not_contain_the_backup_codes(self, dao, conn, org_a, admin_session):
        """
        RED BEFORE THE FIX, on the VALUE: this reads the persisted row back and
        finds the codes in it. audit_log is append-only by grant, so a row
        written here can never be removed.
        """
        target = dao.create_user(org_a, "mfa@org-a.test", "a sufficiently long password")
        codes = dao.enrol_totp(admin_session, target, "SECRET")
        assert len(codes) == 8 and all(isinstance(c, str) for c in codes)

        recorded = _resource_ids(conn, "totp_enrolled")
        assert recorded, "no totp_enrolled audit row was written at all"
        blob = "\n".join(recorded)
        leaked = [c for c in codes if c in blob]
        assert not leaked, (
            f"{len(leaked)} of {len(codes)} TOTP backup codes appear verbatim in "
            f"audit_log.resource_id for action='totp_enrolled'. audit_log is "
            f"append-only by grant, so these cannot be deleted. Recorded value has "
            f"shape {recorded[0][:12]!r}... ({len(recorded[0])} chars)."
        )

    def test_nowhere_in_the_audit_log_at_all(self, dao, conn, org_a, admin_session):
        """Not resource_id specifically -- anywhere in the log, including details."""
        target = dao.create_user(org_a, "mfa2@org-a.test", "a sufficiently long password")
        codes = dao.enrol_totp(admin_session, target, "SECRET")

        text = _all_audit_text(conn)
        leaked = [c for c in codes if c in text]
        assert not leaked, f"{len(leaked)} of {len(codes)} backup codes appear somewhere in audit_log"

    def test_totp_enrolment_still_names_the_user_it_enrolled(self, dao, conn, org_a, admin_session):
        """
        Not recording the codes must not mean recording nothing: the enrolment
        record has to name WHOSE second factor was set, which is the target
        user -- a parameter, never the return value.
        """
        target = dao.create_user(org_a, "mfa3@org-a.test", "a sufficiently long password")
        dao.enrol_totp(admin_session, target, "SECRET")

        assert _resource_ids(conn, "totp_enrolled") == [str(target)]

    def test_the_secret_is_not_recorded_either(self, dao, conn, org_a, admin_session):
        """`secret` is a parameter, and no declaration may ever point at it."""
        target = dao.create_user(org_a, "mfa4@org-a.test", "a sufficiently long password")
        dao.enrol_totp(admin_session, target, "SECRET-VALUE-9137")

        assert "SECRET-VALUE-9137" not in _all_audit_text(conn)


# ───────────────────────────────────────────────────────────────────────────
# THE POINT OF THE WAVE: the DEFAULT is safe for a method nobody has written yet
# ───────────────────────────────────────────────────────────────────────────


@requires_db
class TestTheDefaultIsSafeForAMethodNobodyHasWrittenYet:
    """
    The fix is only worth anything if it protects the NEXT method. These
    define brand-new auditable methods -- not enrol_totp, not anything on the
    exemption lists -- and check what the decorator does with their return
    values by default.
    """

    def _dao_with(self, conn, method_name, decorated):
        """Attach a freshly-decorated method to a DataAccess subclass."""
        cls = type("DataAccessUnderTest", (DataAccess,), {method_name: decorated})
        return cls(
            conn,
            clock=SystemClock(),
            password_hasher=BcryptHasher(rounds=4),
            secret_cipher=ReversibleCipher(),
        )

    def test_an_undeclared_method_returning_a_secret_records_no_identifier(self, conn, dao, org_a, admin_session):
        """
        A new auditable method that returns something sensitive and says
        nothing about where its id comes from must record NO id. This is the
        assertion that prevents the next instance of the exposure.
        """

        @auditable(action="brand_new_action", resource_type="totp")
        def issue_more_codes(self, session, target_user_id):
            return ["s3cr3t-alpha", "s3cr3t-beta"]

        subject = self._dao_with(conn, "issue_more_codes", issue_more_codes)
        returned = subject.issue_more_codes(admin_session, admin_session.user_id)

        recorded = _resource_ids(conn, "brand_new_action")
        assert recorded == ["<none>"], (
            "a newly written auditable method that declares nothing recorded "
            f"{recorded!r} -- the decorator is still writing whatever came back."
        )
        for secret in returned:
            assert secret not in _all_audit_text(conn)

    def test_a_method_that_opts_in_by_result_does_record_it(self, conn, dao, org_a, admin_session):
        """The opt-in must actually work, or the default would be unusable."""

        made = uuid.uuid4()

        @auditable(action="brand_new_creator", resource_type="totp", resource_id_from_result=True)
        def make_thing(self, session):
            return made

        subject = self._dao_with(conn, "make_thing", make_thing)
        subject.make_thing(admin_session)

        assert _resource_ids(conn, "brand_new_creator") == [str(made)]

    def test_a_method_may_opt_in_to_one_field_of_its_result(self, conn, dao, org_a, admin_session):
        """A callable opt-in names one field, so the rest of the payload stays out."""

        class Receipt:
            def __init__(self):
                self.thing_id = uuid.uuid4()
                self.secret_material = "must-not-be-audited-7781"

        @auditable(
            action="brand_new_receipt",
            resource_type="totp",
            resource_id_from_result=lambda result: result.thing_id,
        )
        def make_receipt(self, session):
            return Receipt()

        subject = self._dao_with(conn, "make_receipt", make_receipt)
        receipt = subject.make_receipt(admin_session)

        assert _resource_ids(conn, "brand_new_receipt") == [str(receipt.thing_id)]
        assert "must-not-be-audited-7781" not in _all_audit_text(conn)

    def test_declaring_both_sources_is_refused_at_decoration_time(self):
        """Two answers to one question is a mistake, not a preference order."""
        from clinical.data_access import ConfigurationError

        with pytest.raises(ConfigurationError):

            @auditable(
                action="ambiguous",
                resource_type="totp",
                resource_id_param="thing_id",
                resource_id_from_result=True,
            )
            def ambiguous(self, session, thing_id):
                return thing_id


# ───────────────────────────────────────────────────────────────────────────
# Nothing that legitimately recorded an id may stop recording it
# ───────────────────────────────────────────────────────────────────────────


@requires_db
class TestTheCreatorsStillRecordTheirIds:
    def test_create_organisation_records_the_org_id(self, dao, conn):
        org_id = dao.create_organisation("Org Z")
        assert str(org_id) in _resource_ids(conn, "organisation_created")

    def test_create_user_records_the_user_id(self, dao, conn, org_a):
        user_id = dao.create_user(org_a, "someone@org-a.test", "a sufficiently long password")
        assert str(user_id) in _resource_ids(conn, "user_created")

    def test_create_patient_records_the_patient_id(self, dao, conn, admin_session):
        patient_id = dao.create_patient(admin_session, "Test", datetime.date(1990, 1, 1), "M")
        assert _resource_ids(conn, "patient_created") == [str(patient_id)]

    def test_login_records_the_session_id_not_the_session_repr(self, dao, conn, org_a, admin_session):
        """
        login returns a Session dataclass. str(Session(...)) is a repr, not an
        identifier, and it was what the audit row contained. The declaration
        names one field.
        """
        recorded = _resource_ids(conn, "login_succeeded")
        assert recorded == [str(admin_session.session_id)]
        assert not any("Session(" in r for r in recorded)


# ───────────────────────────────────────────────────────────────────────────
# Structural: no auditable method may take its id from an undeclared source
# ───────────────────────────────────────────────────────────────────────────


# Deliberately records NO resource_id, each for a stated reason. These are the
# methods whose return value is a result set, a status string or a verdict --
# never an identifier. Before w121 they wrote that value into resource_id
# verbatim; they now write '<none>'. Giving each of them a real identifier is
# carded separately and deliberately NOT done here.
NO_RESOURCE_ID = {
    # A login that failed created no session, so there is no session id to
    # name. The submitted email is recorded in `details`, where it belongs.
    "log_failed_login",
    # A sweep, not an act on one resource: one run touches every due exception,
    # and the per-exception records are written by the methods it calls.
    "retry_scheduler",
    # ── result-set returns: a list of rows is not an identifier ──────────────
    "find_reanalyses",
    "find_interpretations_by_criteria",
    "trace_report_ancestors",
    "find_reports_by_interpretation_criteria",
    "get_exceptions_for_order",
    "get_open_exceptions_by_owner",
    "list_open_exceptions",
    "get_exception_by_id",
    # ── status-string returns: a verdict is not an identifier ────────────────
    "check_consent",
    "check_identity_resolved",
    "validate_order_for_submission",
    # ── reviewer claims: `-> Any`, and the claim row's own id is not returned ─
    "_record_accept",
    "record_sole_signatory_claim",
    "_record_disagreement",
    "_add_variant_by_reviewer",
    "_mark_variant_not_relevant",
    # ── a multi-id or structured return: which of the two is "the" resource is
    #    a question this wave does not answer ─────────────────────────────────
    "record_pipeline_result",
    "create_amendment",
}


def _auditable_methods():
    source = (pathlib.Path(__file__).resolve().parent.parent / "data_access.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "DataAccess"]:
        for node in cls.body:
            if not isinstance(node, ast.FunctionDef):
                continue
            for dec in node.decorator_list:
                if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Name) and dec.func.id == "auditable"):
                    continue
                kwargs = {k.arg: k.value for k in dec.keywords}
                if isinstance(kwargs.get("auditable"), ast.Constant) and kwargs["auditable"].value is False:
                    continue
                yield node, kwargs


def test_every_auditable_method_declares_where_its_identifier_comes_from():
    """
    THE FORCING FUNCTION. The exposure happened because the decorator's default
    was "write whatever came back" -- so a method whose return value was a
    secret leaked it without anyone choosing to record it. The default is now
    "record nothing", and this test makes the choice explicit in both
    directions: an auditable method either declares a source for its id, or is
    named here as deliberately recording none.

    A method added tomorrow that returns a payload and declares nothing fails
    this test. That is the point of the wave.
    """
    offenders = []
    for node, kwargs in _auditable_methods():
        if "resource_id_param" in kwargs or "resource_id_from_result" in kwargs:
            continue
        if node.name in NO_RESOURCE_ID:
            continue
        returns = ast.unparse(node.returns) if node.returns else "<unannotated>"
        offenders.append(f"{node.name} (line {node.lineno}, returns {returns})")

    assert not offenders, (
        "auditable method(s) that declare no source for the audit row's resource_id: "
        f"{offenders}. Before w121 such a method wrote its RETURN VALUE into the "
        "append-only audit_log -- which is how eight plaintext TOTP backup codes "
        "were permanently recorded. Declare resource_id_param='<parameter>' or "
        "resource_id_from_result=True (or a lambda naming one field), or add the "
        "method to NO_RESOURCE_ID with a reason."
    )


def test_no_declaration_points_at_a_credential_shaped_parameter():
    """
    The remaining way to leak is to declare a source that IS the secret. Name
    the parameters that must never become a resource_id.
    """
    forbidden = {"password", "new_password", "secret", "totp_code", "code", "codes", "token", "api_key"}
    bad = []
    for node, kwargs in _auditable_methods():
        dec = kwargs.get("resource_id_param")
        if isinstance(dec, ast.Constant) and dec.value in forbidden:
            bad.append(f"{node.name}: resource_id_param={dec.value!r} is a credential")
    assert not bad, bad


def test_declared_resource_id_param_names_a_real_parameter():
    """A typo'd parameter name would fall back to '<none>' silently."""
    bad = []
    for node, kwargs in _auditable_methods():
        dec = kwargs.get("resource_id_param")
        if dec is None:
            continue
        name = dec.value
        params = {a.arg for a in node.args.args} | {a.arg for a in node.args.kwonlyargs}
        if name not in params:
            bad.append(f"{node.name}: resource_id_param={name!r} is not a parameter of it")
    assert not bad, bad


# ───────────────────────────────────────────────────────────────────────────
# Sizing the exposure that already exists (read-only)
# ───────────────────────────────────────────────────────────────────────────


class TestTheExposureReportClassifier:
    """
    The counter has to recognise a pre-w121 row for what it is. Rows of the
    old shape are constructed here directly -- they are exactly what the
    decorator used to write.
    """

    def test_a_backup_code_list_is_classified_as_a_credential(self):
        from clinical.audit_payload_exposure_report import classify

        old_row = "['d620e8cae2', '89ebd91e0f', '7e94c1189f']"
        assert classify("totp_enrolled", old_row) == "credential"

    def test_a_uuid_is_an_identifier(self):
        from clinical.audit_payload_exposure_report import classify

        assert classify("report_created", str(uuid.uuid4())) == "identifier"

    def test_the_markers_are_not_payloads(self):
        from clinical.audit_payload_exposure_report import classify

        assert classify("anything", "<none>") == "marker"
        assert classify("anything", "<denied>") == "marker"

    def test_an_object_repr_and_a_result_set_and_a_verdict_are_named_separately(self):
        from clinical.audit_payload_exposure_report import classify

        assert classify("login_succeeded", "Session(session_id=UUID('x'), user_id=UUID('y'))") == "object_repr"
        assert classify("interpretations_query", "[{'id': 1}]") == "result_set"
        assert classify("report_integrity_verified", "True") == "verdict"

    def test_the_artefact_class_identity_is_not_counted_as_a_payload(self):
        from clinical.audit_payload_exposure_report import classify

        assert classify("retention_policy_set", "vcf") == "identifier"


@requires_db
class TestTheExposureReportCountsRealRows:
    def test_it_counts_pre_w121_shaped_rows_without_touching_them(self, dao, conn, org_a, admin_session):
        """
        Insert three rows of the OLD shape, count them, and prove the counter
        is read-only: the rows are all still there afterwards, unchanged.
        """
        from clinical.audit_payload_exposure_report import gather

        now = datetime.datetime(2026, 9, 1, tzinfo=timezone.utc)
        old_shape = "['d620e8cae2', '89ebd91e0f', '7e94c1189f', '86966a1a1c']"
        with conn.cursor() as cur:
            for _ in range(3):
                cur.execute(
                    'INSERT INTO audit_log (action, resource_type, resource_id, outcome, org_id, user_id, "timestamp") '
                    "VALUES ('totp_enrolled', 'totp', %s, 'success', %s, %s, %s)",
                    (old_shape, org_a, admin_session.user_id, now),
                )
            cur.execute("SELECT count(*) FROM audit_log")
            before = cur.fetchone()[0]
        conn.commit()

        result = gather(conn)
        assert result["by_kind"]["credential"] == 3
        assert len(result["credential_rows"]) == 3
        assert all(r["approx_items"] == 4 for r in result["credential_rows"])
        # The report never carries the values out.
        assert "d620e8cae2" not in json_dumps(result)

        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM audit_log")
            after = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM audit_log WHERE resource_id = %s", (old_shape,))
            still_there = cur.fetchone()[0]
        conn.commit()
        assert after == before, "the report is read-only; it must not remove or add rows"
        assert still_there == 3, "the report must not redact rows -- the grant forbids it"

    def test_the_report_finds_nothing_after_the_fix(self, dao, conn, org_a, admin_session):
        """The same workload that used to produce credential rows now produces none."""
        from clinical.audit_payload_exposure_report import gather

        for i in range(3):
            target = dao.create_user(org_a, f"enrol{i}@org-a.test", "a sufficiently long password")
            dao.enrol_totp(admin_session, target, "SECRET")

        result = gather(conn)
        assert result["by_kind"].get("credential", 0) == 0
        assert result["by_kind"].get("result_set", 0) == 0
        assert result["by_kind"].get("object_repr", 0) == 0


def json_dumps(obj):
    import json as _json

    return _json.dumps(obj, default=str)


def test_no_method_returning_a_collection_of_strings_opts_in_by_result():
    """
    enrol_totp's exact shape: `-> List[str]`. A return annotation of a string
    collection is never an identifier, so opting in by result there is the
    exposure being re-introduced.
    """
    bad = []
    for node, kwargs in _auditable_methods():
        if "resource_id_from_result" not in kwargs:
            continue
        returns = ast.unparse(node.returns) if node.returns else ""
        if returns.replace(" ", "").lower() in ("list[str]", "list[str]", "sequence[str]", "tuple[str,...]"):
            bad.append(f"{node.name}: resource_id_from_result over {returns}")
    assert not bad, bad
