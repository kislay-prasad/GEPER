# ruff: noqa: F811  (fixtures are imported from the Phase 1 test modules and re-used as parameters)
"""
Genome Analysis Phase 2, over HTTP: the clinician Chromosome Explorer map.

CALL SETS HERE ARE SYNTHETIC and are injected through the route's call-set
dependency, because no storage for CNV/SV call sets exists yet (see
clinical/genome_endpoints.py). What is real in these tests: the session, the role
gate, the organisation scoping, the audit row and the layout builder.
"""

from __future__ import annotations

import json
import uuid

import pytest

from clinical.genome_analysis import parse_structural_vcf
from clinical.genome_endpoints import get_call_set_source
from clinical.tests.test_genome_analysis_endpoints import (  # noqa: F401
    ACMG_MARKER,
    GENE_MARKER,
    PATH_MARKER,
    PATIENT_COOKIE,
    STAFF_COOKIE,
    THE_REFUSAL,
    _a,
    _document,
    _drive,
    _get,
    _patient_cookie,
    _staff_login,
    _staff_with_roles,
    clinical_dsn,
    client,
    conn,
    dao,
    reset_the_login_throttle,
    world,
)

HEADER = (
    "##fileformat=VCFv4.2\n##source=SyntheticCaller\n##contig=<ID=1,length={l1}>\n##contig=<ID=2,length={l2}>\n"
    "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
)
L = {"GRCh38": (248956422, 242193529), "GRCh37": (249250621, 243199373)}

CNV_SET = "1\t1000\tcnvA\tN\t<DEL>\t60\tPASS\tSVTYPE=DEL;END=5000;CN=1;CIPOS=-20,20;SECRET_INFO=cnv-marker\n"
SV_SET = (
    "1\t9000\tb1\tG\tG]2:700]\t.\tPASS\tSVTYPE=BND;MATEID=b2\n"
    "2\t700\tb2\tC\tC]1:9000]\t.\tPASS\tSVTYPE=BND;MATEID=b1\n"
    "1\t20000\tinv1\tN\t<INV>\t.\tLowQual\tSVTYPE=INV;END=30000;IMPRECISE\n"
)


def _set(assembly, body, record_class, call_set_id):
    l1, l2 = L[assembly]
    return parse_structural_vcf(
        HEADER.format(l1=l1, l2=l2) + body,
        assembly=assembly,
        record_class=record_class,
        caller="SyntheticCaller",
        source_id=f"file-{call_set_id}",
        call_set_id=call_set_id,
    )


class RecordingSource:
    def __init__(self, call_sets=()):
        self._sets = tuple(call_sets)
        self.calls = []

    def call_sets_for(self, session, patient_id):
        self.calls.append((session.org_id, patient_id))
        return self._sets


@pytest.fixture()
def use_source(client):
    from clinical.app import app

    installed = []

    def install(source):
        app.dependency_overrides[get_call_set_source] = lambda: source
        installed.append(source)
        return source

    yield install
    app.dependency_overrides.pop(get_call_set_source, None)


def _map_url(patient, tail=""):
    return f"/clinic/patients/{patient}/genome/map{tail}"


def _audit_rows(conn):
    conn.rollback()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT action, resource_type, resource_id, outcome, details::text FROM audit_log WHERE action = 'genome_read' ORDER BY log_id"
        )
        return cur.fetchall()


