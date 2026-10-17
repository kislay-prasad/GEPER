"""
clinical/tests/test_w173_session_ids_not_in_audit.py
────────────────────────────────────────────────────

THE FINDING. The session cookie's value IS `sessions.session_id`, the whole of
authentication. `audit_log` is append-only by grant, so a session id written to it
can never be removed, and whoever can read the table (an Auditor through
`/admin/audit`, a database reader, a backup) could present it for as long as the
session lives. Before this, `login_succeeded` and `patient_login_succeeded` wrote it
into `details`, and (since the `@auditable` opt-in default) into `resource_id`.

WHAT IS PROVED, over real HTTP and the real database, with the real cookie value as
the needle:

* No column of any audit row, for staff login, patient login, both logouts, and an
  administrator ending a session, contains the session id.
* A login row and its logout row still carry the SAME reference, so a session can be
  followed from start to end; two sessions carry different references.
* A row written the OLD way (simulated here by inserting one) is shown by the audit
  route as a reference, and the stored row is not touched.
* The audit writers scrub a session-id key even if a future `details_builder` names
  one.
* CHARACTERISATION, not endorsement: session validation is not bound to IP address
  or user agent. The test pins today's behaviour so a change to it is deliberate.
"""

from __future__ import annotations

import ast
import json
import pathlib
import uuid

import pytest

from clinical.data_access import scrub_audit_details, session_audit_ref
from clinical.tests import test_w136_trends_retraction as _w136
from clinical.tests.test_w136_trends_retraction import (
    PATIENT_COOKIE,
    STAFF_COOKIE,
    _portal_login,
    _staff_login,
)

# Fixtures bound as names (see test_w169_portal_visibility for why).
client = _w136.client
clinical_dsn = _w136.clinical_dsn
conn = _w136.conn
dao = _w136.dao
reset_the_login_throttle = _w136.reset_the_login_throttle
vcf = _w136.vcf
world = _w136.world

CLINICAL = pathlib.Path(__file__).resolve().parent.parent


def _all_audit_text(conn):
    """Every column of every audit row, as one string: the strictest needle target."""
    with conn.cursor() as cur:
        cur.execute("SELECT coalesce(string_agg(to_jsonb(a)::text, E'\\n' ORDER BY log_id), '') FROM audit_log a")
        text = cur.fetchone()[0]
    conn.commit()
    return text


def _rows(conn, action):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT resource_id, details FROM audit_log WHERE action = %s AND outcome = 'success' ORDER BY log_id",
            (action,),
        )
        rows = cur.fetchall()
    conn.commit()
    return rows


def _details(value):
    return value if isinstance(value, dict) else json.loads(value)


class TestTheReferenceItself:
    def test_it_is_stable_canonical_and_not_the_id(self):
        sid = uuid.uuid4()
        ref = session_audit_ref(sid)
        assert ref == session_audit_ref(str(sid)) == session_audit_ref(str(sid).upper())
        assert ref.startswith("sref_")
        assert str(sid) not in ref
        assert sid.hex not in ref

    def test_different_sessions_have_different_references(self):
        assert session_audit_ref(uuid.uuid4()) != session_audit_ref(uuid.uuid4())

    def test_none_stays_none(self):
        assert session_audit_ref(None) is None

    def test_scrub_rewrites_session_keys_and_nothing_else(self):
        sid = uuid.uuid4()
        out = scrub_audit_details({"session_id": str(sid), "target_session_id": sid, "user_id": "u", "n": 3})
        assert out == {
            "session_ref": session_audit_ref(sid),
            "target_session_ref": session_audit_ref(sid),
            "user_id": "u",
            "n": 3,
        }
        assert scrub_audit_details(None) is None
        assert scrub_audit_details({"session_id": None}) == {"session_ref": None}


