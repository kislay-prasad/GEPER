"""
clinical/tests/test_w166_catalogue_and_orders.py
────────────────────────────────────────────────
Stage 2 of the backend completion: the test catalogue and the order stage,
over HTTP, against PostgreSQL.

WHAT IS PROVED:
  * an Administrator can create and retire catalogue entries; nobody else can,
    and every other principal gets the SAME 403 body whether or not the
    target exists;
  * an Orderer sees the catalogue and can create, place and cancel an order for
    a patient of their own organisation using an active test of their own
    organisation and an active consent of that patient;
  * a patient, test, consent or order belonging to ANOTHER organisation reads
    exactly like one that does not exist (404, byte-identical body);
  * a retired test cannot be ordered, and a malformed identifier meets the
    role gate before it is judged;
  * the audit rows name the acting user.

The two-organisation world and the login helpers are w162's, imported rather
than copied. Orders have no domain reader, so the order row itself is read back
with one narrow SELECT and the limit is stated where it is used.
"""

from __future__ import annotations

import pytest

from clinical.admin_endpoints import ADMIN_FORBIDDEN, ADMIN_NOT_FOUND
from clinical.tests import test_w162_registration_and_consent as _w162
from clinical.tests.test_w162_registration_and_consent import (
    MALFORMED,
    NOT_THE_CLINICIAN,
    NOWHERE,
    STAFF_COOKIE,
    _staff_cookie,
)

# Fixtures shared from test_w162_registration_and_consent. They are BOUND AS NAMES rather than imported:
# pytest discovers a fixture by the attribute it finds in this module, so the
# assignment registers it exactly as an import would, while ruff (F811) does not
# mistake each test's use of the fixture as a parameter for a redefinition of an
# unused import. Plain constants and helpers are imported normally above.
client = _w162.client
clinical_dsn = _w162.clinical_dsn
conn = _w162.conn
dao = _w162.dao
reader = _w162.reader
reset_the_login_throttle = _w162.reset_the_login_throttle
world = _w162.world

NOT_THE_ADMINISTRATOR = ("orderer", "labtech", "auditor", "interpreter", "approver", "nobody")
GOOD_TEST = {"name": "BRCA panel", "assembly": "hg38", "genes": ["BRCA1", "BRCA2"], "cdsco_class": "B"}


def _cookies(cookie):
    return {STAFF_COOKIE: cookie} if cookie is not None else {}


def _make_test(client, world, tag="a", **overrides):
    cookie = _staff_cookie(client, world[tag]["org_id"], world[tag]["admin_email"])
    body = {**GOOD_TEST, **overrides}
    response = client.post("/admin/tests", json=body, cookies=_cookies(cookie))
    assert response.status_code == 201, response.text
    return response.json()["test_id"]


def _order_body(world, tag, test_id, **overrides):
    body = {
        "patient_id": str(world[tag]["patient"]),
        "test_id": test_id,
        "consent_id": str(world[tag]["consent"]),
        "required_scope": "testing",
    }
    body.update(overrides)
    return body


def _orderer_cookie(client, world, tag="a"):
    return _staff_cookie(client, world[tag]["org_id"], world[tag]["orderer_email"])


def _order_row(conn, order_id):
    # The one place raw SQL stands in for a domain reader: no method reads an
    # order back. It selects the columns this file asserts on and nothing else.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT org_id, patient_id, test_id, consent_id, ordered_by, state, priority FROM orders WHERE order_id = %s",
            (order_id,),
        )
        return cur.fetchone()