class TestMapWithoutAnyCallSets:
    def test_cnv_and_sv_are_not_assessed_and_small_variants_come_from_phase_1(
        self, client, dao, world, tmp_path, use_source
    ):
        org = _a(world)
        use_source(RecordingSource())
        _drive(dao, org, org["first_patient"], tmp_path, _document(variants=(("chr17", 7674220),)))
        cookie = _staff_login(client, org["org_id"], org["staff_email"])
        response = _get(client, _map_url(org["first_patient"]), cookie, staff=True)
        assert response.status_code == 200, response.text
        body = response.json()
        assert "not a finding that no such variants exist" in body["call_sets_note"]
        (entry,) = body["maps"]
        m = entry["map"]
        by = {c["name"]: c for c in m["chromosomes"]}
        assert m["assembly"] == "GRCh38" and len(by) == 25 and entry["released_to_patient"] is True
        assert by["17"]["layers"]["small_variants"]["status"] == "variants_present"
        assert by["17"]["layers"]["small_variants"]["markers"][0]["pos"] == 7674220
        for c in by.values():
            assert c["layers"]["cnv"]["status"] == "not_assessed" and c["layers"]["sv"]["status"] == "not_assessed"
        assert m["karyotype"]["status"] == "not_assessed"

    def test_the_default_source_is_empty_by_construction(self, client, dao, world, tmp_path):
        org = _a(world)
        _drive(dao, org, org["first_patient"], tmp_path, _document())
        cookie = _staff_login(client, org["org_id"], org["staff_email"])
        body = _get(client, _map_url(org["first_patient"]), cookie, staff=True).json()
        assert all(c["layers"]["cnv"]["status"] == "not_assessed" for c in body["maps"][0]["map"]["chromosomes"])

    def test_an_unusable_analysis_yields_no_map_and_says_why(self, client, dao, world, tmp_path, use_source):
        org = _a(world)
        use_source(RecordingSource())
        _drive(dao, org, org["first_patient"], tmp_path, _document(assembly="T2T-CHM13"))
        cookie = _staff_login(client, org["org_id"], org["staff_email"])
        (entry,) = _get(client, _map_url(org["first_patient"]), cookie, staff=True).json()["maps"]
        assert entry["map"] is None and entry["status"] == "assembly_unavailable" and entry["reason"]


