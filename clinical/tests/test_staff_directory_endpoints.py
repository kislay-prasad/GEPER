"""
clinical/tests/test_staff_directory_endpoints.py
────────────────────────────────────────────────
Milestone 2, Phase A: the READ-ONLY STAFF DIRECTORY -- `GET /admin/users`, `GET /admin/users/{user_id}` and
`GET /admin/organisation` -- over HTTP against PostgreSQL.

WHAT IS PROVED, in the order the classes ask it:

  1. ADMINISTRATOR ONLY: of the seven principals, one reads; the Auditor and every other role (and a user holding no
     role) get the SAME 403 body, whether or not the identifier names anything, and whatever the query string says
     (the gate runs before any parameter is validated and before any row is read).
  2. ORGANISATION ISOLATION: the directory shows the caller's own organisation only; another organisation's user reads
     exactly like one that does not exist (404, byte-identical); no query string can change the organisation.
  3. FIELD VISIBILITY: the exact fields, the emails an Administrator needs, the current roles, the MFA flag and the lock
     time -- and NO authentication material: no password hash, no TOTP secret, no backup code.
  4. Content: search, filters, pagination, ordering; system accounts are not staff.
  5. AUDIT: every read, refused or permitted, leaves one row naming actor, organisation, action and resource, and NO
     payload -- no email, name, search term or filter.
  6. The directory WRITES nothing.

The two-organisation world and the login helpers are w162's, imported rather than copied.
"""

from __future__ import annotations

import json

from clinical.tests import test_w162_registration_and_consent as _w162
from clinical.tests.test_w162_registration_and_consent import (
    MALFORMED,
    NOWHERE,
    STAFF_COOKIE,
    _staff_cookie,
)

# Fixtures shared from test_w162_registration_and_consent (bound as names; see test_w166 for why).
client = _w162.client
clinical_dsn = _w162.clinical_dsn
conn = _w162.conn
dao = _w162.dao
reader = _w162.reader
reset_the_login_throttle = _w162.reset_the_login_throttle
world = _w162.world

FORBIDDEN = {"detail": "Not permitted."}
NOT_FOUND = {"detail": "Not found."}
PRINCIPALS = ("orderer", "labtech", "admin", "auditor", "interpreter", "approver", "nobody")
SECRET_WORDS = ("password_hash", "hash", "totp_secret", "backup", "bcrypt", "$2b$", "$2a$")

SUMMARY_KEYS = {"user_id", "email", "full_name", "disabled", "mfa_enrolled", "roles", "created_at"}
PROFILE_KEYS = {
    "user_id", "email", "full_name", "registration_number", "hospital", "disabled", "mfa_enrolled",
    "locked_until", "password_changed_at", "created_at", "updated_at", "roles",
}  # fmt: skip
ROLE_KEYS = {"role", "assigned_at", "assigned_by", "basis"}


# ─── helpers ─────────────────────────────────────────────────────────────────


def _cookie(client, world, principal, tag="a"):
    return {STAFF_COOKIE: _staff_cookie(client, world[tag]["org_id"], world[tag][f"{principal}_email"])}


def _get(client, world, principal, path, tag="a"):
    return client.get(path, cookies=_cookie(client, world, principal, tag))


def _routes(world):
    return ("/admin/users", f"/admin/users/{world['a']['approver_id']}", "/admin/organisation")


def _audit(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT action, outcome, resource_type, resource_id, details::text, org_id, user_id "
            "FROM audit_log WHERE action IN ('staff_directory_list', 'staff_user_read', 'staff_organisation_read') "
            "ORDER BY log_id"
        )
        rows = cur.fetchall()
    conn.rollback()
    return rows