class TestTheAdministratorManagesTheCatalogue:
    def test_create_then_list_shows_the_entry_with_a_canonical_assembly(self, client, world, reader):
        test_id = _make_test(client, world, genes=["BRCA1", "brca1", "TP53"])
        listed = client.get("/clinic/tests", cookies=_cookies(_orderer_cookie(client, world)))
        assert listed.status_code == 200, listed.text
        entry = next(t for t in listed.json() if t["test_id"] == test_id)
        assert entry["name"] == "BRCA panel"
        assert entry["assembly"] == "GRCh38"  # 'hg38' stored canonical
        assert entry["status"] == "active"
        assert sorted(entry["genes"]) == ["BRCA1", "TP53"]  # case-insensitive de-dup

        via_domain = reader.list_tests(world["a"]["admin"])
        assert [t["test_id"] for t in via_domain if str(t["test_id"]) == test_id]

    def test_creation_is_audited_with_the_acting_administrator(self, client, world, conn):
        test_id = _make_test(client, world)
        with conn.cursor() as cur:
            cur.execute("SELECT user_id, actor_role, org_id, outcome FROM audit_log WHERE action = 'test_created'")
            rows = cur.fetchall()
        assert len(rows) == 1
        assert rows[0][0] == world["a"]["admin_id"]
        assert rows[0][2] == world["a"]["org_id"]
        assert test_id

    def test_the_creation_row_names_the_new_test_as_its_resource(self, client, world, conn):
        """`create_test` returns the new test's UUID and must OPT IN to having it
        recorded (`resource_id_from_result=True`): an `@auditable` method records no
        return value unless it says so, because a return value can be a payload (the
        w121 TOTP backup codes). Without the opt-in this row reads '<none>'."""
        test_id = _make_test(client, world)
        with conn.cursor() as cur:
            cur.execute("SELECT resource_type, resource_id FROM audit_log WHERE action = 'test_created'")
            rows = cur.fetchall()
        assert rows == [("test", test_id)], rows

    @pytest.mark.parametrize("who", NOT_THE_ADMINISTRATOR)
    def test_no_other_principal_can_create_and_the_refusal_is_uniform(self, client, world, who):
        cookie = _staff_cookie(client, world["a"]["org_id"], world["a"][f"{who}_email"])
        response = client.post("/admin/tests", json=GOOD_TEST, cookies=_cookies(cookie))
        assert response.status_code == 403
        assert response.json()["detail"] == ADMIN_FORBIDDEN

    def test_nothing_was_created_by_the_refused_attempts(self, client, world, reader):
        for who in NOT_THE_ADMINISTRATOR:
            cookie = _staff_cookie(client, world["a"]["org_id"], world["a"][f"{who}_email"])
            client.post("/admin/tests", json=GOOD_TEST, cookies=_cookies(cookie))
        assert reader.list_tests(world["a"]["admin"], include_retired=True) == []

    def test_unauthenticated_is_401(self, client, world):
        assert client.post("/admin/tests", json=GOOD_TEST).status_code == 401
        assert client.get("/clinic/tests").status_code == 401

    @pytest.mark.parametrize(
        "overrides",
        [
            {"name": "   "},
            {"name": "x" * 201},
            {"assembly": "hg99"},
            {"assembly": ""},
            {"genes": []},
            {"genes": ["BRCA1", "not a gene!"]},
            {"genes": ["1BRCA"]},
            {"genes": ["A" * 26]},
        ],
    )
    def test_invalid_entries_are_refused_with_400(self, client, world, overrides):
        cookie = _staff_cookie(client, world["a"]["org_id"], world["a"]["admin_email"])
        response = client.post("/admin/tests", json={**GOOD_TEST, **overrides}, cookies=_cookies(cookie))
        assert response.status_code == 400, response.text

    def test_a_body_naming_an_organisation_or_approver_is_refused(self, client, world):
        cookie = _staff_cookie(client, world["a"]["org_id"], world["a"]["admin_email"])
        for extra in ({"org_id": str(world["b"]["org_id"])}, {"approved_by": str(world["a"]["admin_id"])}):
            response = client.post("/admin/tests", json={**GOOD_TEST, **extra}, cookies=_cookies(cookie))
            assert response.status_code == 422

    def test_a_duplicate_active_name_is_refused_but_a_retired_name_can_be_reused(self, client, world):
        first = _make_test(client, world)
        cookie = _staff_cookie(client, world["a"]["org_id"], world["a"]["admin_email"])
        dup = client.post("/admin/tests", json={**GOOD_TEST, "name": "brca PANEL"}, cookies=_cookies(cookie))
        assert dup.status_code == 400
        assert client.post(f"/admin/tests/{first}/retirement", cookies=_cookies(cookie)).status_code == 204
        assert client.post("/admin/tests", json=GOOD_TEST, cookies=_cookies(cookie)).status_code == 201

    def test_the_other_organisation_may_use_the_same_name(self, client, world):
        _make_test(client, world, "a")
        _make_test(client, world, "b")


