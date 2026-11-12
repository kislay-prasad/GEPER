"""
clinical/tests/test_staff_read_endpoints.py
───────────────────────────────────────────
Milestone 1: the eight read-only STAFF endpoints for patients, orders, consents and samples, over HTTP against
PostgreSQL.

WHAT IS PROVED, in the order the classes ask it:

  1. The ROLE MATRIX, for all eight routes and all seven principals: the roles the matrix names read; every other
     role -- Administrator and a user holding no role included -- gets the SAME 403 body, and gets it WHETHER OR NOT
     the identifier names anything (the gate runs first).
  2. ORGANISATION ISOLATION: another organisation's row reads exactly like one that does not exist (404,
     byte-identical), no list ever contains it, and no query string can change the organisation.
  3. Each resource's CONTENT: fields, filters, search, pagination and ordering; what a role may and may not see
     (clinical indication, QC reason, staff user ids); and that a consent's `status` is a display of the row.
  4. AUDIT: every read, refused or permitted, leaves one row naming actor, organisation, action and resource, and
     NO payload -- no name, search term, date of birth, indication or consent detail.
  5. Parameters are validated AFTER the gate.

The two-organisation world and the login helpers are w162's, imported rather than copied.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from clinical.tests import test_w162_registration_and_consent as _w162
from clinical.tests.test_w162_registration_and_consent import (
    MALFORMED,
    NOWHERE,
    STAFF_COOKIE,
    _staff_cookie,
)
from clinical.tests.test_w166_catalogue_and_orders import _make_test, _order_body

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

# THE APPROVED MATRIX. Administrator appears nowhere: it manages users and settings and has no clinical read access.
ALLOWED = {
    "patients": ("orderer", "interpreter", "approver"),
    "orders": ("orderer", "labtech", "interpreter", "approver", "auditor"),
    "consents": ("orderer", "interpreter", "approver", "auditor"),
    "samples": ("labtech", "orderer", "interpreter", "approver", "auditor"),
}
CLINICAL_ORDER_ROLES = ("orderer", "interpreter", "approver")
QC_REASON_ROLES = ("labtech", "orderer", "interpreter", "approver")

INDICATION = "indication-marker-brca1-family-history"
QC_REASON = "qc-reason-marker-low-yield"


# ─── helpers ─────────────────────────────────────────────────────────────────


def _cookie(client, world, principal, tag="a"):
    return {STAFF_COOKIE: _staff_cookie(client, world[tag]["org_id"], world[tag][f"{principal}_email"])}


def _get(client, world, principal, path, tag="a"):
    return client.get(path, cookies=_cookie(client, world, principal, tag))


def _audit(conn, like="staff_%"):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT action, outcome, resource_type, resource_id, details::text, org_id, user_id "
            "FROM audit_log WHERE action LIKE %s ORDER BY log_id",
            (like,),
        )
        rows = cur.fetchall()
    conn.rollback()
    return rows


@pytest.fixture()
def seeded(client, dao, conn, world):
    """
    Organisation A: the world's patient (one active and one withdrawn consent), plus a second patient with its own
    consent; two orders (one placed with a received, QC-passed sample, one draft); and in organisation B the
    world's own patient, consent and one order, so every query has a neighbour that must never appear.
    """
    a, b = world["a"], world["b"]
    second = dao.create_patient(a["orderer"], "Second Patient", dt.date(1985, 5, 5), "M")
    second_consent = dao.record_consent(a["orderer"], second, "testing")
    conn.commit()

    test_a = _make_test(client, world, "a")
    placed = client.post(
        "/clinic/orders",
        json=_order_body(world, "a", test_a, clinical_indication=INDICATION),
        cookies=_cookie(client, world, "orderer"),
    )
    assert placed.status_code == 201, placed.text
    order_id = placed.json()["order_id"]
    orderer = _cookie(client, world, "orderer")
    assert client.post(f"/clinic/orders/{order_id}/placement", cookies=orderer).status_code == 204
    sample = client.post(
        f"/lab/orders/{order_id}/samples", json={"sample_type": "blood"}, cookies=_cookie(client, world, "labtech")
    )
    assert sample.status_code == 201, sample.text
    sample_id = sample.json()["sample_id"]
    qc = client.post(
        f"/lab/samples/{sample_id}/qc",
        json={"qc_status": "passed", "qc_reason": QC_REASON},
        cookies=_cookie(client, world, "labtech"),
    )
    assert qc.status_code == 204, qc.text

    draft = client.post(
        "/clinic/orders",
        json={
            "patient_id": str(second),
            "test_id": test_a,
            "consent_id": str(second_consent),
            "required_scope": "testing",
            "priority": "urgent",
        },
        cookies=orderer,
    )
    assert draft.status_code == 201, draft.text

    test_b = _make_test(client, world, "b")
    b_order = client.post(
        "/clinic/orders", json=_order_body(world, "b", test_b), cookies=_cookie(client, world, "orderer", "b")
    )
    assert b_order.status_code == 201, b_order.text
    b_placed = client.post(
        f"/clinic/orders/{b_order.json()['order_id']}/placement", cookies=_cookie(client, world, "orderer", "b")
    )
    assert b_placed.status_code == 204, b_placed.text
    b_sample = client.post(
        f"/lab/orders/{b_order.json()['order_id']}/samples",
        json={"sample_type": "blood"},
        cookies=_cookie(client, world, "labtech", "b"),
    )
    assert b_sample.status_code == 201, b_sample.text
    return {
        "patient": str(a["patient"]),
        "second": str(second),
        "consent": str(a["consent"]),
        "withdrawn": str(a["withdrawn_consent"]),
        "second_consent": str(second_consent),
        "test": test_a,
        "order": order_id,
        "draft": draft.json()["order_id"],
        "sample": sample_id,
        "b_patient": str(b["patient"]),
        "b_consent": str(b["consent"]),
        "b_order": b_order.json()["order_id"],
        "b_sample": b_sample.json()["sample_id"],
    }


def _routes(ids):
    """resource -> (list route, detail route)"""
    return {
        "patients": ("/clinic/patients", f"/clinic/patients/{ids['patient']}"),
        "orders": ("/clinic/orders", f"/clinic/orders/{ids['order']}"),
        "consents": (f"/clinic/patients/{ids['patient']}/consents", f"/clinic/consents/{ids['consent']}"),
        "samples": ("/lab/samples", f"/lab/samples/{ids['sample']}"),
    }


# ─── 1. the role matrix ──────────────────────────────────────────────────────


class TestTheRoleMatrix:
    def test_every_route_admits_exactly_the_roles_the_matrix_names(self, client, world, seeded):
        for resource, paths in _routes(seeded).items():
            for path in paths:
                refusals = set()
                for principal in PRINCIPALS:
                    response = _get(client, world, principal, path)
                    if principal in ALLOWED[resource]:
                        assert response.status_code == 200, (path, principal, response.text)
                    else:
                        assert response.status_code == 403, (path, principal, response.text)
                        assert response.json() == FORBIDDEN, (path, principal)
                        refusals.add(response.content)
                assert len(refusals) == 1, (path, "the refusal body varies")

    def test_administrator_has_no_clinical_read_access_on_any_route(self, client, world, seeded):
        for paths in _routes(seeded).values():
            for path in paths:
                assert _get(client, world, "admin", path).json() == FORBIDDEN, path

    def test_the_gate_runs_before_the_lookup_so_a_refusal_never_depends_on_the_identifier(self, client, world, seeded):
        """A refused role gets the identical 403 for a real id, an invented one and a malformed one."""
        for resource, (_, detail) in _routes(seeded).items():
            denied = next(p for p in PRINCIPALS if p not in ALLOWED[resource])
            stem = detail.rsplit("/", 1)[0]
            answers = {
                _get(client, world, denied, f"{stem}/{ident}").content
                for ident in (detail.rsplit("/", 1)[1], NOWHERE, MALFORMED)
            }
            assert len(answers) == 1, (resource, denied)

    def test_anonymous_and_a_forged_session_are_refused_before_any_of_it(self, client, world, seeded):
        for paths in _routes(seeded).values():
            for path in paths:
                anonymous = client.get(path)
                forged = client.get(path, cookies={STAFF_COOKIE: "not-a-session"})
                assert anonymous.status_code == forged.status_code == 401, path
                assert anonymous.content == forged.content, path

    def test_there_is_no_organisation_wide_consent_list(self, client, world, seeded):
        assert _get(client, world, "orderer", "/clinic/consents").status_code in (404, 405)


# ─── 2. organisation isolation ───────────────────────────────────────────────


class TestOrganisationIsolation:
    def test_another_organisations_row_reads_exactly_like_one_that_does_not_exist(self, client, world, seeded):
        cases = [
            ("orderer", "/clinic/patients/{}", seeded["b_patient"]),
            ("orderer", "/clinic/orders/{}", seeded["b_order"]),
            ("orderer", "/clinic/consents/{}", seeded["b_consent"]),
            ("labtech", "/lab/samples/{}", seeded["b_sample"]),
            ("orderer", "/clinic/patients/{}/consents", seeded["b_patient"]),
        ]
        for principal, template, foreign in cases:
            real = _get(client, world, principal, template.format(foreign))
            invented = _get(client, world, principal, template.format(NOWHERE))
            malformed = _get(client, world, principal, template.format(MALFORMED))
            assert real.status_code == invented.status_code == malformed.status_code == 404, template
            assert real.content == invented.content == malformed.content, template
            assert real.json() == NOT_FOUND, template
            assert foreign not in real.text

    def test_no_list_ever_contains_the_other_organisations_rows(self, client, world, seeded):
        patients = _get(client, world, "orderer", "/clinic/patients").json()
        assert seeded["b_patient"] not in {i["patient_id"] for i in patients["items"]}
        assert patients["total"] == 2 and {i["name"] for i in patients["items"]} == {"Patient A", "Second Patient"}
        orders = _get(client, world, "orderer", "/clinic/orders").json()
        assert seeded["b_order"] not in {i["order_id"] for i in orders["items"]} and orders["total"] == 2
        samples = _get(client, world, "labtech", "/lab/samples").json()
        assert seeded["b_sample"] not in {i["sample_id"] for i in samples["items"]} and samples["total"] == 1

    def test_the_other_organisations_user_sees_only_their_own(self, client, world, seeded):
        patients = _get(client, world, "orderer", "/clinic/patients", tag="b").json()
        assert [i["name"] for i in patients["items"]] == ["Patient B"]
        assert _get(client, world, "orderer", f"/clinic/patients/{seeded['patient']}", tag="b").status_code == 404

    def test_a_query_string_cannot_change_the_organisation_or_widen_the_read(self, client, world, seeded):
        a, b = world["a"], world["b"]
        for extra in (f"org_id={b['org_id']}", f"organisation_id={b['org_id']}", f"patient_id={seeded['b_patient']}"):
            body = _get(client, world, "orderer", f"/clinic/orders?{extra}").json()
            assert seeded["b_order"] not in {i["order_id"] for i in body["items"]}, extra
        body = _get(client, world, "orderer", f"/clinic/patients?org_id={b['org_id']}").json()
        assert seeded["b_patient"] not in {i["patient_id"] for i in body["items"]}
        assert a["org_id"] != b["org_id"]

    def test_filtering_by_another_organisations_id_is_an_empty_page_like_an_invented_one(self, client, world, seeded):
        for path, key in (
            ("/clinic/orders?patient_id={}", seeded["b_patient"]),
            ("/clinic/orders?test_id={}", NOWHERE),
            ("/lab/samples?order_id={}", seeded["b_order"]),
        ):
            real = _get(client, world, "orderer", path.format(key)).json()
            invented = _get(client, world, "orderer", path.format(NOWHERE)).json()
            assert real == invented and real["items"] == [] and real["total"] == 0, path
        malformed = _get(client, world, "orderer", "/clinic/orders?patient_id=not-a-uuid").json()
        assert malformed["items"] == [] and malformed["total"] == 0


# ─── 3a. patients ────────────────────────────────────────────────────────────


class TestPatients:
    def test_detail_returns_exactly_the_patient_record(self, client, world, seeded):
        body = _get(client, world, "orderer", f"/clinic/patients/{seeded['patient']}").json()
        assert set(body) == {"patient_id", "name", "dob", "sex", "created_at", "disabled"}
        assert body["patient_id"] == seeded["patient"] and body["name"] == "Patient A"
        assert body["dob"] == "1990-01-01" and body["sex"] == "F" and body["disabled"] is False

    def test_list_is_newest_first_with_total_limit_and_offset(self, client, world, seeded):
        page = _get(client, world, "orderer", "/clinic/patients").json()
        assert set(page) == {"items", "total", "limit", "offset"}
        assert (page["total"], page["limit"], page["offset"]) == (2, 50, 0)
        assert [i["name"] for i in page["items"]] == ["Second Patient", "Patient A"]

    def test_pagination_pages_are_disjoint_and_complete(self, client, dao, conn, world, seeded):
        for n in range(5):
            dao.create_patient(world["a"]["orderer"], f"Bulk {n}", dt.date(2000, 1, 1 + n), "U")
        conn.commit()
        seen = []
        for offset in (0, 3, 6):
            page = _get(client, world, "orderer", f"/clinic/patients?limit=3&offset={offset}").json()
            assert page["total"] == 7 and page["limit"] == 3 and page["offset"] == offset
            seen.extend(i["patient_id"] for i in page["items"])
        assert len(seen) == len(set(seen)) == 7
        beyond = _get(client, world, "orderer", "/clinic/patients?limit=3&offset=30").json()
        assert beyond["items"] == [] and beyond["total"] == 7

    def test_search_is_a_case_insensitive_contains_on_the_name_only(self, client, world, seeded):
        for term in ("second", "SECOND", "cond pat", "Patient"):
            names = {i["name"] for i in _get(client, world, "orderer", f"/clinic/patients?q={term}").json()["items"]}
            assert "Second Patient" in names, term
        assert _get(client, world, "orderer", "/clinic/patients?q=Patient%20B").json()["total"] == 0
        by_dob = _get(client, world, "orderer", "/clinic/patients?q=1985").json()
        assert by_dob["total"] == 0, "the search must not match the date of birth"
        by_id = _get(client, world, "orderer", f"/clinic/patients?q={seeded['patient'][:8]}").json()
        assert by_id["total"] == 0, "the search must not match the identifier"

    def test_search_wildcards_are_literal(self, client, dao, conn, world, seeded):
        dao.create_patient(world["a"]["orderer"], "100% Percent_Name", dt.date(1999, 9, 9), "O")
        conn.commit()
        assert _get(client, world, "orderer", "/clinic/patients?q=100%25").json()["total"] == 1
        assert _get(client, world, "orderer", "/clinic/patients?q=ent_Na").json()["total"] == 1
        assert _get(client, world, "orderer", "/clinic/patients?q=%25%25").json()["total"] == 0
        assert _get(client, world, "orderer", "/clinic/patients?q=__").json()["total"] == 0

    def test_sex_filter(self, client, world, seeded):
        assert [i["name"] for i in _get(client, world, "orderer", "/clinic/patients?sex=M").json()["items"]] == [
            "Second Patient"
        ]

    def test_disabled_patients_are_included_by_default_and_the_flag_is_exposed(self, client, conn, world, seeded):
        with conn.cursor() as cur:
            cur.execute("UPDATE patients SET disabled = true WHERE patient_id = %s", (seeded["second"],))
        conn.commit()
        everyone = _get(client, world, "orderer", "/clinic/patients").json()
        assert everyone["total"] == 2
        assert {i["name"]: i["disabled"] for i in everyone["items"]} == {"Second Patient": True, "Patient A": False}
        only_disabled = _get(client, world, "orderer", "/clinic/patients?disabled=true").json()
        assert [i["name"] for i in only_disabled["items"]] == ["Second Patient"]
        only_active = _get(client, world, "orderer", "/clinic/patients?disabled=false").json()
        assert [i["name"] for i in only_active["items"]] == ["Patient A"]
        assert _get(client, world, "orderer", f"/clinic/patients/{seeded['second']}").json()["disabled"] is True


# ─── 3b. orders ──────────────────────────────────────────────────────────────


class TestOrders:
    def test_detail_fields_and_what_each_role_may_see(self, client, world, seeded):
        for principal in ALLOWED["orders"]:
            body = _get(client, world, principal, f"/clinic/orders/{seeded['order']}").json()
            assert set(body) == {
                "order_id", "patient_id", "test_id", "required_scope", "consent_id", "priority", "state",
                "placed_at", "cancelled_at", "created_at", "ordered_by", "clinical_indication",
            }, principal  # fmt: skip
            assert body["order_id"] == seeded["order"] and body["state"] != "draft" and body["placed_at"]
            assert body["patient_id"] == seeded["patient"] and body["consent_id"] == seeded["consent"]
            if principal in CLINICAL_ORDER_ROLES:
                assert body["clinical_indication"] == INDICATION and body["ordered_by"], principal
            else:
                assert body["clinical_indication"] is None and body["ordered_by"] is None, principal

    def test_a_list_never_carries_the_clinical_indication_or_the_ordering_user(self, client, world, seeded):
        for principal in ALLOWED["orders"]:
            response = _get(client, world, principal, "/clinic/orders")
            assert INDICATION not in response.text, principal
            for item in response.json()["items"]:
                assert item["clinical_indication"] is None and item["ordered_by"] is None, principal

    def test_filters(self, client, world, seeded):
        def ids(query):
            return {i["order_id"] for i in _get(client, world, "orderer", f"/clinic/orders?{query}").json()["items"]}

        assert ids(f"patient_id={seeded['patient']}") == {seeded["order"]}
        assert ids(f"patient_id={seeded['second']}") == {seeded["draft"]}
        assert ids("priority=urgent") == {seeded["draft"]}
        assert ids("state=draft") == {seeded["draft"]}
        assert ids(f"test_id={seeded['test']}") == {seeded["order"], seeded["draft"]}
        assert ids("state=cancelled") == set()
        today = dt.datetime.now(dt.timezone.utc).date().isoformat()
        assert ids(f"created_from={today}&created_to={today}") == {seeded["order"], seeded["draft"]}, (
            "a bare date means the whole day"
        )
        assert ids("created_to=2000-01-01") == set()
        assert ids("created_from=2999-01-01") == set()

    def test_list_is_newest_first_with_a_stable_total(self, client, world, seeded):
        page = _get(client, world, "orderer", "/clinic/orders?limit=1").json()
        assert page["total"] == 2 and page["limit"] == 1 and len(page["items"]) == 1
        assert page["items"][0]["order_id"] == seeded["draft"]
        second = _get(client, world, "orderer", "/clinic/orders?limit=1&offset=1").json()
        assert second["items"][0]["order_id"] == seeded["order"]


# ─── 3c. consents ────────────────────────────────────────────────────────────


class TestConsents:
    def test_detail_hides_the_staff_user_ids(self, client, world, seeded):
        for principal in ALLOWED["consents"]:
            body = _get(client, world, principal, f"/clinic/consents/{seeded['consent']}").json()
            assert set(body) == {
                "consent_id",
                "patient_id",
                "scope",
                "status",
                "recorded_at",
                "withdrawn_at",
                "superseded_by",
            }
            assert body["status"] == "active" and body["scope"] == "testing" and body["withdrawn_at"] is None
            assert "recorded_by" not in body and "withdrawn_by" not in body
        text = _get(client, world, "auditor", f"/clinic/consents/{seeded['withdrawn']}").text
        assert str(world["a"]["orderer_id"]) not in text, "the staff user id leaked"

    def test_a_withdrawn_consent_displays_as_withdrawn(self, client, world, seeded):
        body = _get(client, world, "approver", f"/clinic/consents/{seeded['withdrawn']}").json()
        assert body["status"] == "withdrawn" and body["withdrawn_at"] is not None and body["scope"] == "research"

    def test_a_superseded_consent_displays_as_superseded_and_withdrawn_takes_precedence(
        self, client, dao, conn, world, seeded
    ):
        orderer = world["a"]["orderer"]
        newer = dao.record_consent(orderer, world["a"]["patient"], "testing", supersedes_id=world["a"]["consent"])
        conn.commit()
        old = _get(client, world, "orderer", f"/clinic/consents/{seeded['consent']}").json()
        assert old["status"] == "superseded" and old["superseded_by"] == str(newer)
        assert _get(client, world, "orderer", f"/clinic/consents/{newer}").json()["status"] == "active"
        # withdrawn AND superseded: the more restrictive display wins
        dao.withdraw_consent(orderer, newer)
        newest = dao.record_consent(orderer, world["a"]["patient"], "testing", supersedes_id=newer)
        conn.commit()
        both = _get(client, world, "orderer", f"/clinic/consents/{newer}").json()
        assert both["status"] == "withdrawn" and both["superseded_by"] == str(newest)

    def test_the_patients_consents_are_listed_newest_first_and_filterable(self, client, world, seeded):
        page = _get(client, world, "orderer", f"/clinic/patients/{seeded['patient']}/consents").json()
        assert page["total"] == 2
        assert {i["consent_id"]: i["status"] for i in page["items"]} == {
            seeded["consent"]: "active",
            seeded["withdrawn"]: "withdrawn",
        }
        assert all(i["patient_id"] == seeded["patient"] for i in page["items"])

        def statuses(query):
            return [
                i["consent_id"]
                for i in _get(
                    client, world, "orderer", f"/clinic/patients/{seeded['patient']}/consents?{query}"
                ).json()["items"]
            ]

        assert statuses("status=withdrawn") == [seeded["withdrawn"]]
        assert statuses("status=active") == [seeded["consent"]]
        assert statuses("status=superseded") == []
        assert statuses("scope=research") == [seeded["withdrawn"]]
        assert statuses("scope=testing&status=active") == [seeded["consent"]]

    def test_a_list_holds_only_that_patients_consents(self, client, world, seeded):
        page = _get(client, world, "orderer", f"/clinic/patients/{seeded['second']}/consents").json()
        assert [i["consent_id"] for i in page["items"]] == [seeded["second_consent"]]

    def test_an_unknown_status_is_a_400_for_a_permitted_role(self, client, world, seeded):
        response = _get(client, world, "orderer", f"/clinic/patients/{seeded['patient']}/consents?status=bogus")
        assert response.status_code == 400 and "status" in response.json()["detail"]


# ─── 3d. samples ─────────────────────────────────────────────────────────────


class TestSamples:
    def test_detail_fields_and_the_qc_reason_by_role(self, client, world, seeded):
        for principal in ALLOWED["samples"]:
            body = _get(client, world, principal, f"/lab/samples/{seeded['sample']}").json()
            assert set(body) == {
                "sample_id", "order_id", "type", "received_at", "condition_on_receipt", "qc_status",
                "qc_recorded_at", "created_at", "qc_reason",
            }, principal  # fmt: skip
            assert body["order_id"] == seeded["order"] and body["type"] == "blood" and body["qc_status"] == "passed"
            assert body["qc_reason"] == (QC_REASON if principal in QC_REASON_ROLES else None), principal

    def test_the_staff_user_ids_are_never_shown(self, client, world, seeded):
        for path in (f"/lab/samples/{seeded['sample']}", "/lab/samples"):
            text = _get(client, world, "labtech", path).text
            for key in ("labtech_id", "orderer_id"):
                assert str(world["a"][key]) not in text, (path, key)

    def test_a_list_never_carries_the_qc_reason(self, client, world, seeded):
        for principal in ALLOWED["samples"]:
            response = _get(client, world, principal, "/lab/samples")
            assert QC_REASON not in response.text, principal
            assert all(i["qc_reason"] is None for i in response.json()["items"]), principal

    def test_filters_and_paging(self, client, world, seeded):
        listing = _get(client, world, "labtech", "/lab/samples").json()
        assert listing["total"] == 1 and listing["items"][0]["sample_id"] == seeded["sample"]
        assert _get(client, world, "labtech", f"/lab/samples?order_id={seeded['order']}").json()["total"] == 1
        assert _get(client, world, "labtech", f"/lab/samples?order_id={seeded['draft']}").json()["total"] == 0
        assert _get(client, world, "labtech", "/lab/samples?qc_status=passed").json()["total"] == 1
        assert _get(client, world, "labtech", "/lab/samples?qc_status=failed").json()["total"] == 0
        assert _get(client, world, "labtech", "/lab/samples?limit=1&offset=1").json()["items"] == []


# ─── 4. audit ────────────────────────────────────────────────────────────────


class TestEveryReadIsAuditedWithNoPayload:
    def test_each_of_the_eight_routes_leaves_one_row_naming_actor_organisation_action_and_resource(
        self, client, conn, world, seeded
    ):
        a = world["a"]
        reads = [
            ("orderer", "/clinic/patients?q=Patient&sex=F", "staff_patient_list", "patient", "<none>"),
            ("orderer", f"/clinic/patients/{seeded['patient']}", "staff_patient_read", "patient", seeded["patient"]),
            ("orderer", "/clinic/orders", "staff_order_list", "order", "<none>"),
            ("orderer", f"/clinic/orders/{seeded['order']}", "staff_order_read", "order", seeded["order"]),
            (
                "orderer",
                f"/clinic/patients/{seeded['patient']}/consents",
                "staff_consent_list",
                "patient",
                seeded["patient"],
            ),
            ("orderer", f"/clinic/consents/{seeded['consent']}", "staff_consent_read", "consent", seeded["consent"]),
            ("labtech", "/lab/samples", "staff_sample_list", "sample", "<none>"),
            ("labtech", f"/lab/samples/{seeded['sample']}", "staff_sample_read", "sample", seeded["sample"]),
        ]
        before = len(_audit(conn))
        for principal, path, *_ in reads:
            assert _get(client, world, principal, path).status_code == 200, path
        rows = _audit(conn)[before:]
        assert len(rows) == len(reads)
        for (principal, _, action, rtype, rid), row in zip(reads, rows):
            assert row[0] == action and row[1] == "success" and row[2] == rtype and row[3] == rid, (action, row)
            assert row[4] == "{}", (action, row[4])
            assert row[5] == a["org_id"] and row[6] == a[f"{principal}_id"], (action, row)

    def test_a_refusal_is_audited_as_denied_with_no_payload(self, client, conn, world, seeded):
        assert _get(client, world, "admin", f"/clinic/patients/{seeded['patient']}").status_code == 403
        assert _get(client, world, "nobody", "/clinic/orders").status_code == 403
        rows = [r for r in _audit(conn) if r[1] == "denied"]
        assert {r[0] for r in rows} == {"staff_patient_read", "staff_order_list"}
        assert all(r[4] == "{}" for r in rows)

    def test_nothing_identifying_reaches_the_audit_log(self, client, conn, world, seeded):
        _get(client, world, "orderer", "/clinic/patients?q=Second")
        _get(client, world, "orderer", f"/clinic/patients/{seeded['second']}")
        _get(client, world, "orderer", f"/clinic/orders/{seeded['order']}")
        _get(client, world, "labtech", f"/lab/samples/{seeded['sample']}")
        _get(client, world, "orderer", f"/clinic/patients/{seeded['patient']}/consents?scope=research&status=withdrawn")
        dump = json.dumps(_audit(conn), default=str)
        for secret in ("Second", "Patient A", "1985", "1990", INDICATION, QC_REASON, "research", "withdrawn"):
            assert secret not in dump, secret

    def test_an_unknown_id_is_still_a_row_and_a_404(self, client, conn, world, seeded):
        assert _get(client, world, "orderer", f"/clinic/patients/{NOWHERE}").status_code == 404
        rows = _audit(conn)
        assert rows[-1][0] == "staff_patient_read" and rows[-1][3] == NOWHERE


# ─── 5. parameters are validated after the gate ──────────────────────────────


class TestParametersAreValidatedAfterTheGate:
    BAD = (
        "limit=0",
        "limit=201",
        "limit=abc",
        "limit=1.5",
        "offset=-1",
        "offset=x",
        "q=a",
        "disabled=maybe",
        "created_from=yesterday",
    )

    def test_a_permitted_role_gets_a_400_with_fixed_text(self, client, world, seeded):
        for query in self.BAD:
            path = (
                "/clinic/patients" if query.split("=")[0] in ("limit", "offset", "q", "disabled") else "/clinic/orders"
            )
            response = _get(client, world, "orderer", f"{path}?{query}")
            assert response.status_code == 400, query
            assert response.json()["detail"], query

    def test_the_same_bad_parameters_do_not_change_a_refused_roles_answer(self, client, world, seeded):
        for query in self.BAD:
            path = (
                "/clinic/patients" if query.split("=")[0] in ("limit", "offset", "q", "disabled") else "/clinic/orders"
            )
            for principal in ("admin", "nobody"):
                response = _get(client, world, principal, f"{path}?{query}")
                assert response.status_code == 403 and response.json() == FORBIDDEN, (principal, query)

    def test_limit_is_bounded_at_two_hundred(self, client, world, seeded):
        assert _get(client, world, "orderer", "/clinic/patients?limit=200").json()["limit"] == 200
        assert _get(client, world, "orderer", "/clinic/patients?limit=1").json()["limit"] == 1
