# ruff: noqa: F811  (fixtures are imported from test_w135_patient_report_access and re-used as parameters)
"""
clinical/tests/test_genome_analysis_endpoints.py
────────────────────────────────────────────────
Genome Analysis, Phase 1, over HTTP against the app the service serves.

THE QUESTIONS, in the order the suite asks them:

  1. Does a patient see ONLY their own, RELEASED, NOT-RETRACTED analyses?
     (cross-patient AND cross-organisation, with a real same-organisation
     patient as the control and the same patient e-mail in both organisations)
  2. Can anything in the request change whose data comes back?
  3. Does a clinician see only their own organisation, behind a role gate that
     answers before any existence check?
  4. Does the response say "not assessed" where it must, never name a
     karyotype or a sex, and keep the genome build with every coordinate?
  5. Does it leak anything from the run document beyond coordinates and alleles?

Fixtures and helpers come from test_w135_patient_report_access.py, which owns
the "drive one order to a released report through the real writers" machinery;
this file only varies the run document.
"""

from __future__ import annotations

import ast
import hashlib
import json
import pathlib
import uuid

import pytest

from clinical.data_access import PORTAL_RELEASE_CONSUMER, AuthorizationError
from clinical.tests.test_w135_patient_report_access import (  # noqa: F401  (fixtures are used by name)
    PASSWORD,
    PATIENT_COOKIE,
    STAFF_COOKIE,
    T0,
    THE_REFUSAL,
    _portal_login,
    _retract,
    _staff_login,
    clinical_dsn,
    client,
    conn,
    dao,
    reset_the_login_throttle,
    world,
)

# Cannot arrive by coincidence: stored in the run document, must never be returned.
GENE_MARKER = "genome-p1-gene-marker-must-not-leak"
ACMG_MARKER = "genome-p1-acmg-marker-must-not-leak"
PATH_MARKER = "/srv/genome-p1-path-marker-must-not-leak/sample.vcf"


def _variant(chrom, pos, ref="A", alt="T"):
    return {
        "variant": {"chrom": chrom, "pos": pos, "ref": ref, "alt": alt, "variant_type": "SNV", "gene": GENE_MARKER},
        "acmg": {"classification": ACMG_MARKER},
        "gene": GENE_MARKER,
    }


def _document(assembly="GRCh38", variants=(("chr17", 7674220), ("chrX", 5000)), complete=True):
    return {
        "assembly": assembly,
        "run_complete": complete,
        "geper_version": "1.0.0",
        "generated_at": "2026-09-01T00:00:00+00:00",
        "input_vcf": PATH_MARKER,
        "variants": [_variant(c, p) for c, p in variants],
    }


_counter = iter(range(1, 10_000))


def _drive(dao, org, patient_id, tmp_path, run_document, upto="released"):
    """`_pipeline` from the w135 file, with the run document as a parameter and a
    distinct VCF per call. Same real writers, same role gates; nothing forged."""
    staff = org["staff"]
    consent_id = dao.record_consent(staff, patient_id, "testing")
    order_id = dao.create_order(
        staff,
        patient_id=patient_id,
        test_id=org["test_id"],
        required_scope="testing",
        consent_id=consent_id,
        priority="routine",
        clinical_indication="genome-p1",
    )
    dao.place_order(staff, order_id)
    sample_id = dao.receive_sample(staff, order_id, "blood")
    dao.record_qc(staff, sample_id, "passed")

    path = tmp_path / f"run{next(_counter)}.vcf"
    path.write_text(f"##fileformat=VCFv4.2\n#CHROM\tPOS\nchr1\t{next(_counter)}\n", encoding="utf-8")
    system = dao._create_system_session(org["org_id"])
    result = dao.record_pipeline_result(
        system, sample_id, str(path), hashlib.sha256(path.read_bytes()).hexdigest(), run_document
    )
    report_id = result.report_id
    if upto == "draft":
        return report_id
    dao.record_sole_signatory_claim(
        staff, interpretation_id=result.interpretation_id, report_id=report_id, reason="genome-p1 fixture"
    )
    dao.submit_for_review(staff, report_id)
    if upto == "under_review":
        return report_id
    dao.approve_report(staff, report_id)
    if upto == "approved":
        return report_id
    dao._release_report(staff, report_id, consumer=PORTAL_RELEASE_CONSUMER, actor_id=staff.user_id)
    return report_id