class TestTheCatalogueIsTenantScoped:
    def test_another_organisations_catalogue_is_not_listed(self, client, world):
        a_test = _make_test(client, world, "a")
        listed = client.get("/clinic/tests", cookies=_cookies(_orderer_cookie(client, world, "b")))
        assert listed.status_code == 200
        assert a_test not in [t["test_id"] for t in listed.json()]

    def test_another_organisations_administrator_cannot_retire_and_it_reads_as_nothing(self, client, world, reader):
        a_test = _make_test(client, world, "a")
        b_admin = _staff_cookie(client, world["b"]["org_id"], world["b"]["admin_email"])
        real = client.post(f"/admin/tests/{a_test}/retirement", cookies=_cookies(b_admin))
        invented = client.post(f"/admin/tests/{NOWHERE}/retirement", cookies=_cookies(b_admin))
        assert real.status_code == invented.status_code == 404
        assert real.content == invented.content
        assert real.json()["detail"] == ADMIN_NOT_FOUND
        assert [t["status"] for t in reader.list_tests(world["a"]["admin"])] == ["active"]

    def test_retirement_is_admin_only_and_gate_first_for_a_malformed_id(self, client, world):
        a_test = _make_test(client, world, "a")
        orderer = _orderer_cookie(client, world)
        for target in (a_test, NOWHERE, MALFORMED):
            response = client.post(f"/admin/tests/{target}/retirement", cookies=_cookies(orderer))
            assert response.status_code == 403
            assert response.json()["detail"] == ADMIN_FORBIDDEN
        admin = _staff_cookie(client, world["a"]["org_id"], world["a"]["admin_email"])
        assert client.post(f"/admin/tests/{MALFORMED}/retirement", cookies=_cookies(admin)).status_code == 404

    def test_the_catalogue_read_needs_orderer_or_administrator(self, client, world):
        for who in ("labtech", "auditor", "interpreter", "approver", "nobody"):
            cookie = _staff_cookie(client, world["a"]["org_id"], world["a"][f"{who}_email"])
            response = client.get("/clinic/tests", cookies=_cookies(cookie))
            assert response.status_code == 403, who
        admin = _staff_cookie(client, world["a"]["org_id"], world["a"]["admin_email"])
        assert client.get("/clinic/tests", cookies=_cookies(admin)).status_code == 200


class TestTheOrdererCreatesPlacesAndCancelsAnOrder:
    def test_a_draft_order_is_linked_to_the_right_patient_organisation_test_and_orderer(self, client, world, conn):
        test_id = _make_test(client, world)
        response = client.post(
            "/clinic/orders",
            json=_order_body(world, "a", test_id, priority="urgent", clinical_indication="family history"),
            cookies=_cookies(_orderer_cookie(client, world)),
        )
        assert response.status_code == 201, response.text
        row = _order_row(conn, response.json()["order_id"])
        assert row == (
            world["a"]["org_id"],
            world["a"]["patient"],
            __import__("uuid").UUID(test_id),
            world["a"]["consent"],
            world["a"]["orderer_id"],
            "draft",
            "urgent",
        )

    def test_place_then_a_second_placement_is_refused(self, client, world, conn):
        test_id = _make_test(client, world)
        cookie = _cookies(_orderer_cookie(client, world))
        order_id = client.post("/clinic/orders", json=_order_body(world, "a", test_id), cookies=cookie).json()[
            "order_id"
        ]
        assert client.post(f"/clinic/orders/{order_id}/placement", cookies=cookie).status_code == 204
        assert _order_row(conn, order_id)[5] == "placed"
        assert client.post(f"/clinic/orders/{order_id}/placement", cookies=cookie).status_code == 400

    def test_cancel_then_a_second_cancel_is_refused(self, client, world, conn):
        test_id = _make_test(client, world)
        cookie = _cookies(_orderer_cookie(client, world))
        order_id = client.post("/clinic/orders", json=_order_body(world, "a", test_id), cookies=cookie).json()[
            "order_id"
        ]
        assert client.post(f"/clinic/orders/{order_id}/cancellation", cookies=cookie).status_code == 204
        assert _order_row(conn, order_id)[5] == "cancelled"
        assert client.post(f"/clinic/orders/{order_id}/cancellation", cookies=cookie).status_code == 400

    def test_the_audit_rows_name_the_orderer(self, client, world, conn):
        test_id = _make_test(client, world)
        cookie = _cookies(_orderer_cookie(client, world))
        order_id = client.post("/clinic/orders", json=_order_body(world, "a", test_id), cookies=cookie).json()[
            "order_id"
        ]
        client.post(f"/clinic/orders/{order_id}/placement", cookies=cookie)
        with conn.cursor() as cur:
            cur.execute("SELECT action, user_id FROM audit_log WHERE action IN ('order_created','order_placed')")
            rows = sorted(cur.fetchall())
        assert rows == [("order_created", world["a"]["orderer_id"]), ("order_placed", world["a"]["orderer_id"])]

    def test_priority_and_scope_are_validated(self, client, world):
        test_id = _make_test(client, world)
        cookie = _cookies(_orderer_cookie(client, world))
        assert (
            client.post(
                "/clinic/orders", json=_order_body(world, "a", test_id, priority="asap"), cookies=cookie
            ).status_code
            == 400
        )
        assert (
            client.post(
                "/clinic/orders", json=_order_body(world, "a", test_id, required_scope="research"), cookies=cookie
            ).status_code
            == 400
        )

    def test_a_withdrawn_consent_cannot_back_an_order(self, client, world):
        test_id = _make_test(client, world)
        cookie = _cookies(_orderer_cookie(client, world))
        response = client.post(
            "/clinic/orders",
            json=_order_body(
                world, "a", test_id, consent_id=str(world["a"]["withdrawn_consent"]), required_scope="research"
            ),
            cookies=cookie,
        )
        assert response.status_code == 404

    def test_a_retired_test_cannot_be_ordered_and_reads_as_nothing(self, client, world):
        test_id = _make_test(client, world)
        admin = _staff_cookie(client, world["a"]["org_id"], world["a"]["admin_email"])
        assert client.post(f"/admin/tests/{test_id}/retirement", cookies=_cookies(admin)).status_code == 204
        cookie = _cookies(_orderer_cookie(client, world))
        retired = client.post("/clinic/orders", json=_order_body(world, "a", test_id), cookies=cookie)
        invented = client.post("/clinic/orders", json=_order_body(world, "a", NOWHERE), cookies=cookie)
        assert retired.status_code == invented.status_code == 404
        assert retired.content == invented.content
        listed = client.get("/clinic/tests", cookies=cookie).json()
        assert test_id not in [t["test_id"] for t in listed]