def _sql(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall() if cur.description else None
    conn.commit()
    return rows


def _emails(page):
    return {i["email"] for i in page["items"]}


# ─── 1. administrator only ───────────────────────────────────────────────────


class TestOnlyTheAdministratorMayRead:
    def test_each_route_admits_the_administrator_and_refuses_everyone_else_with_one_body(self, client, world):
        for path in _routes(world):
            refusals = set()
            for principal in PRINCIPALS:
                response = _get(client, world, principal, path)
                if principal == "admin":
                    assert response.status_code == 200, (path, response.text)
                else:
                    assert response.status_code == 403, (path, principal, response.text)
                    assert response.json() == FORBIDDEN, (path, principal)
                    refusals.add(response.content)
            assert len(refusals) == 1, (path, "the refusal body varies by role")

    def test_the_auditor_cannot_read_the_directory_or_the_organisation(self, client, world):
        for path in _routes(world):
            assert _get(client, world, "auditor", path).json() == FORBIDDEN, path

    def test_the_gate_runs_before_the_lookup_so_a_refusal_never_depends_on_the_identifier(self, client, world):
        for principal in ("auditor", "orderer", "nobody"):
            answers = {
                _get(client, world, principal, f"/admin/users/{ident}").content
                for ident in (world["a"]["approver_id"], world["b"]["approver_id"], NOWHERE, MALFORMED)
            }
            assert answers == {json.dumps(FORBIDDEN, separators=(",", ":")).encode()}, principal

    def test_the_gate_runs_before_any_parameter_is_validated(self, client, world):
        bad = ("limit=0", "limit=201", "limit=abc", "offset=-1", "q=a", "role=bogus", "disabled=maybe")
        for query in bad:
            for principal in ("auditor", "nobody", "orderer"):
                response = _get(client, world, principal, f"/admin/users?{query}")
                assert response.status_code == 403 and response.json() == FORBIDDEN, (principal, query)
            assert _get(client, world, "admin", f"/admin/users?{query}").status_code == 400, query

    def test_anonymous_and_forged_sessions_are_refused_before_any_of_it(self, client, world):
        for path in _routes(world):
            anonymous = client.get(path)
            forged = client.get(path, cookies={STAFF_COOKIE: "not-a-session"})
            assert anonymous.status_code == forged.status_code == 401, path
            assert anonymous.content == forged.content, path

    def test_an_administrator_gains_no_clinical_read_access_from_the_role(self, client, world):
        for path in ("/clinic/patients", "/clinic/orders", "/lab/samples"):
            assert _get(client, world, "admin", path).json() == FORBIDDEN, path

    def test_a_disabled_administrator_cannot_read(self, client, dao, conn, world):
        cookie = _cookie(client, world, "admin")
        dao.set_user_disabled(world["a"]["admin"], world["a"]["admin_id"], True)
        conn.commit()
        assert client.get("/admin/users", cookies=cookie).status_code == 401


# ─── 2. organisation isolation ───────────────────────────────────────────────


class TestOrganisationIsolation:
    def test_the_list_holds_only_the_callers_own_staff(self, client, world):
        a = _get(client, world, "admin", "/admin/users").json()
        assert a["total"] == 7 and _emails(a) == {f"{p}@a.example" for p in PRINCIPALS}
        b = _get(client, world, "admin", "/admin/users", tag="b").json()
        assert b["total"] == 7 and _emails(b) == {f"{p}@b.example" for p in PRINCIPALS}
        assert not any("@b.example" in e for e in _emails(a))

    def test_another_organisations_user_reads_exactly_like_one_that_does_not_exist(self, client, world):
        for foreign in (world["b"]["approver_id"], world["b"]["admin_id"], world["b"]["nobody_id"]):
            real = _get(client, world, "admin", f"/admin/users/{foreign}")
            invented = _get(client, world, "admin", f"/admin/users/{NOWHERE}")
            malformed = _get(client, world, "admin", f"/admin/users/{MALFORMED}")
            assert real.status_code == invented.status_code == malformed.status_code == 404
            assert real.content == invented.content == malformed.content
            assert real.json() == NOT_FOUND and str(foreign) not in real.text

    def test_the_other_organisations_administrator_cannot_read_this_one(self, client, world):
        response = _get(client, world, "admin", f"/admin/users/{world['a']['approver_id']}", tag="b")
        assert response.status_code == 404 and response.json() == NOT_FOUND

    def test_a_query_string_cannot_change_the_organisation(self, client, world):
        a_id, b_id = str(world["a"]["org_id"]), str(world["b"]["org_id"])
        for extra in (f"org_id={b_id}", f"organisation_id={b_id}", f"orgId={b_id}"):
            users = _get(client, world, "admin", f"/admin/users?{extra}").json()
            assert _emails(users) == {f"{p}@a.example" for p in PRINCIPALS}, extra
            organisation = _get(client, world, "admin", f"/admin/organisation?{extra}").json()
            assert organisation["org_id"] == a_id, extra

    def test_the_organisation_endpoint_names_only_the_callers_own(self, client, world):
        a = _get(client, world, "admin", "/admin/organisation").json()
        b = _get(client, world, "admin", "/admin/organisation", tag="b").json()
        assert (a["org_id"], a["name"]) == (str(world["a"]["org_id"]), "Org A")
        assert (b["org_id"], b["name"]) == (str(world["b"]["org_id"]), "Org B")
        assert set(a) == {"org_id", "name", "created_at", "disabled"} and a["disabled"] is False

    def test_a_search_never_reaches_the_other_organisation(self, client, world):
        for term in ("@b.example", "b.example", "orderer@b"):
            assert _get(client, world, "admin", f"/admin/users?q={term}").json()["total"] == 0, term


# ─── 3. field visibility ─────────────────────────────────────────────────────


class TestFieldVisibility:
    def test_the_summary_and_profile_have_exactly_the_documented_fields(self, client, world):
        page = _get(client, world, "admin", "/admin/users").json()
        assert set(page) == {"items", "total", "limit", "offset"}
        assert all(set(i) == SUMMARY_KEYS for i in page["items"])
        profile = _get(client, world, "admin", f"/admin/users/{world['a']['approver_id']}").json()
        assert set(profile) == PROFILE_KEYS
        assert all(set(r) == ROLE_KEYS for r in profile["roles"])

    def test_the_administrator_sees_the_email_needed_to_identify_the_account(self, client, world):
        profile = _get(client, world, "admin", f"/admin/users/{world['a']['interpreter_id']}").json()
        assert profile["email"] == "interpreter@a.example" and profile["user_id"] == str(world["a"]["interpreter_id"])

    def test_no_authentication_material_is_ever_returned(self, client, conn, world):
        _sql(
            conn,
            "UPDATE users SET totp_secret = 'ENCRYPTED-SECRET-MARKER', totp_enrolled_at = now() WHERE user_id = %s",
            (world["a"]["approver_id"],),
        )
        _sql(
            conn,
            "INSERT INTO totp_backup_codes (user_id, org_id, code_hash, created_at) VALUES (%s, %s, %s, now())",
            (world["a"]["approver_id"], world["a"]["org_id"], "BACKUP-CODE-HASH-MARKER"),
        )
        hashes = [
            r[0] for r in _sql(conn, "SELECT password_hash FROM users WHERE org_id = %s", (world["a"]["org_id"],))
        ]
        assert len(hashes) == 7 and all(h.startswith("$2") for h in hashes)
        texts = [
            _get(client, world, "admin", "/admin/users?limit=200").text,
            _get(client, world, "admin", f"/admin/users/{world['a']['approver_id']}").text,
            _get(client, world, "admin", "/admin/organisation").text,
        ]
        for text in texts:
            lowered = text.lower()
            for word in SECRET_WORDS:
                assert word not in lowered, word
            for marker in ("ENCRYPTED-SECRET-MARKER", "BACKUP-CODE-HASH-MARKER", *hashes):
                assert marker not in text, marker

    def test_mfa_is_a_flag_only(self, client, conn, world):
        before = _get(client, world, "admin", f"/admin/users/{world['a']['approver_id']}").json()
        assert before["mfa_enrolled"] is False
        _sql(
            conn,
            "UPDATE users SET totp_secret = 'x', totp_enrolled_at = now() WHERE user_id = %s",
            (world["a"]["approver_id"],),
        )
        after = _get(client, world, "admin", f"/admin/users/{world['a']['approver_id']}").json()
        assert after["mfa_enrolled"] is True and "totp" not in json.dumps(after).lower()
        listed = {i["user_id"]: i["mfa_enrolled"] for i in _get(client, world, "admin", "/admin/users").json()["items"]}
        assert listed[str(world["a"]["approver_id"])] is True and listed[str(world["a"]["admin_id"])] is False

    def test_locked_until_is_shown_only_while_the_lock_is_in_force(self, client, conn, world):
        target = world["a"]["labtech_id"]
        path = f"/admin/users/{target}"
        assert _get(client, world, "admin", path).json()["locked_until"] is None
        _sql(conn, "UPDATE users SET locked_until = now() + interval '30 minutes' WHERE user_id = %s", (target,))
        assert _get(client, world, "admin", path).json()["locked_until"] is not None
        _sql(conn, "UPDATE users SET locked_until = now() - interval '1 minute' WHERE user_id = %s", (target,))
        assert _get(client, world, "admin", path).json()["locked_until"] is None

    def test_the_profile_lists_current_role_assignments_with_the_approver_basis(self, client, world):
        approver = _get(client, world, "admin", f"/admin/users/{world['a']['approver_id']}").json()
        assert [r["role"] for r in approver["roles"]] == ["Approver"]
        assert approver["roles"][0]["basis"] == "w162 test fixture: approves"
        assert approver["roles"][0]["assigned_by"] == str(world["a"]["approver_id"])
        assert _get(client, world, "admin", f"/admin/users/{world['a']['nobody_id']}").json()["roles"] == []

    def test_a_revoked_role_is_not_shown(self, client, dao, conn, world):
        dao.assign_role(world["a"]["admin"], world["a"]["orderer_id"], "Auditor")
        conn.commit()
        path = f"/admin/users/{world['a']['orderer_id']}"
        assert sorted(r["role"] for r in _get(client, world, "admin", path).json()["roles"]) == ["Auditor", "Orderer"]
        dao.revoke_role(world["a"]["admin"], world["a"]["orderer_id"], "Auditor")
        conn.commit()
        assert [r["role"] for r in _get(client, world, "admin", path).json()["roles"]] == ["Orderer"]

    def test_the_signing_identity_fields_are_shown_in_the_profile_only(self, client, conn, world):
        _sql(
            conn,
            "UPDATE users SET full_name = 'Dr A. Approver', registration_number = 'MCI-1', hospital = 'ABC' WHERE user_id = %s",
            (world["a"]["approver_id"],),
        )
        profile = _get(client, world, "admin", f"/admin/users/{world['a']['approver_id']}").json()
        assert (profile["full_name"], profile["registration_number"], profile["hospital"]) == (
            "Dr A. Approver",
            "MCI-1",
            "ABC",
        )
        listed = [
            i
            for i in _get(client, world, "admin", "/admin/users").json()["items"]
            if i["user_id"] == profile["user_id"]
        ][0]
        assert (
            listed["full_name"] == "Dr A. Approver" and "registration_number" not in listed and "hospital" not in listed
        )


# ─── 4. content: search, filters, pagination, system accounts ────────────────


class TestContent:
    def test_the_list_is_newest_first_with_total_limit_and_offset(self, client, world):
        page = _get(client, world, "admin", "/admin/users").json()
        assert (page["total"], page["limit"], page["offset"]) == (7, 50, 0)
        created = [i["created_at"] for i in page["items"]]
        assert created == sorted(created, reverse=True)

    def test_pages_are_disjoint_and_complete(self, client, world):
        seen = []
        for offset in (0, 3, 6):
            page = _get(client, world, "admin", f"/admin/users?limit=3&offset={offset}").json()
            assert page["total"] == 7 and page["limit"] == 3 and page["offset"] == offset
            seen.extend(i["user_id"] for i in page["items"])
        assert len(seen) == len(set(seen)) == 7
        beyond = _get(client, world, "admin", "/admin/users?limit=3&offset=70").json()
        assert beyond["items"] == [] and beyond["total"] == 7

    def test_search_matches_the_email_or_the_full_name_case_insensitively(self, client, conn, world):
        _sql(conn, "UPDATE users SET full_name = 'Priya Sharma' WHERE user_id = %s", (world["a"]["interpreter_id"],))
        for term in ("interpreter@a", "INTERPRETER", "priya", "SHARMA", "ya sha"):
            emails = _emails(_get(client, world, "admin", f"/admin/users?q={term}").json())
            assert emails == {"interpreter@a.example"}, term
        assert _get(client, world, "admin", "/admin/users?q=nobody-here").json()["total"] == 0

    def test_search_wildcards_are_literal(self, client, conn, world):
        _sql(conn, "UPDATE users SET full_name = %s WHERE user_id = %s", ("50% Name_Odd", world["a"]["labtech_id"]))
        assert _emails(_get(client, world, "admin", "/admin/users?q=50%25").json()) == {"labtech@a.example"}
        assert _emails(_get(client, world, "admin", "/admin/users?q=e_Odd").json()) == {"labtech@a.example"}
        assert _get(client, world, "admin", "/admin/users?q=%25%25").json()["total"] == 0
        assert _get(client, world, "admin", "/admin/users?q=__").json()["total"] == 0

    def test_search_does_not_match_identifiers(self, client, world):
        assert _get(client, world, "admin", f"/admin/users?q={str(world['a']['approver_id'])[:8]}").json()["total"] == 0

    def test_the_role_filter_uses_currently_held_roles(self, client, dao, conn, world):
        assert _emails(_get(client, world, "admin", "/admin/users?role=Auditor").json()) == {"auditor@a.example"}
        assert _get(client, world, "admin", "/admin/users?role=System").status_code == 400
        dao.assign_role(world["a"]["admin"], world["a"]["orderer_id"], "Auditor")
        conn.commit()
        assert _emails(_get(client, world, "admin", "/admin/users?role=Auditor").json()) == {
            "auditor@a.example",
            "orderer@a.example",
        }
        dao.revoke_role(world["a"]["admin"], world["a"]["orderer_id"], "Auditor")
        conn.commit()
        assert _emails(_get(client, world, "admin", "/admin/users?role=Auditor").json()) == {"auditor@a.example"}
        assert _get(client, world, "admin", "/admin/users?role=Orderer").json()["total"] == 1

    def test_disabled_staff_are_included_by_default_and_filterable(self, client, dao, conn, world):
        dao.set_user_disabled(world["a"]["admin"], world["a"]["labtech_id"], True)
        conn.commit()
        everyone = _get(client, world, "admin", "/admin/users").json()
        assert everyone["total"] == 7
        assert {i["email"]: i["disabled"] for i in everyone["items"]}["labtech@a.example"] is True
        assert _emails(_get(client, world, "admin", "/admin/users?disabled=true").json()) == {"labtech@a.example"}
        assert _get(client, world, "admin", "/admin/users?disabled=false").json()["total"] == 6
        assert _get(client, world, "admin", f"/admin/users/{world['a']['labtech_id']}").json()["disabled"] is True

    def test_system_accounts_are_not_staff(self, client, dao, conn, world):
        dao._create_system_session(world["a"]["org_id"])
        conn.commit()
        system_ids = [
            r[0]
            for r in _sql(
                conn, "SELECT user_id FROM users WHERE org_id = %s AND is_system_account", (world["a"]["org_id"],)
            )
        ]
        assert len(system_ids) == 1
        listing = _get(client, world, "admin", "/admin/users?limit=200").json()
        assert listing["total"] == 7 and not any("@platform" in e for e in _emails(listing))
        hidden = _get(client, world, "admin", f"/admin/users/{system_ids[0]}")
        invented = _get(client, world, "admin", f"/admin/users/{NOWHERE}")
        assert hidden.status_code == 404 and hidden.content == invented.content
        assert _get(client, world, "admin", "/admin/users?q=platform").json()["total"] == 0

    def test_the_organisation_profile_shows_its_disabled_flag(self, client, conn, world):
        cookie = _cookie(client, world, "admin")
        assert client.get("/admin/organisation", cookies=cookie).json()["disabled"] is False
        _sql(conn, "UPDATE organisations SET disabled = true WHERE org_id = %s", (world["a"]["org_id"],))
        assert client.get("/admin/organisation", cookies=cookie).json()["disabled"] is True
        _sql(conn, "UPDATE organisations SET disabled = false WHERE org_id = %s", (world["a"]["org_id"],))


# ─── 5. audit ────────────────────────────────────────────────────────────────


class TestEveryReadIsAuditedWithNoPayload:
    def test_each_route_leaves_one_row_naming_actor_organisation_action_and_resource(self, client, conn, world):
        a = world["a"]
        reads = [
            ("/admin/users", "staff_directory_list", "user", "<none>"),
            (f"/admin/users/{a['approver_id']}", "staff_user_read", "user", str(a["approver_id"])),
            ("/admin/organisation", "staff_organisation_read", "organisation", "<none>"),
        ]
        for path, *_ in reads:
            assert _get(client, world, "admin", path).status_code == 200, path
        rows = _audit(conn)
        assert len(rows) == 3
        for (_, action, rtype, rid), row in zip(reads, rows):
            assert (row[0], row[1], row[2], row[3]) == (action, "success", rtype, rid), row
            assert row[4] == "{}", (action, row[4])
            assert row[5] == a["org_id"] and row[6] == a["admin_id"], row

    def test_a_refusal_is_audited_as_denied_with_no_payload(self, client, conn, world):
        for principal in ("auditor", "orderer", "nobody"):
            for path in _routes(world):
                assert _get(client, world, principal, path).status_code == 403
        rows = _audit(conn)
        assert len(rows) == 9 and {r[1] for r in rows} == {"denied"}
        assert {r[0] for r in rows} == {"staff_directory_list", "staff_user_read", "staff_organisation_read"}
        assert all(r[4] == "{}" for r in rows)

    def test_nothing_identifying_reaches_the_audit_log(self, client, conn, world):
        _sql(conn, "UPDATE users SET full_name = 'Priya Sharma' WHERE user_id = %s", (world["a"]["interpreter_id"],))
        _get(client, world, "admin", "/admin/users?q=priya&role=Interpreter&disabled=false")
        _get(client, world, "admin", f"/admin/users/{world['a']['interpreter_id']}")
        _get(client, world, "admin", "/admin/organisation")
        _get(client, world, "auditor", "/admin/users?q=secretterm")
        dump = json.dumps(_audit(conn), default=str)
        for word in (
            "@a.example",
            "interpreter@",
            "Priya",
            "priya",
            "Sharma",
            "secretterm",
            "Org A",
            "Interpreter",
            "password",
        ):
            assert word not in dump, word

    def test_an_unknown_id_is_still_a_row_and_a_404(self, client, conn, world):
        assert _get(client, world, "admin", f"/admin/users/{NOWHERE}").status_code == 404
        rows = _audit(conn)
        assert rows[-1][0] == "staff_user_read" and rows[-1][3] == NOWHERE and rows[-1][1] == "success"

    def test_a_bad_parameter_is_audited_as_a_failure_without_the_value(self, client, conn, world):
        assert _get(client, world, "admin", "/admin/users?q=x").status_code == 400
        dump = json.dumps(_audit(conn), default=str)
        assert "staff_directory_list" in dump and '"x"' not in dump


# ─── 6. read-only ────────────────────────────────────────────────────────────


class TestTheDirectoryWritesNothing:
    def test_reading_the_directory_changes_no_user_role_or_organisation_row(self, client, conn, world):
        def snapshot():
            return _sql(
                conn,
                "SELECT (SELECT count(*) FROM users), (SELECT count(*) FROM role_assignments), "
                "(SELECT count(*) FROM organisations), "
                "(SELECT md5(string_agg(user_id::text || password_hash || disabled::text || updated_at::text, ',' ORDER BY user_id)) FROM users), "
                "(SELECT count(*) FROM role_assignments WHERE revoked_at IS NOT NULL)",
            )

        cookie = _cookie(client, world, "admin")
        before = snapshot()
        for path in (
            "/admin/users",
            f"/admin/users/{world['a']['approver_id']}",
            "/admin/organisation",
            "/admin/users?q=a&limit=2",
        ):
            assert client.get(path, cookies=cookie).status_code in (200, 400)
        assert snapshot() == before

    def test_there_is_no_write_verb_on_the_new_paths(self, client, world):
        cookie = _cookie(client, world, "admin")
        for path in ("/admin/users", "/admin/organisation"):
            for verb in ("post", "put", "patch", "delete"):
                assert getattr(client, verb)(path, cookies=cookie).status_code in (404, 405, 422), (verb, path)
        assert client.put(f"/admin/users/{world['a']['approver_id']}", cookies=cookie).status_code in (404, 405)