class TestStaffLoginAndLogout:
    def test_neither_row_holds_the_session_id_and_they_correlate(self, client, conn, world):
        org = world["a"]
        cookie = _staff_login(client, org["org_id"], org["staff_email"])
        assert uuid.UUID(cookie)  # the cookie value is the session id

        response = client.post("/auth/logout", cookies={STAFF_COOKIE: cookie})
        assert response.status_code == 204, response.text

        assert cookie not in _all_audit_text(conn)

        ((login_rid, login_details),) = _rows(conn, "login_succeeded")[-1:]
        ((logout_rid, logout_details),) = _rows(conn, "session_logged_out")
        ref = session_audit_ref(cookie)
        assert login_rid == ref
        assert _details(login_details)["session_ref"] == ref
        assert logout_rid == ref
        assert _details(logout_details)["session_ref"] == ref
        assert "session_id" not in _details(login_details)
        assert "session_id" not in _details(logout_details)

    def test_two_sessions_carry_two_references(self, client, conn, world):
        org = world["a"]
        first = _staff_login(client, org["org_id"], org["staff_email"])
        second = _staff_login(client, org["org_id"], org["staff_email"])
        refs = [r for r, _ in _rows(conn, "login_succeeded")[-2:]]
        assert refs == [session_audit_ref(first), session_audit_ref(second)]
        assert refs[0] != refs[1]


class TestPatientLoginAndLogout:
    def test_neither_row_holds_the_session_id_and_they_correlate(self, client, conn, world):
        org = world["a"]
        cookie = _portal_login(client, org["org_id"], org["first_email"])
        assert uuid.UUID(cookie)

        response = client.post("/portal/logout", cookies={PATIENT_COOKIE: cookie})
        assert response.status_code == 204, response.text

        assert cookie not in _all_audit_text(conn)

        ((login_rid, login_details),) = _rows(conn, "patient_login_succeeded")[-1:]
        ((logout_rid, logout_details),) = _rows(conn, "patient_session_logged_out")
        ref = session_audit_ref(cookie)
        assert login_rid == ref
        assert _details(login_details)["session_ref"] == ref
        assert logout_rid == ref
        assert _details(logout_details)["session_ref"] == ref
        for details in (login_details, logout_details):
            assert "session_id" not in _details(details)


class TestAdministratorEndsASession:
    def test_the_target_is_recorded_as_a_reference_only(self, client, conn, dao, world):
        org = world["a"]
        victim_cookie = _staff_login(client, org["org_id"], org["staff_email"])
        admin = dao.session_valid(uuid.UUID(_staff_login(client, org["org_id"], org["staff_email"])))

        dao.terminate_session(admin, uuid.UUID(victim_cookie))

        assert victim_cookie not in _all_audit_text(conn)
        ((rid, details),) = _rows(conn, "session_terminated")
        ref = session_audit_ref(victim_cookie)
        assert rid == ref
        assert _details(details) == {"target_session_ref": ref}
        # and it matches the login row of the session that was ended
        assert any(r == ref for r, _ in _rows(conn, "login_succeeded"))


class TestTheWritersFailSafe:
    def test_a_details_mapping_naming_session_id_is_scrubbed_on_write(self, conn, dao, world):
        org = world["a"]
        sid = uuid.uuid4()
        dao._write_audit_entry(
            "w173_probe",
            "session",
            "probe",
            "success",
            org_id=org["org_id"],
            details={"session_id": str(sid), "target_session_id": str(sid)},
        )
        dao._DataAccess__connection.commit()
        assert str(sid) not in _all_audit_text(conn)
        ((_, details),) = _rows(conn, "w173_probe")
        assert _details(details) == {
            "session_ref": session_audit_ref(sid),
            "target_session_ref": session_audit_ref(sid),
        }

    def test_the_failure_audit_writer_scrubs_too(self, conn, dao, world):
        org = world["a"]
        sid = uuid.uuid4()
        dao._audit("w173_probe_failure", org["org_id"], None, {"session_id": str(sid)})
        dao._DataAccess__connection.commit()
        assert str(sid) not in _all_audit_text(conn)