class TestOrdersRefuseEveryoneElse:
    @pytest.mark.parametrize("who", NOT_THE_CLINICIAN)
    def test_only_the_orderer_may_create_place_or_cancel(self, client, world, who):
        test_id = _make_test(client, world)
        orderer = _cookies(_orderer_cookie(client, world))
        order_id = client.post("/clinic/orders", json=_order_body(world, "a", test_id), cookies=orderer).json()[
            "order_id"
        ]

        cookie = _cookies(_staff_cookie(client, world["a"]["org_id"], world["a"][f"{who}_email"]))
        for response in (
            client.post("/clinic/orders", json=_order_body(world, "a", test_id), cookies=cookie),
            client.post(f"/clinic/orders/{order_id}/placement", cookies=cookie),
            client.post(f"/clinic/orders/{order_id}/cancellation", cookies=cookie),
        ):
            assert response.status_code == 403, who
            assert response.json()["detail"] == ADMIN_FORBIDDEN

    def test_unauthenticated_is_401(self, client, world):
        assert client.post("/clinic/orders", json=_order_body(world, "a", NOWHERE)).status_code == 401
        assert client.post(f"/clinic/orders/{NOWHERE}/placement").status_code == 401
        assert client.post(f"/clinic/orders/{NOWHERE}/cancellation").status_code == 401

    def test_malformed_ids_meet_the_gate_first(self, client, world):
        non_orderer = _cookies(_staff_cookie(client, world["a"]["org_id"], world["a"]["labtech_email"]))
        body = _order_body(world, "a", MALFORMED, patient_id=MALFORMED, consent_id=MALFORMED)
        assert client.post("/clinic/orders", json=body, cookies=non_orderer).status_code == 403
        orderer = _cookies(_orderer_cookie(client, world))
        assert client.post("/clinic/orders", json=body, cookies=orderer).status_code == 404

    def test_a_body_naming_an_organisation_or_orderer_is_refused(self, client, world):
        test_id = _make_test(client, world)
        cookie = _cookies(_orderer_cookie(client, world))
        for extra in (
            {"org_id": str(world["b"]["org_id"])},
            {"ordered_by": str(world["b"]["orderer_id"])},
            {"state": "placed"},
        ):
            body = {**_order_body(world, "a", test_id), **extra}
            assert client.post("/clinic/orders", json=body, cookies=cookie).status_code == 422


