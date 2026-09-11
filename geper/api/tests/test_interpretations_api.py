"""
tests/test_interpretations_api.py
─────────────────────────────────
Integration tests for POST/GET /interpretations endpoints.

2026-09-12 (D0, "build the link"): a submission now belongs to the
clinical ORGANISATION its API key is bound to (GEPER_API_KEYS entries are
`key:org_uuid`) and names the clinical ORDER and SAMPLE it interprets. The
organisation is never read from the request body -- any key holder could
otherwise write into any tenant -- and the old hard-coded "default"
organisation is gone. Every request below therefore carries a key; the
fixtures bind two keys to two organisations so cross-tenant reads can be
demonstrated, not assumed.
"""

import tempfile
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import api.main as api_main
from api.main import app, get_submission_store
from api.submission_store import SubmissionStore

ORG_A = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
ORG_B = uuid.UUID("bbbbbbbb-0000-4000-8000-000000000002")
KEY_A = "key-for-org-a"
KEY_B = "key-for-org-b"
ORDER = uuid.UUID("0d0d0d0d-0000-4000-8000-000000000003")
SAMPLE = uuid.UUID("5a5a5a5a-0000-4000-8000-000000000004")


def _body(**overrides):
    body = {
        "submission_key": "key-1",
        "vcf_path": "/path/to/vcf",
        "assembly": "hg38",
        "sample_ref": "sample-1",
        "consent_ref": "consent-1",
        "order_id": str(ORDER),
        "sample_id": str(SAMPLE),
    }
    body.update(overrides)
    return {k: v for k, v in body.items() if v is not None}


