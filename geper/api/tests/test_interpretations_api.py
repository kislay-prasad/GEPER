"""
tests/test_interpretations_api.py
─────────────────────────────────
Integration tests for POST/GET /interpretations endpoints.
"""

import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.main import app, get_submission_store
from api.submission_store import SubmissionStore


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
def client(store):
    """FastAPI test client with submission store override."""
    app.dependency_overrides[get_submission_store] = lambda: store
    return TestClient(app)


@pytest.fixture(autouse=True)
def cleanup():
    """Clean up dependency overrides after each test."""
    yield
    app.dependency_overrides.clear()


class TestPostInterpretations:
    """Tests for POST /interpretations endpoint."""

    def test_post_creates_new_submission_queued(self, client, store):
        """POST creates new submission with status=queued."""
        response = client.post(
            "/interpretations",
            json={
                "submission_key": "key-1",
                "vcf_path": "/path/to/vcf",
                "assembly": "hg38",
                "sample_ref": "sample-1",
                "consent_ref": "consent-1",
            },
        )

        assert response.status_code == 202
        data = response.json()
        assert data["status"] == "queued"
        assert data["id"]
        assert data["interpretation_id"] is None

    def test_post_with_optional_metadata(self, client, store):
        """POST accepts hpo_terms and qc_metrics."""
        response = client.post(
            "/interpretations",
            json={
                "submission_key": "key-2",
                "vcf_path": "/path/to/vcf",
                "assembly": "hg38",
                "sample_ref": "sample-1",
                "consent_ref": "consent-1",
                "hpo_terms": {"terms": ["HP:0001234"]},
                "qc_metrics": {"depth": 50},
            },
        )

        assert response.status_code == 202
        data = response.json()
        assert data["status"] == "queued"

    def test_post_idempotency_returns_existing_queued(self, client, store):
        """POST with same submission_key returns existing submission."""
        # First POST
        response1 = client.post(
            "/interpretations",
            json={
                "submission_key": "key-3",
                "vcf_path": "/path/1",
                "assembly": "hg38",
                "sample_ref": "sample-1",
                "consent_ref": "consent-1",
            },
        )
        assert response1.status_code == 202
        id1 = response1.json()["id"]

        # Second POST with same key — should return same ID
        response2 = client.post(
            "/interpretations",
            json={
                "submission_key": "key-3",
                "vcf_path": "/path/2",
                "assembly": "hg38",
                "sample_ref": "sample-2",
                "consent_ref": "consent-2",
            },
        )
        assert response2.status_code == 202
        id2 = response2.json()["id"]
        assert id2 == id1

    def test_post_idempotency_returns_existing_complete(self, client, store):
        """POST with same key as completed submission returns full response."""
        # Create and complete a submission
        sub = store.create_submission(
            org_id="default",
            submission_key="key-4",
            vcf_path="/path/to/vcf",
            assembly="hg38",
            sample_ref="sample-1",
            consent_ref="consent-1",
        )
        store.update_status(
            sub.id,
            "complete",
            interpretation_id="interp-1",
            run_document_ref='{"doc": "ref"}',
        )

        # POST with same key — should return same shape with interpretation_id
        response = client.post(
            "/interpretations",
            json={
                "submission_key": "key-4",
                "vcf_path": "/path/to/vcf",
                "assembly": "hg38",
                "sample_ref": "sample-1",
                "consent_ref": "consent-1",
            },
        )

        assert response.status_code == 202
        data = response.json()
        assert data["id"] == sub.id
        assert data["status"] == "complete"
        assert data["interpretation_id"] == "interp-1"

    def test_post_requires_submission_key(self, client):
        """POST requires submission_key."""
        response = client.post(
            "/interpretations",
            json={
                "vcf_path": "/path/to/vcf",
                "assembly": "hg38",
                "sample_ref": "sample-1",
                "consent_ref": "consent-1",
            },
        )
        assert response.status_code == 422

    def test_post_requires_vcf_path(self, client):
        """POST requires vcf_path."""
        response = client.post(
            "/interpretations",
            json={
                "submission_key": "key-5",
                "assembly": "hg38",
                "sample_ref": "sample-1",
                "consent_ref": "consent-1",
            },
        )
        assert response.status_code == 422

    def test_post_requires_consent_ref(self, client):
        """POST requires consent_ref."""
        response = client.post(
            "/interpretations",
            json={
                "submission_key": "key-6",
                "vcf_path": "/path/to/vcf",
                "assembly": "hg38",
                "sample_ref": "sample-1",
            },
        )
        assert response.status_code == 422


class TestGetInterpretations:
    """Tests for GET /interpretations/{id} endpoint."""

    def test_get_queued_submission(self, client, store):
        """GET returns queued submission with status."""
        sub = store.create_submission(
            org_id="default",
            submission_key="key-7",
            vcf_path="/path/to/vcf",
            assembly="hg38",
            sample_ref="sample-1",
            consent_ref="consent-1",
        )

        response = client.get(f"/interpretations/{sub.id}")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "queued"
        assert data["id"] == sub.id
        assert data["interpretation_id"] is None
        assert data["error_message"] is None

    def test_get_running_submission(self, client, store):
        """GET returns running submission."""
        sub = store.create_submission(
            org_id="default",
            submission_key="key-8",
            vcf_path="/path/to/vcf",
            assembly="hg38",
            sample_ref="sample-1",
            consent_ref="consent-1",
        )
        store.update_status(sub.id, "running")

        response = client.get(f"/interpretations/{sub.id}")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "running"

    def test_get_complete_submission(self, client, store):
        """GET returns complete submission with interpretation_id."""
        sub = store.create_submission(
            org_id="default",
            submission_key="key-9",
            vcf_path="/path/to/vcf",
            assembly="hg38",
            sample_ref="sample-1",
            consent_ref="consent-1",
        )
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
        sub = store.create_submission(
            org_id="default",
            submission_key="key-10",
            vcf_path="/path/to/vcf",
            assembly="hg38",
            sample_ref="sample-1",
            consent_ref="consent-1",
        )
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
        sub = store.create_submission(
            org_id="default",
            submission_key="key-11",
            vcf_path="/path/to/vcf",
            assembly="hg38",
            sample_ref="sample-1",
            consent_ref="consent-1",
        )
        store.update_status(
            sub.id,
            "interrupted",
            error_message="Worker died mid-execution at startup reconciliation",
        )

        response = client.get(f"/interpretations/{sub.id}")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "interrupted"

    def test_get_nonexistent_submission(self, client):
        """GET nonexistent submission returns 404."""
        response = client.get("/interpretations/nonexistent-id")
        assert response.status_code == 404