class TestNoOrderCrossesAnOrganisation:
    def _same_as_invented(self, client, cookie, real_body, invented_body):
        real = client.post("/clinic/orders", json=real_body, cookies=cookie)
        invented = client.post("/clinic/orders", json=invented_body, cookies=cookie)
        assert real.status_code == invented.status_code == 404
        assert real.content == invented.content

    def test_another_organisations_patient_reads_as_nothing(self, client, world, conn):
        test_id = _make_test(client, world, "a")
        cookie = _cookies(_orderer_cookie(client, world, "a"))
        self._same_as_invented(
            client,
            cookie,
            _order_body(world, "a", test_id, patient_id=str(world["b"]["patient"])),
            _order_body(world, "a", test_id, patient_id=NOWHERE),
        )

    def test_another_organisations_consent_reads_as_nothing(self, client, world):
        test_id = _make_test(client, world, "a")
        cookie = _cookies(_orderer_cookie(client, world, "a"))
        self._same_as_invented(
            client,
            cookie,
            _order_body(world, "a", test_id, consent_id=str(world["b"]["consent"])),
            _order_body(world, "a", test_id, consent_id=NOWHERE),
        )

    def test_another_organisations_test_reads_as_nothing(self, client, world):
        b_test = _make_test(client, world, "b")
        cookie = _cookies(_orderer_cookie(client, world, "a"))
        self._same_as_invented(
            client,
            cookie,
            _order_body(world, "a", b_test),
            _order_body(world, "a", NOWHERE),
        )

    def test_another_organisations_order_cannot_be_placed_or_cancelled(self, client, world, conn):
        b_test = _make_test(client, world, "b")
        b_cookie = _cookies(_orderer_cookie(client, world, "b"))
        b_order = client.post("/clinic/orders", json=_order_body(world, "b", b_test), cookies=b_cookie).json()[
            "order_id"
        ]

        a_cookie = _cookies(_orderer_cookie(client, world, "a"))
        for verb in ("placement", "cancellation"):
            real = client.post(f"/clinic/orders/{b_order}/{verb}", cookies=a_cookie)
            invented = client.post(f"/clinic/orders/{NOWHERE}/{verb}", cookies=a_cookie)
            assert real.status_code == invented.status_code == 404
            assert real.content == invented.content
        assert _order_row(conn, b_order)[5] == "draft"


class TestAWithdrawnConsentCannotBeOrderedOn:
    """
    A consent that has been withdrawn cannot back a new order, and the refusal is the SAME generic 404 as for a
    consent that does not exist: the response must not tell the caller that the withdrawn consent exists.

    WHAT THIS PINS, AND WHAT IT DOES NOT. It pins creation only (`DataAccess.create_order` selects the consent with
    `withdrawn_at IS NULL`). What withdrawal does to orders and reports that already exist is NOT decided in this tree
    (see `DataAccess.withdraw_consent`) and nothing here asserts either way.
    """

    def _withdraw(self, client, world, cookie, tag="a"):
        response = client.post(f"/clinic/consents/{world[tag]['consent']}/withdrawal", cookies=cookie)
        assert response.status_code == 204, response.text

    def test_a_withdrawn_consent_reads_exactly_like_one_that_does_not_exist(self, client, world, conn):
        test_id = _make_test(client, world, "a")
        cookie = _cookies(_orderer_cookie(client, world, "a"))
        self._withdraw(client, world, cookie)

        withdrawn = client.post("/clinic/orders", json=_order_body(world, "a", test_id), cookies=cookie)
        invented = client.post(
            "/clinic/orders", json=_order_body(world, "a", test_id, consent_id=NOWHERE), cookies=cookie
        )

        assert withdrawn.status_code == invented.status_code == 404
        assert withdrawn.content == invented.content  # byte-identical: nothing says the consent exists
        assert str(world["a"]["consent"]) not in withdrawn.text
        assert "withdraw" not in withdrawn.text.lower()

        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM orders WHERE consent_id = %s", (world["a"]["consent"],))
            assert cur.fetchone()[0] == 0  # refused: no draft order was written
        conn.rollback()

    def test_withdrawing_one_consent_leaves_another_organisations_consent_usable(self, client, world):
        a_cookie = _cookies(_orderer_cookie(client, world, "a"))
        self._withdraw(client, world, a_cookie)

        b_test = _make_test(client, world, "b")
        b_cookie = _cookies(_orderer_cookie(client, world, "b"))
        response = client.post("/clinic/orders", json=_order_body(world, "b", b_test), cookies=b_cookie)
        assert response.status_code == 201, response.text