@pytest.fixture
def db_path():
    """Create a temporary database."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Path(tmpdir) / "test.db"
        yield db


@pytest.fixture
def store(db_path):
    """Create a test SubmissionStore."""
    return SubmissionStore(db_path)


@pytest.fixture
def keys(monkeypatch):
    """Two keys, two organisations. `_API_KEYS` is read per request, so
    patching the module attribute is exactly what a configured deployment
    looks like to the dependency."""
    monkeypatch.setattr(api_main, "_API_KEYS", {KEY_A: ORG_A, KEY_B: ORG_B})


@pytest.fixture
def client(store, keys):
    """FastAPI test client authenticated as organisation A."""
    app.dependency_overrides[get_submission_store] = lambda: store
    return TestClient(app, headers={"X-Api-Key": KEY_A})


@pytest.fixture
def client_b(store, keys):
    """Same app and store, authenticated as organisation B."""
    app.dependency_overrides[get_submission_store] = lambda: store
    return TestClient(app, headers={"X-Api-Key": KEY_B})


@pytest.fixture(autouse=True)
def cleanup():
    """Clean up dependency overrides after each test."""
    yield
    app.dependency_overrides.clear()


def _stored(store, **kwargs):
    defaults = dict(
        org_id=str(ORG_A),
        submission_key="key-x",
        vcf_path="/path/to/vcf",
        assembly="hg38",
        sample_ref="sample-1",
        consent_ref="consent-1",
        order_id=str(ORDER),
        sample_id=str(SAMPLE),
    )
    defaults.update(kwargs)
    return store.create_submission(**defaults)


class TestApiKeyParsing:
    """GEPER_API_KEYS is `key:org_uuid,...`. A key with no organisation
    cannot own a submission, so the old bare-key format is refused at
    startup rather than silently bound to nothing."""

    def test_parses_key_org_pairs(self):
        parsed = api_main._parse_api_keys(f" {KEY_A}:{ORG_A} , {KEY_B}:{ORG_B} ")
        assert parsed == {KEY_A: ORG_A, KEY_B: ORG_B}

    def test_unset_is_dev_mode(self):
        assert api_main._parse_api_keys("") is None
        assert api_main._parse_api_keys(" , ") is None

    @pytest.mark.parametrize(
        "raw",
        [
            "bare-key",  # the pre-2026-09-12 format: no organisation
            "k1:not-a-uuid",
            ":" + str(ORG_A),  # empty key
            f"k1:{ORG_A},k1:{ORG_B}",  # one key, two organisations
        ],
    )
    def test_malformed_entries_are_refused(self, raw):
        with pytest.raises(ValueError):
            api_main._parse_api_keys(raw)

    def test_bare_key_format_refuses_to_start(self):
        """At import, in a subprocess (the module exits on a bad config)."""
        import os
        import subprocess
        import sys

        geper_root = Path(__file__).resolve().parents[2]
        env = dict(os.environ)
        env["GEPER_API_KEYS"] = "legacy-key-without-org"
        env["GEPER_CORS_ORIGINS"] = "https://example.test"
        proc = subprocess.run(
            [sys.executable, "-c", "import api.main"],
            cwd=geper_root,
            env=env,
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 1, proc.stderr
        assert "key:org_uuid" in proc.stderr
        assert "legacy-key-without-org" not in proc.stderr, "a key must never be echoed to a log"


class TestPostInterpretations:
    """Tests for POST /interpretations endpoint."""

    def test_post_creates_new_submission_queued(self, client, store):
        """POST creates new submission with status=queued, owned by the
        key's organisation and naming the clinical order and sample."""
        response = client.post("/interpretations", json=_body())

        assert response.status_code == 202
        data = response.json()
        assert data["status"] == "queued"
        assert data["id"]
        assert data["interpretation_id"] is None

        stored = store.get_submission(data["id"])
        assert stored.org_id == str(ORG_A)
        assert stored.order_id == str(ORDER)
        assert stored.sample_id == str(SAMPLE)

    def test_organisation_comes_from_the_key_not_the_body(self, client, store):
        response = client.post("/interpretations", json=_body(org_id=str(ORG_B)))
        assert response.status_code == 202
        assert store.get_submission(response.json()["id"]).org_id == str(ORG_A)

    def test_no_default_organisation_in_dev_mode(self, store, monkeypatch):
        """With no keys configured there is no organisation to own a
        submission; the old code filed it under "default"."""
        monkeypatch.setattr(api_main, "_API_KEYS", None)
        app.dependency_overrides[get_submission_store] = lambda: store
        response = TestClient(app).post("/interpretations", json=_body())
        assert response.status_code == 401
        assert "organisation" in response.json()["detail"]

    def test_unknown_key_is_refused(self, store, keys):
        app.dependency_overrides[get_submission_store] = lambda: store
        response = TestClient(app, headers={"X-Api-Key": "nope"}).post("/interpretations", json=_body())
        assert response.status_code == 401

    def test_post_with_optional_metadata(self, client, store):
        """POST accepts hpo_terms and qc_metrics."""
        response = client.post(
            "/interpretations",
            json=_body(submission_key="key-2", hpo_terms={"terms": ["HP:0001234"]}, qc_metrics={"depth": 50}),
        )

        assert response.status_code == 202
        data = response.json()
        assert data["status"] == "queued"

    def test_post_idempotency_returns_existing_queued(self, client, store):
        """POST with same submission_key returns existing submission."""
        response1 = client.post("/interpretations", json=_body(submission_key="key-3", vcf_path="/path/1"))
        assert response1.status_code == 202
        id1 = response1.json()["id"]

        response2 = client.post(
            "/interpretations",
            json=_body(submission_key="key-3", vcf_path="/path/2", sample_ref="sample-2", consent_ref="consent-2"),
        )
        assert response2.status_code == 202
        assert response2.json()["id"] == id1

    def test_idempotency_is_per_organisation(self, client, client_b, store):
        """The same client key under two organisations is two submissions."""
        id_a = client.post("/interpretations", json=_body(submission_key="shared")).json()["id"]
        id_b = client_b.post("/interpretations", json=_body(submission_key="shared")).json()["id"]
        assert id_a != id_b
        assert store.get_submission(id_b).org_id == str(ORG_B)

    def test_post_idempotency_returns_existing_complete(self, client, store):
        """POST with same key as completed submission returns full response."""
        sub = _stored(store, submission_key="key-4")
        store.update_status(
            sub.id,
            "complete",
            interpretation_id="interp-1",
            run_document_ref='{"doc": "ref"}',
        )

        response = client.post("/interpretations", json=_body(submission_key="key-4"))

        assert response.status_code == 202
        data = response.json()
        assert data["id"] == sub.id
        assert data["status"] == "complete"
        assert data["interpretation_id"] == "interp-1"

    @pytest.mark.parametrize("missing", ["submission_key", "vcf_path", "consent_ref", "order_id", "sample_id"])
    def test_post_requires_field(self, client, missing):
        response = client.post("/interpretations", json=_body(**{missing: None}))
        assert response.status_code == 422

    @pytest.mark.parametrize("field", ["order_id", "sample_id"])
    def test_clinical_ids_must_be_uuids(self, client, field):
        response = client.post("/interpretations", json=_body(**{field: "ORD-123"}))
        assert response.status_code == 422


