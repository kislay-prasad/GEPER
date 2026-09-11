"""
Per-sample variant allele fraction (VAF): the three states must stay apart.

NABL 112A Issue 01 (18-Dec-2024) s.7.8.5(b)(iii) requires the laboratory to
work with within-sample allele fractions. The data was always in the input
-- `VCFParser._parse_data_line` split the whole line and then used only the
first eight columns -- so this is a field that was parsed and discarded, not
one that was absent.

What these tests exist to hold down is narrower than "VAF is computed": it
is that a MISSING fraction never renders as a number, and that an ABSENCE
and a FAILURE never collapse to the same value. Those two are separate
claims and a fix that produced the right float while flattening the other
two states would have reproduced the defect it was written to close.

Deliberately NOT tested here, because GEPER does not do it: zygosity. The
clause implies a het/hom range, GEPER makes no zygosity call anywhere, and
that gap is carded for clinical sign-off rather than smuggled in with the
plumbing.
"""

import csv
import os

import pytest

from pipeline.stage_schemas import StageStatus
from pipeline.vcf_parser import VCFParser
from report.clinical_report_builder import variant_allele_fraction_text
from report.export_lims import LIMS_VAF_MULTIPLE_SAMPLES, _variant_allele_fraction

_HEADER_NO_SAMPLES = "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
_HEADER_ONE_SAMPLE = "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tNA12878\n"
_HEADER_TRIO = "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tNA12878\tNA12891\tNA12892\n"


def _parse(tmp_path, header, *data_lines):
    path = os.path.join(str(tmp_path), "in.vcf")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("##fileformat=VCFv4.2\n")
        fh.write(header)
        for line in data_lines:
            fh.write(line if line.endswith("\n") else line + "\n")
    return VCFParser(path).parse()


def _fractions(variant):
    return variant.to_dict()["variant_allele_fractions"]


def _result(variant):
    """The `variant_result` shape the report renderers actually receive."""
    return {"variant": variant.to_dict()}


# ---------------------------------------------------------------------------
# Known-positive first: if the FOUND path did not work, every "is not a
# number" assertion below would pass for the wrong reason.
# ---------------------------------------------------------------------------


def test_ad_backed_fraction_is_computed_and_rendered(tmp_path):
    (variant,) = _parse(tmp_path, _HEADER_ONE_SAMPLE, "1\t100\t.\tA\tG\t50\tPASS\t.\tGT:AD:DP\t0/1:25,23:48")
    state = _fractions(variant)["NA12878"]

    assert state["status"] == StageStatus.FOUND.value
    assert state["value"] == pytest.approx(23 / 48)
    assert state["alt_depth"] == 23
    assert state["depth"] == 48
    assert state["source"] == "format_ad"

    text = variant_allele_fraction_text(_result(variant))
    assert "23/48 reads" in text
    assert "NA12878" in text


def test_denominator_is_sum_ad_not_dp(tmp_path):
    """DP is post-filter total depth and can differ from the summed allele depths; dividing by it would understate the fraction."""
    (variant,) = _parse(tmp_path, _HEADER_ONE_SAMPLE, "1\t100\t.\tA\tG\t50\tPASS\t.\tGT:AD:DP\t0/1:10,10:97")
    state = _fractions(variant)["NA12878"]

    assert state["value"] == pytest.approx(0.5), "must divide by sum(AD)=20, not DP=97"
    assert state["depth"] == 20
    assert state["dp"] == 97, "DP is still reported for the reviewer, just never divided by"


# ---------------------------------------------------------------------------
# State (2) ABSENT -- never a number, never zero.
# ---------------------------------------------------------------------------


def test_sites_only_vcf_yields_no_fraction_and_renders_as_never_measured(tmp_path):
    (variant,) = _parse(tmp_path, _HEADER_NO_SAMPLES, "1\t100\t.\tA\tG\t50\tPASS\t.")

    assert _fractions(variant) == {}, "no genotype columns means no per-sample entries at all"

    text = variant_allele_fraction_text(_result(variant))
    assert "no per-sample genotype columns" in text
    assert "not a fraction of zero" in text
    for numeral in ("0.0", "0%", "n/a", "N/A"):
        assert numeral not in text, f"an unmeasured fraction must not render as {numeral!r}"


