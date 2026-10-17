"""
clinical/tests/test_w169_portal_visibility.py
─────────────────────────────────────────────
A patient sees a report in the portal only when it was released TO THE PORTAL.

Before this wave the portal's "released" predicate was "a release_events row
exists, for any consumer". `_release_report` accepts any consumer (the schema
reserves the column for a LIMS or a referring clinician), so a report delivered
only to an external party would have appeared in the patient's portal as a side
effect. The predicate is now: the report is `released` AND it has a live
`release_events` row whose consumer is `PORTAL_RELEASE_CONSUMER`.

This file proves the narrowing from the other side than the existing suites do:
those release to the portal and expect the patient to see the report; these
release to somebody else and expect the patient to see nothing. It reuses w136's
fixtures (two organisations, patients with portal logins, the pipeline and
amendment drivers) rather than copying them.

What is deliberately NOT narrowed, and is not tested here because it is
unchanged: the retention clock still starts at the first release of ANY kind,
and a retraction still creates one notification debt per distinct consumer.
"""

from __future__ import annotations

import datetime as dt
import pathlib

from clinical.data_access import PORTAL_RELEASE_CONSUMER, DataAccess
from clinical.tests import test_w136_trends_retraction as _w136
from clinical.tests.test_w136_trends_retraction import (
    PATIENT_COOKIE,
    _amend,
    _pipeline,
    _portal_login,
    _series,
)

# Fixtures shared from test_w136_trends_retraction. They are BOUND AS NAMES rather than imported:
# pytest discovers a fixture by the attribute it finds in this module, so the
# assignment registers it exactly as an import would, while ruff (F811) does not
# mistake each test's use of the fixture as a parameter for a redefinition of an
# unused import. Plain constants and helpers are imported normally above.
client = _w136.client
clinical_dsn = _w136.clinical_dsn
conn = _w136.conn
dao = _w136.dao
reset_the_login_throttle = _w136.reset_the_login_throttle
vcf = _w136.vcf
world = _w136.world

CLINICAL = pathlib.Path(__file__).resolve().parent.parent


def _release(dao, conn, org, report_id, consumer):
    dao._release_report(org["staff"], report_id, consumer=consumer, actor_id=org["staff"].user_id)
    conn.commit()


def _my_reports(client, cookie):
    response = client.get("/portal/me/reports", cookies={PATIENT_COOKIE: cookie})
    assert response.status_code == 200, response.text
    return response.json()


def _my_report(client, cookie, report_id):
    return client.get(f"/portal/me/reports/{report_id}", cookies={PATIENT_COOKIE: cookie})


def _report_stage(client, cookie):
    response = client.get("/portal/me/tests", cookies={PATIENT_COOKIE: cookie})
    assert response.status_code == 200, response.text
    stages = [{s["stage"]: s["state"] for s in order["stages"]} for order in response.json()]
    assert len(stages) == 1, stages
    return stages[0]["report"]