def _get(client, path, cookie=None, staff=False, **kw):
    cookies = {(STAFF_COOKIE if staff else PATIENT_COOKIE): cookie} if cookie else {}
    return client.get(path, cookies=cookies, **kw)


def _a(world):
    return world["a"]


def _patient_cookie(client, org, which="first"):
    return _portal_login(client, org["org_id"], org[f"{which}_email"])


LIST = "/portal/me/genome/chromosomes"


# ─── 1. the patient routes: OFF by default, and withheld even when on ───────


@pytest.fixture()
def patient_client(clinical_dsn):
    """A client over an app that mounts the patient genome router, as the flag
    would. Built here rather than by reloading clinical.app, whose route table is
    fixed at import."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from clinical.genome_endpoints import portal_genome_router

    app = FastAPI()
    app.include_router(portal_genome_router)
    return TestClient(app, client=("203.0.113.11", 45678))


class TestPatientRoutesAreOffByDefault:
    @pytest.mark.parametrize("path", [LIST, f"{LIST}/17", f"{LIST}/X", f"{LIST}/nonsense"])
    def test_the_served_app_does_not_mount_them(self, client, dao, world, tmp_path, path):
        org = _a(world)
        _drive(dao, org, org["first_patient"], tmp_path, _document())
        cookie = _patient_cookie(client, org)
        for cookies in ({}, {PATIENT_COOKIE: cookie}):
            response = client.get(path, cookies=cookies)
            assert response.status_code == 404, (path, bool(cookies))
        # indistinguishable from a path that never existed
        ghost = client.get("/portal/me/never-existed", cookies={PATIENT_COOKIE: cookie})
        assert (ghost.status_code, ghost.content) == (404, client.get(LIST).content)

    @pytest.mark.parametrize(
        "value,expected",
        [
            (None, False),
            ("", False),
            ("0", False),
            ("false", False),
            ("no", False),
            ("maybe", False),
            (" ", False),
            ("1", True),
            ("true", True),
            ("TRUE", True),
            (" yes ", True),
        ],
    )
    def test_the_flag_is_an_explicit_opt_in(self, value, expected):
        from clinical.genome_endpoints import PATIENT_GENOME_ENV, patient_routes_enabled

        env = {} if value is None else {PATIENT_GENOME_ENV: value}
        assert patient_routes_enabled(env) is expected

    def test_the_real_app_only_includes_the_router_behind_the_flag(self):
        source = (pathlib.Path(__file__).resolve().parent.parent / "app.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        guarded = [
            n
            for n in tree.body
            if isinstance(n, ast.If)
            and "patient_routes_enabled" in ast.unparse(n.test)
            and "portal_genome_router" in ast.unparse(n)
        ]
        assert len(guarded) == 1
        unguarded = [n for n in tree.body if isinstance(n, ast.Expr) and "portal_genome_router" in ast.unparse(n)]
        assert unguarded == []


class TestEvenWhenEnabledThePatientGetsNoGenomicData:
    def test_released_report_yields_reference_metadata_and_a_withheld_statement(
        self, patient_client, client, dao, world, tmp_path
    ):
        org = _a(world)
        report_id = _drive(dao, org, org["first_patient"], tmp_path, _document())
        response = _get(patient_client, LIST, _patient_cookie(client, org))
        assert response.status_code == 200, response.text
        (analysis,) = response.json()["analyses"]
        assert set(analysis) == {
            "report_ids",
            "released_at",
            "assembly",
            "findings",
            "reference_chromosomes",
            "limitations",
        }
        assert analysis["report_ids"] == [str(report_id)] and analysis["assembly"] == "GRCh38"
        assert analysis["findings"]["status"] == "withheld"
        names = [c["name"] for c in analysis["reference_chromosomes"]]
        assert names == [str(n) for n in range(1, 23)] + ["MT"]
        assert analysis["reference_chromosomes"][0]["length_bp"] == 248956422

    def test_nothing_from_the_run_document_reaches_the_patient(self, patient_client, client, dao, world, tmp_path):
        org = _a(world)
        _drive(
            dao,
            org,
            org["first_patient"],
            tmp_path,
            _document(variants=(("chr17", 7674220), ("chrX", 5000), ("chrY", 99), ("chrM", 73))),
        )
        cookie = _patient_cookie(client, org)
        text = "".join(_get(patient_client, p, cookie).text for p in (LIST, f"{LIST}/17", f"{LIST}/MT"))
        for marker in (
            GENE_MARKER,
            ACMG_MARKER,
            PATH_MARKER,
            "interpretation_id",
            "7674220",
            "5000",
            "variant",
            "count",
            "variants_present",
            "no_variants_reported",
            "provenance",
            "generated_at",
            "First A",
            "1990",
        ):
            assert marker not in text, marker

    def test_the_response_does_not_vary_with_the_variants(self, patient_client, client, dao, world, tmp_path):
        org = _a(world)
        _drive(dao, org, org["first_patient"], tmp_path, _document(variants=(("chr1", 10), ("chr2", 20))))
        _drive(dao, org, org["second_patient"], tmp_path, _document(variants=()))
        first = _get(patient_client, LIST, _patient_cookie(client, org, "first")).json()["analyses"][0]
        second = _get(patient_client, LIST, _patient_cookie(client, org, "second")).json()["analyses"][0]

        def strip(analysis):
            return {k: v for k, v in analysis.items() if k not in ("report_ids", "released_at")}

        assert strip(first) == strip(second)

    def test_x_and_y_are_not_available_in_any_spelling(self, patient_client, client, dao, world, tmp_path):
        org = _a(world)
        _drive(dao, org, org["first_patient"], tmp_path, _document(variants=(("chrX", 5), ("chrY", 6))))
        cookie = _patient_cookie(client, org)
        for name in ("X", "Y", "chrX", "chrY", "x", "CHRY"):
            response = _get(patient_client, f"{LIST}/{name}", cookie)
            assert response.status_code == 404 and response.json() == {"detail": "Not found."}, name
        listing = _get(patient_client, LIST, cookie).json()["analyses"][0]["reference_chromosomes"]
        assert all(c["name"] not in ("X", "Y") for c in listing)

    def test_detail_route_returns_one_reference_chromosome(self, patient_client, client, dao, world, tmp_path):
        org = _a(world)
        _drive(dao, org, org["first_patient"], tmp_path, _document(variants=(("chr17", 7674220), ("chrM", 73))))
        cookie = _patient_cookie(client, org)
        bodies = [_get(patient_client, f"{LIST}/{s}", cookie) for s in ("17", "chr17", "CHR17")]
        assert {r.status_code for r in bodies} == {200}
        assert len({json.dumps(r.json(), sort_keys=True) for r in bodies}) == 1
        (entry,) = bodies[0].json()["analyses"][0]["reference_chromosomes"]
        assert entry["name"] == "17" and entry["length_bp"] == 83257441
        assert _get(patient_client, f"{LIST}/chrM", cookie).json()["chromosome"] == "MT"

    @pytest.mark.parametrize("name", ["23", "chr23", "0", "GL000220.1", "chrUn_gl000220", "x y", "%2e%2e", "MTX"])
    def test_a_name_that_is_not_a_chromosome_is_a_404(self, patient_client, client, dao, world, tmp_path, name):
        org = _a(world)
        _drive(dao, org, org["first_patient"], tmp_path, _document())
        response = _get(patient_client, f"{LIST}/{name}", _patient_cookie(client, org))
        assert response.status_code == 404 and response.json() == {"detail": "Not found."}

    @pytest.mark.parametrize("upto", ["draft", "under_review", "approved"])
    def test_an_unreleased_report_reveals_nothing_at_all(self, patient_client, client, dao, world, tmp_path, upto):
        org = _a(world)
        _drive(dao, org, org["first_patient"], tmp_path, _document(), upto=upto)
        cookie = _patient_cookie(client, org)
        assert _get(patient_client, LIST, cookie).json() == {"analyses": []}
        assert _get(patient_client, f"{LIST}/17", cookie).json() == {"chromosome": "17", "analyses": []}

    def test_a_retracted_report_is_not_shown(self, patient_client, client, dao, conn, world, tmp_path):
        org = _a(world)
        report_id = _drive(dao, org, org["first_patient"], tmp_path, _document())
        cookie = _patient_cookie(client, org)
        assert len(_get(patient_client, LIST, cookie).json()["analyses"]) == 1
        _retract(conn, org["org_id"], report_id, "genome-p1 retraction")
        assert _get(patient_client, LIST, cookie).json() == {"analyses": []}

    def test_builds_are_never_merged_and_an_unusable_build_gives_no_reference(
        self, patient_client, client, dao, world, tmp_path
    ):
        org = _a(world)
        _drive(dao, org, org["first_patient"], tmp_path, _document(assembly="GRCh37"))
        _drive(dao, org, org["first_patient"], tmp_path, _document(assembly="hg38"))
        _drive(dao, org, org["first_patient"], tmp_path, _document(assembly="T2T-CHM13"))
        analyses = _get(patient_client, LIST, _patient_cookie(client, org)).json()["analyses"]
        by_build = {a["assembly"]: a for a in analyses}
        assert set(by_build) == {"GRCh37", "GRCh38", None} and len(analyses) == 3
        assert by_build["GRCh37"]["reference_chromosomes"][0]["length_bp"] == 249250621
        assert by_build["GRCh38"]["reference_chromosomes"][0]["length_bp"] == 248956422
        assert by_build[None]["reference_chromosomes"] == []


# ─── 2. isolation: cross-patient and cross-organisation ──────────────────────


class TestIsolation:
    def test_a_different_real_same_org_patient_is_not_reachable(self, patient_client, client, dao, world, tmp_path):
        org = _a(world)
        r1 = _drive(dao, org, org["first_patient"], tmp_path, _document())
        r2 = _drive(dao, org, org["second_patient"], tmp_path, _document())
        first = _get(patient_client, LIST, _patient_cookie(client, org, "first")).json()["analyses"]
        second = _get(patient_client, LIST, _patient_cookie(client, org, "second")).json()["analyses"]
        assert [a["report_ids"] for a in first] == [[str(r1)]]
        assert [a["report_ids"] for a in second] == [[str(r2)]]

    def test_nothing_in_the_request_can_name_another_patient(self, patient_client, client, dao, world, tmp_path):
        org = _a(world)
        _drive(dao, org, org["first_patient"], tmp_path, _document())
        _drive(dao, org, org["second_patient"], tmp_path, _document())
        cookie = _patient_cookie(client, org, "first")
        other = str(org["second_patient"])
        baseline = _get(patient_client, LIST, cookie).json()
        for variation in (
            dict(params={"patient_id": other}),
            dict(params={"patient": other, "org_id": str(world["b"]["org_id"])}),
            dict(headers={"X-Patient-Id": other}),
            dict(headers={"X-Org-Id": str(world["b"]["org_id"])}),
        ):
            response = _get(patient_client, LIST, cookie, **variation)
            assert response.status_code == 200 and response.json() == baseline, variation
        assert _get(patient_client, f"/portal/{other}/genome/chromosomes", cookie).status_code in (404, 405)
        assert _get(patient_client, f"{LIST}/17/{other}", cookie).status_code == 404

    def test_the_same_patient_email_in_another_organisation_sees_only_its_own(
        self, patient_client, client, dao, world, tmp_path
    ):
        a, b = world["a"], world["b"]
        assert a["first_email"] == b["first_email"]
        ra = _drive(dao, a, a["first_patient"], tmp_path, _document())
        rb = _drive(dao, b, b["first_patient"], tmp_path, _document())
        in_a = _get(patient_client, LIST, _patient_cookie(client, a)).json()["analyses"]
        in_b = _get(patient_client, LIST, _patient_cookie(client, b)).json()["analyses"]
        assert [x["report_ids"] for x in in_a] == [[str(ra)]]
        assert [x["report_ids"] for x in in_b] == [[str(rb)]]

    def test_no_session_is_the_one_refusal(self, patient_client, world):
        for path in (LIST, f"{LIST}/17", f"{LIST}/not-a-chromosome"):
            response = _get(patient_client, path)
            assert (response.status_code, response.content) == THE_REFUSAL, path

    def test_a_staff_session_is_not_a_patient_session(self, patient_client, client, world):
        org = _a(world)
        staff_cookie = _staff_login(client, org["org_id"], org["staff_email"])
        for cookie_name in (PATIENT_COOKIE, STAFF_COOKIE):
            response = patient_client.get(LIST, cookies={cookie_name: staff_cookie})
            assert (response.status_code, response.content) == THE_REFUSAL

    def test_the_data_layer_refuses_a_staff_session_directly(self, dao, world):
        with pytest.raises(AuthorizationError):
            dao.list_my_genome_analyses(_a(world)["staff"])


# ─── 4. clinician routes ─────────────────────────────────────────────────────


def _staff_with_roles(dao, conn, org_id, email, roles):
    user_id = dao.create_user(org_id, email, PASSWORD)
    with conn.cursor() as cur:
        for role in roles:
            cur.execute(
                "INSERT INTO role_assignments (user_id, org_id, role, assigned_by, assigned_at, basis) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (user_id, org_id, role, user_id, T0, "genome-p1 test fixture" if role == "Approver" else None),
            )
    conn.commit()


def _clinic(patient_id, tail="chromosomes"):
    return f"/clinic/patients/{patient_id}/genome/{tail}"


class TestClinicianRoutes:
    def test_clinician_sees_unreleased_work_marked_as_such(self, client, dao, world, tmp_path):
        org = _a(world)
        _drive(dao, org, org["first_patient"], tmp_path, _document(variants=(("chr17", 1),)), upto="draft")
        released_id = _drive(dao, org, org["first_patient"], tmp_path, _document(variants=(("chr5", 2),)))
        cookie = _staff_login(client, org["org_id"], org["staff_email"])
        response = _get(client, _clinic(org["first_patient"]), cookie, staff=True)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["patient_id"] == str(org["first_patient"])
        flags = sorted(a["released_to_patient"] for a in body["analyses"])
        assert flags == [False, True] and len(body["analyses"]) == 2
        draft = next(a for a in body["analyses"] if not a["released_to_patient"])
        assert draft["report_states"] == ["draft"] and draft["assembly"] == "GRCh38"
        assert "interpretation_id" in draft
        released = next(a for a in body["analyses"] if a["released_to_patient"])
        assert released["report_states"] == ["released"]
        assert str(released_id)  # the released one is the one the patient would also see

    def test_clinician_chromosome_detail_lists_variants_and_normalises_the_name(self, client, dao, world, tmp_path):
        org = _a(world)
        _drive(dao, org, org["first_patient"], tmp_path, _document(variants=(("chr17", 7674220),)))
        cookie = _staff_login(client, org["org_id"], org["staff_email"])
        for spelling in ("17", "chr17"):
            body = _get(client, _clinic(org["first_patient"], f"chromosomes/{spelling}"), cookie, staff=True).json()
            assert body["chromosome"] == "17"
            assert body["analyses"][0]["chromosome"]["small_variants"]["variants"][0]["pos"] == 7674220
        bad = _get(client, _clinic(org["first_patient"], "chromosomes/chr23"), cookie, staff=True)
        assert bad.status_code == 404

    def test_a_retracted_report_is_no_longer_released_to_the_patient(self, client, dao, conn, world, tmp_path):
        org = _a(world)
        report_id = _drive(dao, org, org["first_patient"], tmp_path, _document())
        _retract(conn, org["org_id"], report_id, "genome-p1")
        cookie = _staff_login(client, org["org_id"], org["staff_email"])
        (analysis,) = _get(client, _clinic(org["first_patient"]), cookie, staff=True).json()["analyses"]
        assert analysis["released_to_patient"] is False

    def test_the_role_gate_answers_before_any_existence_check(self, client, dao, conn, world, tmp_path):
        org = _a(world)
        _drive(dao, org, org["first_patient"], tmp_path, _document())
        for tag, roles in (("lab", ["Lab technician"]), ("adm", ["Administrator"]), ("aud", ["Auditor"])):
            _staff_with_roles(dao, conn, org["org_id"], f"{tag}@a.example", roles)
            cookie = _staff_login(client, org["org_id"], f"{tag}@a.example")
            real = _get(client, _clinic(org["first_patient"]), cookie, staff=True)
            invented = _get(client, _clinic(uuid.uuid4()), cookie, staff=True)
            malformed = _get(client, _clinic("not-a-uuid"), cookie, staff=True)
            other_org = _get(client, _clinic(world["b"]["first_patient"]), cookie, staff=True)
            detail = _get(client, _clinic(org["first_patient"], "chromosomes/chr23"), cookie, staff=True)
            assert {r.status_code for r in (real, invented, malformed, other_org, detail)} == {403}, tag
            assert len({r.content for r in (real, invented, malformed, other_org, detail)}) == 1, tag

    @pytest.mark.parametrize("role", ["Orderer", "Interpreter", "Approver"])
    def test_each_clinical_role_may_read(self, client, dao, conn, world, tmp_path, role):
        org = _a(world)
        _drive(dao, org, org["first_patient"], tmp_path, _document())
        _staff_with_roles(dao, conn, org["org_id"], "only@a.example", [role])
        cookie = _staff_login(client, org["org_id"], "only@a.example")
        assert _get(client, _clinic(org["first_patient"]), cookie, staff=True).status_code == 200

    def test_cross_organisation_is_indistinguishable_from_nonexistent(self, client, dao, world, tmp_path):
        a, b = world["a"], world["b"]
        _drive(dao, a, a["first_patient"], tmp_path, _document())
        cookie_b = _staff_login(client, b["org_id"], b["staff_email"])
        foreign = _get(client, _clinic(a["first_patient"]), cookie_b, staff=True)
        invented = _get(client, _clinic(uuid.uuid4()), cookie_b, staff=True)
        malformed = _get(client, _clinic("nope"), cookie_b, staff=True)
        assert {r.status_code for r in (foreign, invented, malformed)} == {404}
        assert len({r.content for r in (foreign, invented, malformed)}) == 1
        for tail in ("chromosomes", "chromosomes/17"):
            assert _get(client, _clinic(a["first_patient"], tail), cookie_b, staff=True).status_code == 404

    def test_same_org_staff_cannot_see_across_the_org_boundary_in_either_direction(self, client, dao, world, tmp_path):
        a, b = world["a"], world["b"]
        _drive(dao, b, b["first_patient"], tmp_path, _document(variants=(("chr9", 5),)))
        cookie_a = _staff_login(client, a["org_id"], a["staff_email"])
        assert _get(client, _clinic(b["first_patient"]), cookie_a, staff=True).status_code == 404
        cookie_b = _staff_login(client, b["org_id"], b["staff_email"])
        assert _get(client, _clinic(b["first_patient"]), cookie_b, staff=True).status_code == 200

    def test_a_patient_session_is_not_a_staff_session(self, client, world):
        org = _a(world)
        patient_cookie = _patient_cookie(client, org)
        for cookie_name in (PATIENT_COOKIE, STAFF_COOKIE):
            response = client.get(_clinic(org["first_patient"]), cookies={cookie_name: patient_cookie})
            assert (response.status_code, response.content) == THE_REFUSAL

    def test_unauthenticated_is_the_one_refusal(self, client, world):
        assert (_get(client, _clinic(_a(world)["first_patient"])).status_code, _get(client, _clinic("x")).content) == (
            THE_REFUSAL
        )

    def test_the_routes_are_read_only(self, client, patient_client, world):
        org = _a(world)
        cookie = _staff_login(client, org["org_id"], org["staff_email"])
        for method in ("post", "put", "patch", "delete"):
            response = getattr(client, method)(_clinic(org["first_patient"]), cookies={STAFF_COOKIE: cookie})
            assert response.status_code == 405
        for method in ("post", "put", "patch", "delete"):
            assert getattr(patient_client, method)(LIST).status_code in (401, 405)
            assert getattr(client, method)(LIST).status_code == 404  # unmounted by default


# ─── 5. structural guards (no database needed) ───────────────────────────────


class TestStaffGenomeReadsAreAudited:
    def _rows(self, conn, patient_id=None):
        with conn.cursor() as cur:
            cur.execute(
                "SELECT action, resource_type, resource_id, outcome, details::text, org_id, user_id "
                "FROM audit_log WHERE action = 'genome_read' ORDER BY log_id"
            )
            return cur.fetchall()

    def test_a_successful_read_is_one_row_naming_the_patient_and_no_genome(self, client, dao, conn, world, tmp_path):
        org = _a(world)
        _drive(dao, org, org["first_patient"], tmp_path, _document(variants=(("chr17", 7674220),)))
        cookie = _staff_login(client, org["org_id"], org["staff_email"])
        assert self._rows(conn) == []
        assert _get(client, _clinic(org["first_patient"]), cookie, staff=True).status_code == 200
        conn.rollback()
        (row,) = self._rows(conn)
        action, resource_type, resource_id, outcome, details, org_id, user_id = row
        assert (action, resource_type, resource_id) == ("genome_read", "patient", str(org["first_patient"]))
        assert outcome == "success" and org_id == org["org_id"] and user_id == org["staff"].user_id
        for forbidden in (
            GENE_MARKER,
            ACMG_MARKER,
            PATH_MARKER,
            "7674220",
            "run_document",
            "variant",
            "assembly",
            "GRCh38",
        ):
            assert forbidden not in details, forbidden

    def test_the_detail_route_is_audited_too(self, client, dao, conn, world, tmp_path):
        org = _a(world)
        _drive(dao, org, org["first_patient"], tmp_path, _document())
        cookie = _staff_login(client, org["org_id"], org["staff_email"])
        assert _get(client, _clinic(org["first_patient"], "chromosomes/17"), cookie, staff=True).status_code == 200
        conn.rollback()
        assert len(self._rows(conn)) == 1

    def test_a_refusal_is_recorded_as_denied_without_content(self, client, dao, conn, world, tmp_path):
        org = _a(world)
        _drive(dao, org, org["first_patient"], tmp_path, _document())
        _staff_with_roles(dao, conn, org["org_id"], "lab@a.example", ["Lab technician"])
        cookie = _staff_login(client, org["org_id"], "lab@a.example")
        assert _get(client, _clinic(org["first_patient"]), cookie, staff=True).status_code == 403
        conn.rollback()
        (row,) = self._rows(conn)
        assert row[0] == "genome_read" and row[3] == "denied"
        assert GENE_MARKER not in row[4] and "GRCh38" not in row[4]

    def test_a_cross_organisation_attempt_leaves_a_row_in_the_callers_organisation_only(
        self, client, dao, conn, world, tmp_path
    ):
        a, b = world["a"], world["b"]
        _drive(dao, a, a["first_patient"], tmp_path, _document())
        cookie_b = _staff_login(client, b["org_id"], b["staff_email"])
        assert _get(client, _clinic(a["first_patient"]), cookie_b, staff=True).status_code == 404
        conn.rollback()
        rows = self._rows(conn)
        assert rows and all(r[5] == b["org_id"] for r in rows)

    def test_the_patient_portal_reader_is_not_audited_under_the_standing_ruling(
        self, client, dao, conn, world, tmp_path
    ):
        # Recorded so the choice is visible: portal readers are auditable=False pending the open ruling.
        org = _a(world)
        _drive(dao, org, org["first_patient"], tmp_path, _document())
        _patient_cookie(client, org)
        conn.rollback()
        assert [r for r in self._rows(conn) if r[0] != "genome_read"] == []


class TestStructure:
    def test_patient_routes_take_no_identifier_and_no_query_parameters(self):
        from clinical.genome_endpoints import portal_genome_router

        seen = []
        for route in portal_genome_router.routes:
            seen.append(route.path)
            assert not route.dependant.query_params, route.path
            assert not route.dependant.body_params, route.path
            assert {p.name for p in route.dependant.path_params} <= {"name"}, route.path
            assert route.methods == {"GET"}
        assert sorted(seen) == ["/portal/me/genome/chromosomes", "/portal/me/genome/chromosomes/{name}"]

    def test_the_patient_data_layer_method_has_no_identifier_parameter(self):
        # The decorators wrap with *args/**kwargs, so read the DECLARED signature from the source.
        import clinical.data_access as module

        tree = ast.parse(pathlib.Path(module.__file__).read_text(encoding="utf-8"))
        (fn,) = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "list_my_genome_analyses"]
        assert [a.arg for a in fn.args.args] == ["self", "patient_session"]
        assert not fn.args.kwonlyargs and fn.args.vararg is None and fn.args.kwarg is None

    def test_the_clinician_routes_are_get_only_under_clinic(self):
        from clinical.genome_endpoints import clinic_genome_router

        for route in clinic_genome_router.routes:
            assert route.path.startswith("/clinic/patients/") and route.methods == {"GET"}

    def test_the_endpoint_module_does_not_log_or_print(self):
        source = (pathlib.Path(__file__).resolve().parent.parent / "genome_endpoints.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        assert "logging" not in imported
        calls = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        assert "print" not in calls

    def test_the_access_log_redaction_covers_these_paths_unchanged(self):
        from clinical.log_redaction import REDACTED, redact_target

        target = redact_target("/portal/me/genome/chromosomes/17?note=someone@example.org")
        assert target == f"/portal/me/genome/chromosomes/17?note={REDACTED}"

    def test_the_summary_is_derived_from_the_validated_assembly_not_guessed(self):
        from clinical.genome_analysis import summarise_run_document

        assert summarise_run_document({"variants": []}).status == "assembly_unavailable"