class TestMapWithSyntheticCallSets:
    def test_cnv_and_sv_layers_are_drawn_on_the_matching_build_only(self, client, dao, world, tmp_path, use_source):
        org = _a(world)
        use_source(
            RecordingSource(
                [
                    _set("GRCh38", CNV_SET, "cnv", "cnv38"),
                    _set("GRCh38", SV_SET, "sv", "sv38"),
                    _set("GRCh37", CNV_SET, "cnv", "cnv37"),
                ]
            )
        )
        _drive(dao, org, org["first_patient"], tmp_path, _document())
        cookie = _staff_login(client, org["org_id"], org["staff_email"])
        body = _get(client, _map_url(org["first_patient"]), cookie, staff=True).json()
        by_build = {e["map"]["assembly"]: e for e in body["maps"]}
        assert set(by_build) == {"GRCh38", "GRCh37"} and len(body["maps"]) == 2
        m38, m37 = by_build["GRCh38"]["map"], by_build["GRCh37"]["map"]
        c1 = next(c for c in m38["chromosomes"] if c["name"] == "1")["layers"]
        assert c1["cnv"]["status"] == "findings_present" and c1["cnv"]["segments"][0]["id"] == "cnv:cnv38:6"
        assert c1["cnv"]["segments"][0]["start_breakpoint"]["precision"] == "interval"
        assert [i["sv_type"] for i in c1["sv"]["intervals"]] == ["INV"] and c1["sv"]["intervals"][0]["start"][
            "precision"
        ] == "imprecise_unquantified"
        assert len(m38["links"]) == 2 and m38["breakend_pairing"]["paired_pairs"] == 1
        assert [cs["call_set_id"] for cs in m38["call_sets"]] == ["cnv38", "sv38"]
        assert [x["call_set_id"] for x in m38["excluded_call_sets"]] == ["cnv37"]
        # the GRCh37 call set got a map of ITS OWN build, with no small-variant analysis behind it
        assert by_build["GRCh37"]["interpretation_id"] is None and by_build["GRCh37"]["reason"]
        assert (
            next(c for c in m37["chromosomes"] if c["name"] == "1")["layers"]["small_variants"]["status"]
            == "not_assessed"
        )
        assert [cs["call_set_id"] for cs in m37["call_sets"]] == ["cnv37"]

    def test_chromosome_route_normalises_the_name_and_restricts_to_a_region(
        self, client, dao, world, tmp_path, use_source
    ):
        org = _a(world)
        use_source(RecordingSource([_set("GRCh38", CNV_SET, "cnv", "c"), _set("GRCh38", SV_SET, "sv", "s")]))
        _drive(dao, org, org["first_patient"], tmp_path, _document(variants=(("chr1", 1200), ("chr1", 90_000))))
        cookie = _staff_login(client, org["org_id"], org["staff_email"])
        for spelling in ("1", "chr1", "CHR1"):
            body = _get(client, _map_url(org["first_patient"], f"/{spelling}"), cookie, staff=True).json()
            assert body["chromosome"] == "1" and [c["name"] for c in body["maps"][0]["map"]["chromosomes"]] == ["1"]
        body = _get(client, _map_url(org["first_patient"], "/1?start=900&end=2000"), cookie, staff=True).json()
        layers = body["maps"][0]["map"]["chromosomes"][0]["layers"]
        assert (
            len(layers["cnv"]["segments"]) == 1 and layers["sv"]["breakends"] == [] and layers["sv"]["intervals"] == []
        )
        assert [mk["pos"] for mk in layers["small_variants"]["markers"]] == [1200]
        far = _get(client, _map_url(org["first_patient"], "/1?start=40000&end=45000"), cookie, staff=True).json()
        far_layers = far["maps"][0]["map"]["chromosomes"][0]["layers"]
        assert far_layers["cnv"]["status"] == "no_calls_reported" and far_layers["cnv"]["segments"] == []

    @pytest.mark.parametrize("query", ["?start=abc&end=5", "?start=10", "?start=0&end=5", "?start=9&end=3", "?end=7"])
    def test_a_bad_region_is_a_400_after_the_gate(self, client, dao, world, tmp_path, use_source, query):
        org = _a(world)
        use_source(RecordingSource())
        _drive(dao, org, org["first_patient"], tmp_path, _document())
        cookie = _staff_login(client, org["org_id"], org["staff_email"])
        response = _get(client, _map_url(org["first_patient"], f"/1{query}"), cookie, staff=True)
        assert response.status_code == 400 and response.json() == {"detail": "Invalid region."}

    @pytest.mark.parametrize("name", ["23", "chr23", "GL000220.1", "0", "x y"])
    def test_a_name_that_is_not_a_chromosome_is_a_404(self, client, dao, world, tmp_path, use_source, name):
        org = _a(world)
        use_source(RecordingSource())
        cookie = _staff_login(client, org["org_id"], org["staff_email"])
        response = _get(client, _map_url(org["first_patient"], f"/{name}"), cookie, staff=True)
        assert response.status_code == 404 and response.json() == {"detail": "Not found."}