class TestAReportReleasedOnlyToSomebodyElseIsNotThePatients:
    def test_it_is_absent_from_the_list_the_single_read_the_series_and_the_status(self, client, dao, conn, world, vcf):
        org = world["a"]
        run = _pipeline(dao, org, org["first_patient"], vcf, "approved")
        report = str(run["report_id"])
        _release(dao, conn, org, run["report_id"], "LIMS")
        cookie = _portal_login(client, org["org_id"], org["first_email"])

        assert _my_reports(client, cookie) == []
        read = _my_report(client, cookie, report)
        invented = _my_report(client, cookie, "00000000-0000-0000-0000-000000000000")
        assert read.status_code != 200
        assert (read.status_code, read.content) == (invented.status_code, invented.content)
        assert _series(client, cookie) == []
        assert _report_stage(client, cookie) != "completed"

    def test_the_positive_twin_releasing_the_same_report_to_the_portal_shows_it_everywhere(
        self, client, dao, conn, world, vcf
    ):
        org = world["a"]
        run = _pipeline(dao, org, org["first_patient"], vcf, "approved")
        report = str(run["report_id"])
        _release(dao, conn, org, run["report_id"], "LIMS")
        cookie = _portal_login(client, org["org_id"], org["first_email"])
        assert _my_reports(client, cookie) == []

        _release(dao, conn, org, run["report_id"], PORTAL_RELEASE_CONSUMER)

        assert [r["report_id"] for r in _my_reports(client, cookie)] == [report]
        assert _my_report(client, cookie, report).status_code == 200
        assert [p["report_id"] for p in _series(client, cookie)] == [report]
        assert _report_stage(client, cookie) == "completed"

    def test_the_date_the_patient_is_told_is_the_portal_release_not_the_earlier_external_one(
        self, client, dao, conn, world, vcf
    ):
        org = world["a"]
        run = _pipeline(dao, org, org["first_patient"], vcf, "approved")
        _release(dao, conn, org, run["report_id"], "LIMS")
        _release(dao, conn, org, run["report_id"], PORTAL_RELEASE_CONSUMER)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT consumer, released_at FROM release_events WHERE report_id = %s ORDER BY released_at",
                (run["report_id"],),
            )
            rows = cur.fetchall()
        assert [r[0] for r in rows] == ["LIMS", PORTAL_RELEASE_CONSUMER]
        cookie = _portal_login(client, org["org_id"], org["first_email"])
        told = dt.datetime.fromisoformat(_my_reports(client, cookie)[0]["released_at"])
        assert told == rows[1][1], (told, rows)
        assert told != rows[0][1]

    def test_a_state_of_released_with_only_an_external_delivery_is_still_not_shown(self, client, dao, conn, world, vcf):
        """The existing release predicate is two conditions; this is the third
        row of the same shape -- state alone, and a delivery to the wrong party,
        are both insufficient."""
        org = world["a"]
        run = _pipeline(dao, org, org["first_patient"], vcf, "released")  # released to the portal by the driver
        with conn.cursor() as cur:
            cur.execute("UPDATE release_events SET consumer = 'LIMS' WHERE report_id = %s", (run["report_id"],))
        conn.commit()
        cookie = _portal_login(client, org["org_id"], org["first_email"])
        assert _my_reports(client, cookie) == []


class TestTheAmendmentEdgeNeverNamesAnOriginalThePatientWasNotSent:
    def test_an_original_released_only_externally_is_not_named_but_a_portal_release_draws_the_edge(
        self, client, dao, conn, world, vcf
    ):
        org = world["a"]
        run = _pipeline(dao, org, org["first_patient"], vcf, "approved")
        amendment = _amend(dao, org, run)  # the amendment is released to the portal
        _release(dao, conn, org, run["report_id"], "LIMS")  # the original goes to an external consumer only
        cookie = _portal_login(client, org["org_id"], org["first_email"])

        series = _series(client, cookie)
        assert [p["report_id"] for p in series] == [str(amendment)], series
        assert series[0]["amends_report_id"] is None, series
        assert str(run["report_id"]) not in str(series), series

        _release(dao, conn, org, run["report_id"], PORTAL_RELEASE_CONSUMER)
        edge = {p["report_id"]: p["amends_report_id"] for p in _series(client, cookie)}
        assert edge[str(amendment)] == str(run["report_id"]), edge


class TestTheConstantAndTheDatabaseAgree:
    def test_the_migration_the_schema_and_the_portal_sql_use_the_same_literal(self):
        literal = f"'{PORTAL_RELEASE_CONSUMER}'"
        migration = (CLINICAL / "migrations" / "0009_portal_release_once.sql").read_text(encoding="utf-8")
        schema = (CLINICAL / "schema.sql").read_text(encoding="utf-8")
        start = schema.index("CREATE UNIQUE INDEX release_events_portal_once")
        index_ddl = schema[start : schema.index(";", start)]
        assert literal in index_ddl and "tombstoned_at IS NULL" in index_ddl
        assert migration.count(f"consumer = {literal}") >= 2  # the pre-check and the index
        assert f"re.consumer = {literal}" in DataAccess._MY_RELEASED_REPORTS_SQL
        assert f"re2.consumer = {literal}" in DataAccess._MY_RELEASED_REPORTS_SQL