def test_format_without_ad_or_af_is_not_run_not_error(tmp_path):
    (variant,) = _parse(tmp_path, _HEADER_ONE_SAMPLE, "1\t100\t.\tA\tG\t50\tPASS\t.\tGT:DP\t0/1:48")
    state = _fractions(variant)["NA12878"]

    assert state["status"] == StageStatus.NOT_RUN.value, "nothing was measured; nothing failed"
    assert state["value"] is None
    assert state["reason"] is not None


def test_not_found_is_never_used(tmp_path):
    """
    NOT_FOUND means "checked, and confirmed absent" -- a reading a number
    that was never measured does not have. The distinction is the whole
    point of the tri-state, so it is asserted rather than left to review.
    """
    variants = _parse(
        tmp_path,
        _HEADER_ONE_SAMPLE,
        "1\t100\t.\tA\tG\t50\tPASS\t.\tGT:AD\t0/1:25,23",
        "1\t200\t.\tC\tT\t50\tPASS\t.\tGT:DP\t0/1:48",
        "1\t300\t.\tG\tA\t50\tPASS\t.\tGT:AD\t0/1:bogus",
    )
    seen = {state["status"] for v in variants for state in _fractions(v).values()}
    assert seen == {StageStatus.FOUND.value, StageStatus.NOT_RUN.value, StageStatus.ERROR.value}
    assert StageStatus.NOT_FOUND.value not in seen


# ---------------------------------------------------------------------------
# State (3) UNPARSEABLE -- distinct from ABSENT. This is the pair the whole
# card exists to keep apart, so each failure mode is asserted by name.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "data_line, expected_fragment",
    [
        # AD that isn't integers at all.
        ("1\t100\t.\tA\tG\t50\tPASS\t.\tGT:AD\t0/1:25,bogus", "not a comma-separated list of integers"),
        # AD too short for the ALT being evaluated.
        ("1\t100\t.\tA\tG\t50\tPASS\t.\tGT:AD\t0/1:25", "no depth at index 1"),
        # No reads at all: 0/0 is undefined, which is not the same claim as 0%.
        ("1\t100\t.\tA\tG\t50\tPASS\t.\tGT:AD\t0/1:0,0", "undefined here rather than zero"),
        # More values than FORMAT declares has no legal reading.
        ("1\t100\t.\tA\tG\t50\tPASS\t.\tGT:AD\t0/1:25,23:99", "FORMAT declares only"),
        # FORMAT/AF present but not a number.
        ("1\t100\t.\tA\tG\t50\tPASS\t.\tGT:AF\t0/1:bogus", "is not a number"),
    ],
)
def test_malformed_format_is_error_with_a_reason_never_absence(tmp_path, data_line, expected_fragment):
    (variant,) = _parse(tmp_path, _HEADER_ONE_SAMPLE, data_line)
    state = _fractions(variant)["NA12878"]

    assert state["status"] == StageStatus.ERROR.value
    assert state["value"] is None
    assert expected_fragment in state["reason"]

    text = variant_allele_fraction_text(_result(variant))
    assert "Could not be determined" in text
    assert "no per-sample genotype columns" not in text, "a failure must not read as an absence"


def test_zero_alt_depth_with_real_coverage_is_a_real_zero_not_an_absence(tmp_path):
    """
    The mirror image of the sum(AD)==0 case: 0 alt reads out of 48 IS a
    measured fraction of zero, and must survive as one. A `0.0 or 'n/a'`
    idiom anywhere on this path would erase it.
    """
    (variant,) = _parse(tmp_path, _HEADER_ONE_SAMPLE, "1\t100\t.\tA\tG\t50\tPASS\t.\tGT:AD\t0/0:48,0")
    state = _fractions(variant)["NA12878"]

    assert state["status"] == StageStatus.FOUND.value
    assert state["value"] == 0.0
    text = variant_allele_fraction_text(_result(variant))
    assert "0/48 reads" in text
    assert "Not applicable" not in text and "no per-sample genotype columns" not in text