class TestAccessControl:
    def test_the_role_gate_precedes_everything_including_region_and_name_checks(
        self, client, dao, conn, world, tmp_path, use_source
    ):
        org = _a(world)
        source = use_source(RecordingSource([_set("GRCh38", CNV_SET, "cnv", "c")]))
        _drive(dao, org, org["first_patient"], tmp_path, _document())
        for tag, roles in (("lab", ["Lab technician"]), ("adm", ["Administrator"]), ("aud", ["Auditor"])):
            _staff_with_roles(dao, conn, org["org_id"], f"{tag}@a.example", roles)
            cookie = _staff_login(client, org["org_id"], f"{tag}@a.example")
            attempts = [
                _map_url(org["first_patient"]),
                _map_url(uuid.uuid4()),
                _map_url("not-a-uuid"),
                _map_url(world["b"]["first_patient"]),
                _map_url(org["first_patient"], "/chr23"),
                _map_url(org["first_patient"], "/1?start=abc"),
            ]
            responses = [_get(client, u, cookie, staff=True) for u in attempts]
            assert {r.status_code for r in responses} == {403}, tag
            assert len({r.content for r in responses}) == 1, tag
        assert source.calls == []  # a refused caller never reaches the call-set source

    @pytest.mark.parametrize("role", ["Orderer", "Interpreter", "Approver"])
    def test_each_clinical_role_may_read(self, client, dao, conn, world, tmp_path, use_source, role):
        org = _a(world)
        use_source(RecordingSource())
        _drive(dao, org, org["first_patient"], tmp_path, _document())
        _staff_with_roles(dao, conn, org["org_id"], "only@a.example", [role])
        cookie = _staff_login(client, org["org_id"], "only@a.example")
        assert _get(client, _map_url(org["first_patient"]), cookie, staff=True).status_code == 200

    def test_cross_organisation_is_indistinguishable_from_nonexistent_and_never_reaches_the_source(
        self, client, dao, world, tmp_path, use_source
    ):
        a, b = world["a"], world["b"]
        source = use_source(RecordingSource([_set("GRCh38", CNV_SET, "cnv", "c")]))
        _drive(dao, a, a["first_patient"], tmp_path, _document())
        cookie_b = _staff_login(client, b["org_id"], b["staff_email"])
        foreign = _get(client, _map_url(a["first_patient"]), cookie_b, staff=True)
        invented = _get(client, _map_url(uuid.uuid4()), cookie_b, staff=True)
        malformed = _get(client, _map_url("nope"), cookie_b, staff=True)
        assert {r.status_code for r in (foreign, invented, malformed)} == {404}
        assert len({r.content for r in (foreign, invented, malformed)}) == 1
        for tail in ("/1", "/1?start=1&end=9"):
            assert _get(client, _map_url(a["first_patient"], tail), cookie_b, staff=True).status_code == 404
        assert source.calls == []

    def test_the_source_is_asked_with_the_sessions_organisation_and_the_target_patient(
        self, client, dao, world, tmp_path, use_source
    ):
        a = _a(world)
        source = use_source(RecordingSource())
        _drive(dao, a, a["first_patient"], tmp_path, _document())
        cookie = _staff_login(client, a["org_id"], a["staff_email"])
        _get(client, _map_url(a["first_patient"]), cookie, staff=True)
        assert source.calls == [(a["org_id"], a["first_patient"])]

    def test_a_patient_session_is_not_a_staff_session(self, client, world):
        org = _a(world)
        patient_cookie = _patient_cookie(client, org)
        for cookie_name in (PATIENT_COOKIE, STAFF_COOKIE):
            response = client.get(_map_url(org["first_patient"]), cookies={cookie_name: patient_cookie})
            assert (response.status_code, response.content) == THE_REFUSAL

    def test_unauthenticated_is_the_one_refusal(self, client, world):
        for tail in ("", "/1", "/1?start=1&end=2"):
            response = _get(client, _map_url(_a(world)["first_patient"], tail))
            assert (response.status_code, response.content) == THE_REFUSAL, tail

    def test_the_routes_are_read_only(self, client, world):
        org = _a(world)
        cookie = _staff_login(client, org["org_id"], org["staff_email"])
        for method in ("post", "put", "patch", "delete"):
            for tail in ("", "/1"):
                response = getattr(client, method)(_map_url(org["first_patient"], tail), cookies={STAFF_COOKIE: cookie})
                assert response.status_code == 405


class TestPatientsGetNothingNew:
    def test_the_patient_router_has_no_map_or_finding_route_and_stays_unmounted_by_default(
        self, client, dao, world, tmp_path
    ):
        from clinical.genome_endpoints import portal_genome_router

        paths = sorted(r.path for r in portal_genome_router.routes)
        assert paths == ["/portal/me/genome/chromosomes", "/portal/me/genome/chromosomes/{name}"]
        org = _a(world)
        _drive(dao, org, org["first_patient"], tmp_path, _document())
        cookie = _patient_cookie(client, org)
        for path in (
            "/portal/me/genome/map",
            "/portal/me/genome/map/1",
            "/portal/me/genome/cnv",
            "/portal/me/genome/sv",
            "/portal/me/genome/chromosomes",
            "/portal/me/genome/chromosomes/1",
        ):
            assert client.get(path, cookies={PATIENT_COOKIE: cookie}).status_code == 404, path

    def test_enabled_patient_routes_still_carry_no_cnv_or_sv(self, client, dao, world, tmp_path, use_source):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from clinical.genome_endpoints import portal_genome_router

        org = _a(world)
        use_source(RecordingSource([_set("GRCh38", CNV_SET, "cnv", "c"), _set("GRCh38", SV_SET, "sv", "s")]))
        _drive(dao, org, org["first_patient"], tmp_path, _document())
        app = FastAPI()
        app.include_router(portal_genome_router)
        text = "".join(
            TestClient(app).get(p, cookies={PATIENT_COOKIE: _patient_cookie(client, org)}).text
            for p in ("/portal/me/genome/chromosomes", "/portal/me/genome/chromosomes/1")
        )
        for forbidden in (
            "cnv",
            "segments",
            "breakends",
            "links",
            "SyntheticCaller",
            "cnv-marker",
            "copy_number",
            "deletion",
            "inversion",
        ):
            assert forbidden not in text.lower(), forbidden