class TestTheAuditRouteAndHistoricalRows:
    def test_a_row_written_the_old_way_is_shown_as_a_reference_and_left_untouched(self, client, conn, world):
        org = world["a"]
        old_sid = uuid.uuid4()
        with conn.cursor() as cur:
            cur.execute(
                'INSERT INTO audit_log (org_id, user_id, "timestamp", actor_role, action, resource_type, resource_id, '
                "outcome, details) VALUES (%s, NULL, now(), NULL, 'login_succeeded', 'session', %s, 'success', %s)",
                (org["org_id"], str(old_sid), json.dumps({"session_id": str(old_sid), "user_id": "x"})),
            )
        conn.commit()

        cookie = _staff_login(client, org["org_id"], org["staff_email"])
        response = client.get("/admin/audit?limit=1000", cookies={STAFF_COOKIE: cookie})
        assert response.status_code == 200, response.text

        assert str(old_sid) not in response.text
        assert cookie not in response.text
        shown = [e for e in response.json() if e["details"].get("session_ref") == session_audit_ref(old_sid)]
        assert len(shown) == 1, response.text
        assert "session_id" not in shown[0]["details"]

        # the stored row is exactly as written: remediation is not this route's job
        with conn.cursor() as cur:
            cur.execute("SELECT details FROM audit_log WHERE resource_id = %s", (str(old_sid),))
            stored = _details(cur.fetchone()[0])
        conn.commit()
        assert stored["session_id"] == str(old_sid)

    def test_the_route_still_refuses_an_unauthenticated_caller(self, client):
        assert client.get("/admin/audit").status_code in (401, 403)


class TestWhatSessionValidationIsBoundTo:
    """CHARACTERISATION. `sessions` stores ip_address and user_agent at login, but
    `session_valid` looks the session up by id alone: the same cookie works from any
    address with any user agent. This pins that, so adding a binding is a decision."""

    def test_the_cookie_works_with_a_different_user_agent_and_a_different_forwarded_address(self, client, world):
        org = world["a"]
        cookie = _staff_login(client, org["org_id"], org["staff_email"])
        response = client.get(
            "/admin/audit",
            cookies={STAFF_COOKIE: cookie},
            headers={"User-Agent": "a-completely-different-agent/9", "X-Forwarded-For": "203.0.113.250"},
        )
        assert response.status_code == 200, response.text

    def test_the_lookup_compares_neither_column(self):
        source = (CLINICAL / "data_access.py").read_text(encoding="utf8")
        tree = ast.parse(source)
        method = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "session_valid")
        text = ast.get_source_segment(source, method)
        assert "ip_address" not in text and "user_agent" not in text


class TestNoSessionMethodCanNameTheCredentialAgain:
    """Structural: every @auditable on a session resource that identifies it must go
    through the reference, and no details_builder may name a session id key."""

    def _decorators(self):
        tree = ast.parse((CLINICAL / "data_access.py").read_text(encoding="utf8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                for dec in node.decorator_list:
                    if isinstance(dec, ast.Call) and getattr(dec.func, "id", None) == "auditable":
                        yield node.name, {k.arg: k.value for k in dec.keywords}

    def test_session_resources_use_the_transform(self):
        checked = 0
        for name, kw in self._decorators():
            rtype = kw.get("resource_type")
            if isinstance(rtype, ast.Constant) and rtype.value in ("session", "patient_session"):
                if "resource_id_param" in kw or "resource_id_from_result" in kw:
                    checked += 1
                    transform = kw.get("resource_id_transform")
                    assert isinstance(transform, ast.Name) and transform.id == "session_audit_ref", name
        assert checked >= 5  # login, terminate_session, terminate_my_session, patient_login, patient logout

    def test_no_details_builder_names_a_session_id_key(self):
        for name, kw in self._decorators():
            builder = kw.get("details_builder")
            if builder is None:
                continue
            # DICT KEYS only: reading the attribute (`getattr(x, "session_id")`) to feed
            # session_audit_ref is how the reference is made.
            for node in ast.walk(builder):
                if isinstance(node, ast.Dict):
                    for key in node.keys:
                        if isinstance(key, ast.Constant) and key.value in ("session_id", "target_session_id"):
                            pytest.fail(f"{name}: details_builder keys on {key.value!r}; use session_audit_ref")