def test_trailing_dropped_format_fields_are_legal_not_an_error(tmp_path):
    """VCF 4.x permits dropping trailing FORMAT fields, so a short sample column is an absence, not a malformation."""
    (variant,) = _parse(tmp_path, _HEADER_ONE_SAMPLE, "1\t100\t.\tA\tG\t50\tPASS\t.\tGT:AD:DP\t0/1:25,23")
    state = _fractions(variant)["NA12878"]

    assert state["status"] == StageStatus.FOUND.value
    assert state["value"] == pytest.approx(23 / 48)
    assert state["dp"] is None


def test_sample_column_count_mismatch_errors_every_sample(tmp_path):
    (variant,) = _parse(tmp_path, _HEADER_TRIO, "1\t100\t.\tA\tG\t50\tPASS\t.\tGT:AD\t0/1:25,23\t0/0:48,0")
    states = _fractions(variant)

    assert set(states) == {"NA12878", "NA12891", "NA12892"}
    assert {s["status"] for s in states.values()} == {StageStatus.ERROR.value}
    assert all("declares 3" in s["reason"] for s in states.values())


# ---------------------------------------------------------------------------
# Multiallelic: AD is indexed against the ORIGINAL ALT list, and a skipped
# ALT must not shift the alleles after it.
# ---------------------------------------------------------------------------


def test_multiallelic_alt_index_selects_the_right_allele_depth(tmp_path):
    variants = _parse(tmp_path, _HEADER_ONE_SAMPLE, "1\t100\t.\tA\tG,T\t50\tPASS\t.\tGT:AD:DP\t1/2:10,30,60:100")
    by_alt = {v.alt: _fractions(v)["NA12878"] for v in variants}

    assert by_alt["G"]["alt_depth"] == 30
    assert by_alt["T"]["alt_depth"] == 60
    assert by_alt["G"]["value"] == pytest.approx(0.3)
    assert by_alt["T"]["value"] == pytest.approx(0.6)


def test_skipped_spanning_deletion_does_not_shift_later_allele_depths(tmp_path):
    """
    `*` is skipped by `_parse_data_line` before any Variant is yielded. If
    the AD index were derived from a counter advanced only for surviving
    alleles, T below would read ALT1's depth (30) instead of its own (60).
    """
    variants = _parse(tmp_path, _HEADER_ONE_SAMPLE, "1\t100\t.\tA\t*,T\t50\tPASS\t.\tGT:AD:DP\t1/2:10,30,60:100")

    assert [v.alt for v in variants] == ["T"], "the '*' placeholder is skipped, not yielded"
    assert _fractions(variants[0])["NA12878"]["alt_depth"] == 60


# ---------------------------------------------------------------------------
# Multi-sample rendering: tri-state resolves FIRST, sample count SECOND.
# ---------------------------------------------------------------------------


def test_multi_sample_with_no_data_anywhere_does_not_point_at_the_json(tmp_path):
    """
    The hole the precedence rule closes: reading the sample count first
    would render "multiple samples; see JSON" here, sending a reader to a
    JSON that holds three `not_run` entries and no number -- asserting
    data exists where none does.
    """
    (variant,) = _parse(tmp_path, _HEADER_TRIO, "1\t100\t.\tA\tG\t50\tPASS\t.\tGT:DP\t0/1:48\t0/0:50\t0/1:44")
    text = variant_allele_fraction_text(_result(variant))

    assert "see geper_results.json" not in text
    assert "samples carry an allele fraction" not in text
    assert "neither AD" in text


def test_several_found_samples_point_at_the_json(tmp_path):
    (variant,) = _parse(tmp_path, _HEADER_TRIO, "1\t100\t.\tA\tG\t50\tPASS\t.\tGT:AD\t0/1:25,23\t0/0:48,0\t0/1:20,20")
    text = variant_allele_fraction_text(_result(variant))

    assert "3 samples carry an allele fraction" in text
    assert "geper_results.json" in text


def test_one_found_sample_among_several_is_named_and_the_others_are_not_hidden(tmp_path):
    (variant,) = _parse(tmp_path, _HEADER_TRIO, "1\t100\t.\tA\tG\t50\tPASS\t.\tGT:AD\t0/1:25,23\t0/0:bogus\t0/1:.")
    text = variant_allele_fraction_text(_result(variant))

    assert "NA12878" in text, "a fraction never travels without its sample name"
    assert "2 further sample(s)" in text, "the other samples must not vanish behind one confident number"