class TestAudit:
    def test_each_map_read_is_one_genome_read_row_naming_only_the_patient(
        self, client, dao, conn, world, tmp_path, use_source
    ):
        org = _a(world)
        use_source(
            RecordingSource([_set("GRCh38", CNV_SET, "cnv", "cnv-set-id"), _set("GRCh38", SV_SET, "sv", "sv-set-id")])
        )
        _drive(dao, org, org["first_patient"], tmp_path, _document(variants=(("chr17", 7674220),)))
        cookie = _staff_login(client, org["org_id"], org["staff_email"])
        assert _audit_rows(conn) == []
        assert _get(client, _map_url(org["first_patient"]), cookie, staff=True).status_code == 200
        assert (
            _get(client, _map_url(org["first_patient"], "/1?start=900&end=2000"), cookie, staff=True).status_code == 200
        )
        rows = _audit_rows(conn)
        assert len(rows) == 2
        for action, resource_type, resource_id, outcome, details in rows:
            assert (action, resource_type, resource_id, outcome) == (
                "genome_read",
                "patient",
                str(org["first_patient"]),
                "success",
            )
            for forbidden in (
                GENE_MARKER,
                ACMG_MARKER,
                PATH_MARKER,
                "7674220",
                "SyntheticCaller",
                "cnv-set-id",
                "sv-set-id",
                "cnv-marker",
                "GRCh38",
                "start",
                "900",
                "run_document",
                "deletion",
                "SVTYPE",
            ):
                assert forbidden not in details, forbidden

    def test_a_refused_read_is_one_denied_row_with_no_genomic_content(
        self, client, dao, conn, world, tmp_path, use_source
    ):
        org = _a(world)
        use_source(RecordingSource([_set("GRCh38", CNV_SET, "cnv", "c")]))
        _drive(dao, org, org["first_patient"], tmp_path, _document())
        _staff_with_roles(dao, conn, org["org_id"], "lab@a.example", ["Lab technician"])
        cookie = _staff_login(client, org["org_id"], "lab@a.example")
        assert _get(client, _map_url(org["first_patient"], "/1?start=1&end=2"), cookie, staff=True).status_code == 403
        (row,) = _audit_rows(conn)
        assert row[0] == "genome_read" and row[3] == "denied"
        assert "cnv" not in row[4].lower() and "GRCh38" not in row[4]

    def test_the_audit_vocabulary_did_not_grow(self, client, dao, conn, world, tmp_path, use_source):
        org = _a(world)
        use_source(RecordingSource())
        _drive(dao, org, org["first_patient"], tmp_path, _document())
        cookie = _staff_login(client, org["org_id"], org["staff_email"])
        _get(client, _map_url(org["first_patient"]), cookie, staff=True)
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT action FROM audit_log WHERE action ILIKE '%genome%' OR action ILIKE '%cnv%' OR action ILIKE '%structural%'"
            )
            assert {r[0] for r in cur.fetchall()} == {"genome_read"}


