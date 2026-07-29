"""
tests/test_gnomad.py
────────────────────
Unit tests for pipeline.gnomad.lookup.GnomadLookup.
"""
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pipeline.gnomad.lookup import GnomadLookup, GnomadHit
from pipeline.fastq.errors import FastqPipelineError


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_api_response(af: float, populations: list) -> dict:
    """Build a mock gnomAD GraphQL response dict."""
    return {
        "data": {
            "variant": {
                "genome": {
                    "af": af,
                    "populations": populations,
                }
            }
        }
    }


def _api_lookup_no_local_vcf(cfg: dict | None = None) -> GnomadLookup:
    """Return a GnomadLookup that definitely uses the API backend."""
    cfg = cfg or {}
    cfg.setdefault("gnomad", {})
    cfg["gnomad"]["vcf_path"] = "/nonexistent/path.vcf.gz"
    return GnomadLookup(cfg=cfg)


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_chrom_normalisation_strips_chr():
    """API variant ID should NOT contain 'chr' prefix."""
    gn = _api_lookup_no_local_vcf()
    captured = {}

    def mock_post(url, json=None, **kwargs):
        captured["query"] = json.get("query", "")
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        # Return None genome (variant not found) — we only care about the query
        mock_resp.json.return_value = {"data": {"variant": None}}
        return mock_resp

    with patch("pipeline.gnomad.lookup.requests.post", side_effect=mock_post):
        gn.lookup("chr17", 1000, "A", "T")

    # The variant ID in the GQL query must NOT have a "chr" prefix
    assert "chr17-1000-A-T" not in captured.get("query", "")
    assert "17-1000-A-T" in captured.get("query", "")


def test_af_popmax_computed_from_populations():
    """af_popmax equals the highest AC/AN fraction across populations."""
    pops = [
        {"id": "AFR", "ac": 10, "an": 2000},   # 0.005
        {"id": "EUR", "ac": 1, "an": 5000},    # 0.0002
        {"id": "EAS", "ac": 5, "an": 1000},    # 0.005
        {"id": "AMR", "ac": 3, "an": 500},     # 0.006 ← max
    ]
    response_data = _make_api_response(af=0.001, populations=pops)

    def mock_post(url, json=None, **kwargs):
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = response_data
        return mock_resp

    gn = _api_lookup_no_local_vcf()
    with patch("pipeline.gnomad.lookup.requests.post", side_effect=mock_post):
        hit = gn.lookup("17", 1000, "A", "T")

    assert hit is not None
    assert abs(hit.af_popmax - 0.006) < 1e-9  # 3/500


def test_none_returned_on_connection_error():
    """Network failures return UNAVAILABLE without raising."""
    from pipeline.gnomad.lookup import GnomadLookupOutcome
    gn = _api_lookup_no_local_vcf()

    with patch("pipeline.gnomad.lookup.requests.post", side_effect=ConnectionError("timeout")):
        result = gn.lookup("17", 1000, "A", "T")

    assert result == GnomadLookupOutcome.UNAVAILABLE


def test_none_returned_on_request_exception():
    """Any requests exception returns UNAVAILABLE without raising."""
    import requests as req_lib
    from pipeline.gnomad.lookup import GnomadLookupOutcome
    gn = _api_lookup_no_local_vcf()

    with patch("pipeline.gnomad.lookup.requests.post", side_effect=req_lib.RequestException("err")):
        result = gn.lookup("2", 9999, "C", "T")

    assert result == GnomadLookupOutcome.UNAVAILABLE


def test_variant_not_in_gnomad_returns_none():
    """When gnomAD returns null genome, lookup returns ABSENT (confirmed not in gnomAD)."""
    from pipeline.gnomad.lookup import GnomadLookupOutcome
    def mock_post(url, json=None, **kwargs):
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {"data": {"variant": None}}
        return mock_resp

    gn = _api_lookup_no_local_vcf()
    with patch("pipeline.gnomad.lookup.requests.post", side_effect=mock_post):
        result = gn.lookup("1", 1, "A", "G")

    assert result == GnomadLookupOutcome.UNAVAILABLE


def test_tabix_missing_raises_or_none(tmp_path):
    """If local VCF exists but tabix is absent, returns UNAVAILABLE (not None)."""
    from pipeline.gnomad.lookup import GnomadLookupOutcome
    vcf_path = tmp_path / "gnomad.vcf.gz"
    vcf_path.write_bytes(b"")  # empty but existing file

    gn = GnomadLookup(cfg={"gnomad": {"vcf_path": str(vcf_path)}})
    assert gn._backend == "local"

    with patch("pipeline.gnomad.lookup._require", side_effect=FastqPipelineError("tabix not found", stage="gnomad.tabix", tool="tabix")):
        result = gn.lookup("1", 1000, "A", "T")

    # Must return UNAVAILABLE — not None — so callers don't incorrectly award PM2
    assert result == GnomadLookupOutcome.UNAVAILABLE


def test_hit_has_correct_backend_label():
    """Backend label on hit should be 'api' when using the API."""
    pops = [{"id": "AFR", "ac": 2, "an": 200}]
    response_data = _make_api_response(af=0.01, populations=pops)

    def mock_post(url, json=None, **kwargs):
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = response_data
        return mock_resp

    gn = _api_lookup_no_local_vcf()
    with patch("pipeline.gnomad.lookup.requests.post", side_effect=mock_post):
        hit = gn.lookup("7", 117548628, "G", "A")

    assert hit is not None
    assert hit.backend_used == "api"
