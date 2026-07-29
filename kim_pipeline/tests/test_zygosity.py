"""
tests/test_zygosity.py
───────────────────────
Unit tests for pipeline.zygosity.extractor.
"""

import pytest
from pipeline.zygosity.extractor import ZygosityExtractor, ZygosityResult


# Helper: call extractor with only GT (no FORMAT fields)
def _extract(gt, fmt_keys=None, fmt_vals=None):
    return ZygosityExtractor.extract(
        gt,
        fmt_keys or [],
        fmt_vals or [],
    )


def _with_format(gt, **kwargs):
    """Build a ZygosityResult from GT + keyword FORMAT fields."""
    keys = ["GT"] + list(kwargs.keys())
    vals = [gt] + list(kwargs.values())
    return ZygosityExtractor.extract(gt, keys, vals)


# ── Zygosity determination ─────────────────────────────────────────────────────

def test_heterozygous():
    r = _extract("0/1")
    assert r.zygosity == "heterozygous"


def test_homozygous_alt():
    r = _extract("1/1")
    assert r.zygosity == "homozygous_alt"


def test_homozygous_ref():
    r = _extract("0/0")
    assert r.zygosity == "homozygous_ref"


def test_no_call():
    r = _extract("./.")
    assert r.zygosity == "no_call"


def test_hemizygous():
    r = _extract("1")
    assert r.zygosity == "hemizygous"


def test_multi_allelic():
    r = _extract("0/1/2")
    assert r.zygosity == "multi_allelic"


def test_phased_het():
    r = _extract("0|1")
    assert r.zygosity == "heterozygous"
    assert r.gt == "0|1"


# ── FORMAT field parsing ───────────────────────────────────────────────────────

def test_ad_parsed():
    r = _with_format("0/1", AD="10,25")
    assert r.ad == [10, 25]


def test_dp_parsed():
    r = _with_format("0/1", DP="35")
    assert r.dp == 35


def test_ab_computed():
    r = _with_format("0/1", AD="10,25")
    expected = 25 / 35
    assert r.ab == pytest.approx(expected, rel=1e-5)


def test_gq_parsed():
    r = _with_format("0/1", GQ="99")
    assert r.gq == 99


# ── Convenience methods ────────────────────────────────────────────────────────

def test_is_het():
    res = ZygosityResult(zygosity="heterozygous", gt="0/1",
                         ad=None, dp=None, gq=None, ab=None, phase_set=None)
    assert res.is_het() is True


def test_is_hom_alt():
    res = ZygosityResult(zygosity="homozygous_alt", gt="1/1",
                         ad=None, dp=None, gq=None, ab=None, phase_set=None)
    assert res.is_hom_alt() is True


def test_is_no_call():
    res = ZygosityResult(zygosity="no_call", gt="./.",
                         ad=None, dp=None, gq=None, ab=None, phase_set=None)
    assert res.is_no_call() is True