class TestEndToEndFromVcfText:
    def test_vcf_text_to_validated_records_to_the_authenticated_map(self, client, dao, world, tmp_path, use_source):
        """Synthetic external CNV/SV VCF text -> ingestion -> validation -> the real route. The call sets
        are handed to the route through its dependency because nothing stores them yet."""
        org = _a(world)
        cnv = parse_structural_vcf(
            HEADER.format(l1=L["GRCh38"][0], l2=L["GRCh38"][1])
            + CNV_SET
            + "1\t7000\tbad\tN\t<DEL>\t.\tPASS\tSVTYPE=DEL;END=10\n"
            + "1\t8000\tinv\tN\t<INV>\t.\tPASS\tEND=9000\n",
            assembly="GRCh38",
            record_class="cnv",
            caller="SyntheticCaller",
            call_set_id="e2e",
        )
        assert cnv.counts()["invalid"] == 1 and cnv.counts()["unsupported"] == 1 and len(cnv.cnvs) == 1
        use_source(RecordingSource([cnv]))
        _drive(dao, org, org["first_patient"], tmp_path, _document())
        cookie = _staff_login(client, org["org_id"], org["staff_email"])
        body = _get(client, _map_url(org["first_patient"], "/1"), cookie, staff=True).json()
        m = body["maps"][0]["map"]
        assert [s["start"] for s in m["chromosomes"][0]["layers"]["cnv"]["segments"]] == [1000]
        meta = m["call_sets"][0]
        assert meta["counts"] == {"detected": 1, "uncertain": 0, "unsupported": 1, "invalid": 1, "records": 3}
        assert {r["status"] for r in meta["rejected"]} == {"invalid", "unsupported"}
        json.dumps(body)  # serialisable end to end


class ExcludingSource(RecordingSource):
    """A source that, like the real one, also says which VALIDATED call sets it is holding back, and why."""

    def __init__(self, call_sets=(), excluded=None):
        super().__init__(call_sets)
        self._excluded = dict(excluded or {})

    def excluded_for(self, session, patient_id):
        return dict(self._excluded)


class TestNeverAttachedIsNotTheSameAsHeldBack:
    def test_nothing_attached_nothing_held_back(self, client, dao, world, tmp_path, use_source):
        org = _a(world)
        use_source(ExcludingSource())
        _drive(dao, org, org["first_patient"], tmp_path, _document())
        cookie = _staff_login(client, org["org_id"], org["staff_email"])
        body = _get(client, _map_url(org["first_patient"]), cookie, staff=True).json()
        assert body["held_back_call_sets"] == {} and "is attached to this patient" in body["call_sets_note"]
        assert "NOT shown" not in body["call_sets_note"]

    def test_evidence_held_back_is_reported_with_its_reason_and_not_called_absent(
        self, client, dao, world, tmp_path, use_source
    ):
        org = _a(world)
        use_source(ExcludingSource(excluded={"consent_withdrawn": 1, "integrity_failure": 2}))
        _drive(dao, org, org["first_patient"], tmp_path, _document())
        cookie = _staff_login(client, org["org_id"], org["staff_email"])
        body = _get(client, _map_url(org["first_patient"]), cookie, staff=True).json()
        assert body["held_back_call_sets"] == {"consent_withdrawn": 1, "integrity_failure": 2}
        note = body["call_sets_note"]
        assert "3 validated" in note and "NOT shown" in note and "consent is withdrawn" in note
        assert "integrity check" in note and "is attached to this patient" not in note
        assert "not a finding that no such variants exist" in note
        # nothing identifying leaks: counts and fixed words only
        assert str(org["first_patient"]) not in note

    def test_some_drawn_and_some_held_back_says_both(self, client, dao, world, tmp_path, use_source):
        org = _a(world)
        use_source(ExcludingSource([_set("GRCh38", CNV_SET, "cnv", "c1")], {"order_cancelled": 1}))
        _drive(dao, org, org["first_patient"], tmp_path, _document())
        cookie = _staff_login(client, org["org_id"], org["staff_email"])
        body = _get(client, _map_url(org["first_patient"], "/1"), cookie, staff=True).json()
        assert "none is converted" in body["call_sets_note"] and "the order is cancelled" in body["call_sets_note"]
        assert body["held_back_call_sets"] == {"order_cancelled": 1}