class TestGetInterpretations:
    """Tests for GET /interpretations/{id} endpoint."""

    def test_get_queued_submission(self, client, store):
        """GET returns queued submission with status."""
        sub = _stored(store, submission_key="key-7")

        response = client.get(f"/interpretations/{sub.id}")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "queued"
        assert data["id"] == sub.id
        assert data["interpretation_id"] is None
        assert data["error_message"] is None

    def test_another_organisations_submission_is_not_found(self, client, client_b, store):
        """Org B holding Org A's submission id learns nothing -- 404, the
        same answer as an id that does not exist."""
        sub = _stored(store, submission_key="key-a-only")
        assert client.get(f"/interpretations/{sub.id}").status_code == 200
        assert client_b.get(f"/interpretations/{sub.id}").status_code == 404

    def test_get_running_submission(self, client, store):
        """GET returns running submission."""
        sub = _stored(store, submission_key="key-8")
        store.update_status(sub.id, "running")

        response = client.get(f"/interpretations/{sub.id}")

        assert response.status_code == 200
        assert response.json()["status"] == "running"

    def test_get_complete_submission(self, client, store):
        """GET returns complete submission with interpretation_id."""
        sub = _stored(store, submission_key="key-9")
        store.update_status(
            sub.id,
            "complete",
            interpretation_id="interp-uuid",
            run_document_ref='{"id": "doc-1"}',
        )

        response = client.get(f"/interpretations/{sub.id}")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "complete"
        assert data["interpretation_id"] == "interp-uuid"
        assert data["error_message"] is None

    def test_get_failed_submission_with_error(self, client, store):
        """GET returns failed submission with error message."""
        sub = _stored(store, submission_key="key-10")
        error = "Interpretation timed out after 1800s"
        store.update_status(sub.id, "failed", error_message=error)

        response = client.get(f"/interpretations/{sub.id}")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "failed"
        assert data["error_message"] == error
        assert data["interpretation_id"] is None

    def test_get_interrupted_submission(self, client, store):
        """GET returns interrupted submission."""
        sub = _stored(store, submission_key="key-11")
        store.update_status(
            sub.id,
            "interrupted",
            error_message="Worker died mid-execution at startup reconciliation",
        )

        response = client.get(f"/interpretations/{sub.id}")

        assert response.status_code == 200
        assert response.json()["status"] == "interrupted"

    def test_get_nonexistent_submission(self, client):
        """GET nonexistent submission returns 404."""
        response = client.get("/interpretations/nonexistent-id")
        assert response.status_code == 404

    def test_get_without_organisation_is_refused(self, store, monkeypatch):
        monkeypatch.setattr(api_main, "_API_KEYS", None)
        app.dependency_overrides[get_submission_store] = lambda: store
        sub = _stored(store, submission_key="key-12")
        assert TestClient(app).get(f"/interpretations/{sub.id}").status_code == 401