# ---------------------------------------------------------------------------
# LIMS: the status field is what keeps the empty cells apart.
# ---------------------------------------------------------------------------


def test_lims_never_emits_a_null_fraction_without_a_status_saying_which_null(tmp_path):
    cases = {
        "found": ("1\t100\t.\tA\tG\t50\tPASS\t.\tGT:AD\t0/1:25,23", _HEADER_ONE_SAMPLE),
        StageStatus.NOT_RUN.value: ("1\t100\t.\tA\tG\t50\tPASS\t.\tGT:DP\t0/1:48", _HEADER_ONE_SAMPLE),
        StageStatus.ERROR.value: ("1\t100\t.\tA\tG\t50\tPASS\t.\tGT:AD\t0/1:bogus", _HEADER_ONE_SAMPLE),
        LIMS_VAF_MULTIPLE_SAMPLES: (
            "1\t100\t.\tA\tG\t50\tPASS\t.\tGT:AD\t0/1:25,23\t0/0:48,0\t0/1:20,20",
            _HEADER_TRIO,
        ),
    }
    for expected_status, (data_line, header) in cases.items():
        (variant,) = _parse(tmp_path, header, data_line)
        value, status, sample, reason = _variant_allele_fraction(_result(variant))

        assert status == expected_status, f"{expected_status}: got {status}"
        if expected_status == StageStatus.FOUND.value:
            assert value is not None and sample == "NA12878"
        else:
            assert value is None
            assert sample is None
            assert reason, "a null fraction must always carry the reason it is null"


def test_document_predating_the_field_does_not_claim_anything_about_the_vcf():
    """
    A re-render of a `geper_results.json` written before this field
    existed knows nothing about whether that VCF carried genotype
    columns. Saying "this VCF carries no per-sample genotype columns"
    there would be a fabricated claim about an input the document never
    recorded -- the same absence-vs-record-of-absence error the per-sample
    states themselves exist to prevent, displaced one level up onto the
    document.
    """
    legacy = {"variant": {"chrom": "1", "pos": 100, "ref": "A", "alt": "G"}}

    text = variant_allele_fraction_text(legacy)
    assert "written before per-sample allele fractions were recorded" in text
    assert "carries no per-sample genotype columns" not in text

    value, status, sample, reason = _variant_allele_fraction(legacy)
    assert (value, status, sample) == (None, StageStatus.NOT_RUN.value, None)
    assert "never captured for this run" in reason


def test_lims_sites_only_vcf_is_not_run_not_error(tmp_path):
    (variant,) = _parse(tmp_path, _HEADER_NO_SAMPLES, "1\t100\t.\tA\tG\t50\tPASS\t.")
    value, status, sample, reason = _variant_allele_fraction(_result(variant))

    assert (value, status, sample) == (None, StageStatus.NOT_RUN.value, None)
    assert "no per-sample genotype columns" in reason


def test_csv_carries_both_columns_and_appends_them_last():
    """
    The value column alone cannot distinguish the three empty cases --
    `csv.DictWriter` writes them all as "". The status column shipping
    beside it is the fix, and its position is a documented contract.
    """
    from report.export_lims import _CSV_COLUMNS

    # Was `_CSV_COLUMNS[-2:] == [...]`. Restated 2026-09-11 when
    # `reference_assembly` was appended after the pair: what this pins is
    # that the pair ships adjacent and at the positions it was appended to
    # -- the message below -- not that nothing may ever follow it. A later
    # run-level column appended after it leaves those positions unchanged.
    vaf = _CSV_COLUMNS.index("variant_allele_fraction")
    assert _CSV_COLUMNS[vaf : vaf + 2] == ["variant_allele_fraction", "variant_allele_fraction_status"]
    assert vaf == _CSV_COLUMNS.index("run_caveats") + 1, "the pair follows run_caveats directly"
    assert _CSV_COLUMNS.index("run_caveats") < _CSV_COLUMNS.index("variant_allele_fraction"), (
        "appended after the previously-last column, so existing column positions are unchanged"
    )
    # csv writes None as an empty cell -- the premise the status column exists for.
    assert next(csv.reader([",".join(["" if v is None else str(v) for v in (None, "not_run")])])) == ["", "not_run"]
