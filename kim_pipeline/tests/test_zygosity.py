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


# ── Malformed vs valid discrimination ──────────────────────────────────────────
# DEFECT-zygosity-collapses-absent-and-malformed: a MALFORMED GT token (empty
# string from a truncated sample column, or non-numeric garbage) was silently
# scored through the same boolean logic as a real allele index, landing on a
# real canonical zygosity value indistinguishable from a genuinely valid call
# of that shape -- e.g. gt="" and gt="BAD" both produced "hemizygous", the
# same value a genuine haploid call "1" produces. `malformed` makes that
# collapse visible to a caller without changing what any zygosity value means.


def test_empty_gt_token_is_flagged_malformed():
    r = _extract("")
    assert r.malformed is True


def test_garbage_gt_token_is_flagged_malformed():
    r = _extract("BAD")
    assert r.malformed is True


def test_valid_hemizygous_is_not_flagged_malformed():
    r = _extract("1")
    assert r.zygosity == "hemizygous"
    assert r.malformed is False


def test_valid_no_call_is_not_flagged_malformed():
    r = _extract("./.")
    assert r.zygosity == "no_call"
    assert r.malformed is False


def test_malformed_and_valid_hemizygous_still_share_zygosity_value():
    """Documents the residual: `zygosity` itself is unchanged by design
    (out of scope to redefine it) -- `malformed` is the only discriminator.
    A caller that ignores `malformed` still sees the old collapse."""
    malformed = _extract("BAD")
    valid = _extract("1")
    assert malformed.zygosity == valid.zygosity == "hemizygous"
    assert malformed.malformed != valid.malformed


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
    res = ZygosityResult(
        zygosity="heterozygous", gt="0/1", ad=None, dp=None, gq=None, ab=None, phase_set=None
    )
    assert res.is_het() is True


def test_is_hom_alt():
    res = ZygosityResult(
        zygosity="homozygous_alt", gt="1/1", ad=None, dp=None, gq=None, ab=None, phase_set=None
    )
    assert res.is_hom_alt() is True


def test_is_no_call():
    res = ZygosityResult(
        zygosity="no_call", gt="./.", ad=None, dp=None, gq=None, ab=None, phase_set=None
    )
    assert res.is_no_call() is True
