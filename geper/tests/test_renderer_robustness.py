"""
Renderer robustness: a document shape the renderers cannot read must show as "Not stated" / "Not available", never raise.

Why this matters: on a linked run the PDFs are rendered AFTER the clinical service has approved the report
(`review/signoff.py::approve`), so a renderer exception leaves the report approved with no PDFs and no manifest
(docs/SIGNOFF_RECOVERY_DESIGN.md, section 13, fact 2). Two defects were reproduced there:

  * consent: `consent["clinical_reporting"]` / `consent["research"]` raised KeyError for a consent object without those
    keys (both PDF renderers).
  * ACMG points: `acmg.get("pathogenic_points", 0):g` raised TypeError when the key was present with value None
    (`clinical_report_builder` always sets the key), in the full PDF and the Markdown report.

Fallback semantics asserted here:
  * An unavailable consent permission reads "Not stated" (the existing label for a missing tri-state value) in the
    full PDF, the short PDF and the Markdown report alike. A string such as "no" is NOT read as truthy.
  * An unavailable ACMG point component reads "Not available" and is never replaced by 0.
  * Valid inputs render exactly as before (the helper's valid output is the old `:g`).

Nothing here asserts a clinical meaning: only what the renderers print for the data they are given.
"""

import copy
import os
import re
import tempfile

import pytest

from report import summary as summary_module
from report.clinical_report_builder import (
    _acmg_number_text,
    _consent_field,
    _consent_timestamp,
    acmg_net_points_parts,
)
from report.report_generator import ReportGenerator, _render_consent_line
from report.summary import generate_pdf
from report.summary_short import _classification_text, generate_short_pdf
from tests.test_acmg_net_points import _FIXTURE, _run_real

pypdf = pytest.importorskip("pypdf")


def _document(consent="absent", acmg_extra=None):
    acmg = {"classification": "Pathogenic", "triggered_criteria": []}
    acmg.update(acmg_extra or {})
    document = {
        "geper_version": "test",
        "generated_at": "2026-08-06T00:00:00+00:00",
        "input_vcf": "robustness.vcf",
        "assembly": "GRCh38",
        "vcf_samples": ["SAMPLE01"],
        "variant_count": 1,
        "variants": [
            {
                "variant": {"chrom": "1", "pos": 100, "ref": "A", "alt": "T"},
                "interpretation_result": {"gene_symbol": "BRCA1"},
                "candidate_interpretation": {
                    "executive_summary": "Test summary.",
                    "acmg_classification": acmg,
                    "confidence": {"pending": True},
                    "supporting_evidence": [],
                    "limitations": [],
                    "evidence_sources": [],
                    "conflict_resolution": {"severity": None},
                },
            }
        ],
    }
    if consent != "absent":
        document["patient_consent"] = consent
    return document


def _text(path):
    raw = "\n".join(page.extract_text() for page in pypdf.PdfReader(path).pages)
    return re.sub(r"\s+", " ", raw)


def _render(function, document):
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "report.pdf")
        function(document, out)
        return _text(out)


def _consent_values(text):
    """(clinical reporting, research) as printed, from 'Consent -- Clinical Reporting <v> Consent -- Research Use <v>'."""
    match = re.search(r"Consent -- Clinical Reporting (.+?) Consent -- Research Use (\S+(?: stated)?)", text)
    assert match, text
    return match.group(1).strip(), match.group(2).strip()


FULL, SHORT = generate_pdf, generate_short_pdf

# consent value -> (clinical reporting, research) as printed
CONSENT_CASES = {
    "valid_true_false": ({"clinical_reporting": True, "research": False, "timestamp": "2026-08-01"}, ("Yes", "No")),
    "valid_all_none": ({"clinical_reporting": None, "research": None, "timestamp": None}, ("Not stated", "Not stated")),
    "partial_clinical_missing": ({"research": True}, ("Not stated", "Yes")),
    "partial_research_missing": ({"clinical_reporting": False}, ("No", "Not stated")),
    "wrong_shape_other_keys": ({"consent_given": True, "purpose": "x"}, ("Not stated", "Not stated")),
    "wrong_shape_string_values": ({"clinical_reporting": "no", "research": "yes"}, ("Not stated", "Not stated")),
    "wrong_shape_number_values": ({"clinical_reporting": 1, "research": 0}, ("Not stated", "Not stated")),
    "wrong_shape_not_an_object": ("yes", ("Not stated", "Not stated")),
    "wrong_shape_list": (["clinical_reporting"], ("Not stated", "Not stated")),
}


@pytest.mark.parametrize("renderer", [FULL, SHORT], ids=["full_pdf", "short_pdf"])
@pytest.mark.parametrize("case", sorted(CONSENT_CASES))
def test_consent_shapes_render_without_raising_and_with_consistent_labels(renderer, case):
    consent, expected = CONSENT_CASES[case]
    text = _render(renderer, _document(consent))
    assert _consent_values(text) == expected


@pytest.mark.parametrize("renderer", [FULL, SHORT], ids=["full_pdf", "short_pdf"])
@pytest.mark.parametrize("consent", ["absent", None, {}], ids=["absent", "null", "empty"])
def test_absent_or_empty_consent_adds_no_consent_rows(renderer, consent):
    assert "Consent --" not in _render(renderer, _document(consent))


@pytest.mark.parametrize("renderer", [FULL, SHORT], ids=["full_pdf", "short_pdf"])
def test_the_recorded_timestamp_row_appears_only_for_a_real_string(renderer):
    assert "Consent Recorded 2026-08-01" in _render(renderer, _document(CONSENT_CASES["valid_true_false"][0]))
    for bad in (12345, ["x"], "", "   ", None):
        assert "Consent Recorded" not in _render(renderer, _document({"clinical_reporting": True, "timestamp": bad}))


@pytest.mark.parametrize("case", sorted(CONSENT_CASES))
def test_the_markdown_report_uses_the_same_labels(case):
    consent, (clinical, research) = CONSENT_CASES[case]
    line = _render_consent_line({"patient_consent": consent})
    assert f"Clinical reporting: {clinical}" in line
    assert f"Research use: {research}" in line


def test_the_markdown_report_has_no_consent_line_without_a_consent_object():
    for consent in (None, {}):
        assert _render_consent_line({"patient_consent": consent}) is None
    assert _render_consent_line({}) is None


def test_the_consent_helpers_read_defensively():
    assert _consent_field({"research": True}, "research") is True
    assert _consent_field({"research": False}, "research") is False
    for bad in ({}, {"research": "no"}, {"research": 1}, {"research": None}, "x", None, ["research"]):
        assert _consent_field(bad, "research") is None
    assert _consent_timestamp({"timestamp": "2026-08-01"}) == "2026-08-01"
    for bad in ({}, {"timestamp": ""}, {"timestamp": " "}, {"timestamp": 5}, "x", None):
        assert _consent_timestamp(bad) is None


# ── ACMG net points and their components ──
#
# Wording (human ruling): with both components usable the line is the earlier wording, unchanged; with either
# unavailable it is "Net points: <n> (components not available)" and no subtraction is shown; an unusable net_points
# reads "Not available" with no components. Absent / None net_points shows no line (the documented "no tally ran" case).

POINTS = {"net_points": 4, "pathogenic_points": 5, "benign_points": 1, "net_points_band": "Pathogenic band"}
ARBITRARY_OBJECT = object()  # neither a number nor a container


def _real_variant(acmg_extra):
    """A real `build_variant_result` output (the path the pipeline takes), with the ACMG point fields replaced."""
    label = next(lbl for lbl, e in _FIXTURE.items() if e["expected"]["acmg_classification"] == "Pathogenic")
    _, variant_result = _run_real(label)
    variant_result = copy.deepcopy(variant_result)
    acmg = variant_result["candidate_interpretation"]["acmg_classification"]
    for key in ("net_points", "pathogenic_points", "benign_points", "net_points_band"):
        acmg.pop(key, None)
    acmg.update(acmg_extra)
    return variant_result


def _squash(text):
    return re.sub(r"\s+", " ", text)


def _variant_text(acmg_extra):
    flow = summary_module._build_variant_section(1, _real_variant(acmg_extra), summary_module._build_stylesheet())
    return _squash("\n".join(getattr(item, "text", "") for item in flow if hasattr(item, "text")))


def _markdown_text(acmg_extra):
    candidate = _real_variant(acmg_extra)["candidate_interpretation"]
    return "\n".join(ReportGenerator()._render_clinical_report(candidate, {}))


def _summary_cell(acmg_extra):
    variant = _real_variant(acmg_extra)
    table = summary_module._build_clinician_summary_table([variant], summary_module._build_stylesheet())
    return _squash(getattr(table._cellvalues[1][2], "text", ""))


def _short_cell(acmg_extra):
    return _squash(_classification_text(_real_variant(acmg_extra)["candidate_interpretation"]))


def _line(text):
    """The point total and optional parenthetical after 'Net points...:', or None if the text carries no such line.
    Stops before any threshold-band text, and before whatever paragraph follows in the squashed full-section text."""
    match = re.search(
        r"Net points(?: \(Tavtigian 2018\))?:(?:\*\*|</b>)? ((?:Not available|-?\d[\d.e+-]*)(?: \([^)]*\))?)", text
    )
    return match.group(1).strip() if match else None


def _cell(text):
    """What follows 'net ' in a classification cell, or None."""
    match = re.search(r"<br ?/?>net (.+)$", text)
    return match.group(1).strip() if match else None


LINE_RENDERERS = {"full_pdf_section": _variant_text, "markdown": _markdown_text}
CELL_RENDERERS = {"clinician_summary_cell": _summary_cell, "short_pdf_cell": _short_cell}

#            id: (acmg fields, line expected in the section/markdown, value expected in the cells)
COMPONENT_CASES = {
    "both_available": (dict(POINTS), "4 (pathogenic 5 − benign 1)", "4"),
    "pathogenic_unavailable": (dict(POINTS, pathogenic_points=None), "4 (components not available)", "4"),
    "benign_unavailable": (dict(POINTS, benign_points=None), "4 (components not available)", "4"),
    "both_unavailable": (dict(POINTS, pathogenic_points=None, benign_points=None), "4 (components not available)", "4"),
    "component_keys_absent": (
        {"net_points": 4, "net_points_band": "Pathogenic band"},
        "4 (components not available)",
        "4",
    ),
    "component_wrong_types": (
        dict(POINTS, pathogenic_points="5", benign_points=True),
        "4 (components not available)",
        "4",
    ),
    "genuine_zero_components": (
        dict(POINTS, net_points=0, pathogenic_points=0, benign_points=0.0),
        "0 (pathogenic 0 − benign 0)",
        "0",
    ),
}


@pytest.mark.parametrize("case", sorted(COMPONENT_CASES))
@pytest.mark.parametrize("renderer", sorted(LINE_RENDERERS))
def test_the_net_points_line_with_available_or_unavailable_components(renderer, case):
    acmg, expected, _ = COMPONENT_CASES[case]
    line = _line(LINE_RENDERERS[renderer](acmg))
    assert line == expected
    if "not available" in expected:
        assert "−" not in line and "pathogenic" not in line  # no subtraction is shown, and nothing is shown as 0


@pytest.mark.parametrize("case", sorted(COMPONENT_CASES))
@pytest.mark.parametrize("renderer", sorted(CELL_RENDERERS))
def test_the_classification_cells_show_the_net_total_whatever_the_components(renderer, case):
    acmg, _, expected = COMPONENT_CASES[case]
    assert _cell(CELL_RENDERERS[renderer](acmg)) == expected


# net_points itself: value -> (line in section/markdown, value in the cells); None means "no Net points text at all"
NET_CASES = {
    "valid_positive": (4, "4 (pathogenic 5 − benign 1)", "4"),
    "valid_negative": (-2, "-2 (pathogenic 5 − benign 1)", "-2"),
    "valid_fraction": (4.5, "4.5 (pathogenic 5 − benign 1)", "4.5"),
    "zero_int": (0, "0 (pathogenic 5 − benign 1)", "0"),
    "zero_float": (0.0, "0 (pathogenic 5 − benign 1)", "0"),
    "missing": ("<missing>", None, None),
    "none": (None, None, None),
    "string": ("x", "Not available", "Not available"),
    "numeric_string": ("4", "Not available", "Not available"),
    "list": ([4], "Not available", "Not available"),
    "empty_list": ([], "Not available", "Not available"),
    "dict": ({"points": 4}, "Not available", "Not available"),
    "object": (ARBITRARY_OBJECT, "Not available", "Not available"),
    "bool_true": (True, "Not available", "Not available"),
    "bool_false": (False, "Not available", "Not available"),
    "nan": (float("nan"), "Not available", "Not available"),
    "infinity": (float("inf"), "Not available", "Not available"),
}


def _net_acmg(value):
    acmg = {k: v for k, v in POINTS.items() if k != "net_points"}
    if not (isinstance(value, str) and value == "<missing>"):
        acmg["net_points"] = value
    return acmg


@pytest.mark.parametrize("case", sorted(NET_CASES))
@pytest.mark.parametrize("renderer", sorted(LINE_RENDERERS))
def test_net_points_value_in_the_full_section_and_markdown(renderer, case):
    value, expected_line, _ = NET_CASES[case]
    line = _line(LINE_RENDERERS[renderer](_net_acmg(value)))
    assert line == expected_line
    if expected_line == "Not available":
        assert "−" not in line  # an unusable total carries no components either


@pytest.mark.parametrize("case", sorted(NET_CASES))
@pytest.mark.parametrize("renderer", sorted(CELL_RENDERERS))
def test_net_points_value_in_the_classification_cells(renderer, case):
    value, _, expected_cell = NET_CASES[case]
    assert _cell(CELL_RENDERERS[renderer](_net_acmg(value))) == expected_cell


@pytest.mark.parametrize("case", sorted(NET_CASES))
def test_all_four_places_agree_on_every_net_points_value(case):
    acmg = _net_acmg(NET_CASES[case][0])
    section, markdown = _line(_variant_text(acmg)), _line(_markdown_text(acmg))
    summary, short = _cell(_summary_cell(acmg)), _cell(_short_cell(acmg))
    assert (section is None) == (markdown is None) == (summary is None) == (short is None)
    if section is not None:
        assert section == markdown
        assert summary == short == section.split(" (")[0]


def _old_section_text(net, pathogenic, benign, band):
    """The pre-change full-PDF string, for numeric inputs the old code could format."""
    band_text = f" -- threshold band: {band}" if band else ""
    return f"<b>Net points:</b> {net:g} (pathogenic {pathogenic:g} − benign {benign:g}){band_text}"


def _old_markdown_text(net, pathogenic, benign, band):
    band_text = f" -- threshold band: {band}" if band else ""
    return f"**Net points (Tavtigian 2018):** {net:g} (pathogenic {pathogenic:g} − benign {benign:g}){band_text}"


VALID_NUMERIC = [(4, 5, 1), (-2, 0, 2), (0, 0, 0.0), (4.5, 5.5, 1), (11, 12, 1), (1e16, 1e16, 0), (0.1 + 0.2, 1, 1)]


@pytest.mark.parametrize("net, pathogenic, benign", VALID_NUMERIC)
@pytest.mark.parametrize("band", ["Pathogenic band", None])
def test_valid_numeric_inputs_render_exactly_as_the_old_format_strings(net, pathogenic, benign, band):
    acmg = {"net_points": net, "pathogenic_points": pathogenic, "benign_points": benign, "net_points_band": band}
    net_text, components, with_band = acmg_net_points_parts(acmg)
    assert with_band is True
    band_text = f" -- threshold band: {band}" if band else ""
    assert f"<b>Net points:</b> {net_text} ({components}){band_text}" == _old_section_text(
        net, pathogenic, benign, band
    )
    assert f"**Net points (Tavtigian 2018):** {net_text} ({components}){band_text}" == _old_markdown_text(
        net, pathogenic, benign, band
    )
    assert f"net {net_text}" == f"net {net:g}"


def test_the_acmg_helpers_never_coerce_or_accept_a_bool():
    assert [_acmg_number_text(v) for v in (0, 0.0, -1.5, 5)] == ["0", "0", "-1.5", "5"]
    for unusable in (None, "5", True, False, [], {}, [5], ARBITRARY_OBJECT, float("nan"), float("-inf")):
        assert _acmg_number_text(unusable) is None
    assert acmg_net_points_parts({}) is None
    assert acmg_net_points_parts({"net_points": None, "pathogenic_points": 1, "benign_points": 1}) is None
    assert acmg_net_points_parts({"net_points": "3"}) == ("Not available", None, False)
    assert acmg_net_points_parts({"net_points": 3}) == ("3", "components not available", True)


@pytest.mark.parametrize("renderer", [FULL, SHORT], ids=["full_pdf", "short_pdf"])
@pytest.mark.parametrize("case", sorted(NET_CASES))
def test_whole_documents_render_to_both_pdfs_for_every_net_points_value(renderer, case):
    text = _render(renderer, _document(acmg_extra=_net_acmg(NET_CASES[case][0])))
    expected_cell = NET_CASES[case][2]
    if expected_cell is None:
        assert not re.search(r"\bnet (Not available|-?\d)", text)
    else:
        assert f"net {expected_cell}" in text


@pytest.mark.parametrize("renderer", [FULL, SHORT], ids=["full_pdf", "short_pdf"])
@pytest.mark.parametrize("case", sorted(COMPONENT_CASES))
def test_whole_documents_render_to_both_pdfs_for_every_component_shape(renderer, case):
    acmg, _, expected_cell = COMPONENT_CASES[case]
    assert f"net {expected_cell}" in _render(renderer, _document(acmg_extra=acmg))


# ── absent / None net_points, and the threshold-band text ──
#
# Decisions: an absent or None `net_points` adds NO Net points line (it can mean a tally that was never performed:
# BA1's stand-alone-benign short-circuit, or an unclassified variant), and invalid values (strings, lists, objects, bools,
# NaN, infinity) are never coerced to numbers. The band label (`acmg_net_points_band_label`) is a function of the
# CLASSIFICATION string alone, never of the point total, so it is carried through unchanged.


@pytest.mark.parametrize("value", ["<missing>", None], ids=["absent", "none"])
def test_absent_or_none_net_points_adds_no_net_points_text_anywhere_even_with_a_band(value):
    acmg = _net_acmg(value)
    assert acmg.get("net_points_band")  # the band is present in the input; it must not surface on its own
    for text in (_variant_text(acmg), _markdown_text(acmg)):
        assert "Net points" not in text and "threshold band" not in text
    assert _cell(_summary_cell(acmg)) is None and _cell(_short_cell(acmg)) is None


@pytest.mark.parametrize("renderer", sorted(LINE_RENDERERS))
def test_the_band_follows_a_valid_total_and_is_omitted_when_the_input_has_none(renderer):
    with_band = LINE_RENDERERS[renderer](dict(POINTS))
    assert "(pathogenic 5 − benign 1) -- threshold band: Pathogenic band" in with_band
    without_band = LINE_RENDERERS[renderer](dict(POINTS, net_points_band=None))
    # (the full-section text is one squashed run of paragraphs, so compare the start of the line, not its whole tail)
    assert "threshold band" not in without_band
    assert _line(without_band).startswith("4 (pathogenic 5 − benign 1)")


@pytest.mark.parametrize("renderer", sorted(LINE_RENDERERS))
def test_the_band_text_does_not_depend_on_the_total(renderer):
    """The band is a label for the classification: the same text for different valid totals, and the renderers do not
    compute it from the total."""
    for net in (4, 0, -3, 11.5):
        text = LINE_RENDERERS[renderer](dict(POINTS, net_points=net))
        assert f"-- threshold band: {POINTS['net_points_band']}" in text


# ── Option B (human ruling): the three net_points cases, with a band supplied in every one ──
#   1. valid total          -> "Net points: <n> (...) -- threshold band: <band>", exactly the earlier wording
#   2. invalid total        -> "Net points: Not available" and NO threshold-band text on that line
#   3. absent / None total  -> no Net points text at all (a tally that was never performed)
# The classification label elsewhere in the report is not touched by any of them.

INVALID_NET_CASES = sorted(k for k, v in NET_CASES.items() if v[1] == "Not available")


@pytest.mark.parametrize("renderer", sorted(LINE_RENDERERS))
@pytest.mark.parametrize("case", INVALID_NET_CASES)
def test_option_b_invalid_total_reads_not_available_with_no_band_text(renderer, case):
    acmg = _net_acmg(NET_CASES[case][0])
    assert acmg["net_points_band"] == "Pathogenic band"  # a band IS supplied; it must not appear
    text = LINE_RENDERERS[renderer](acmg)
    assert _line(text).startswith("Not available")
    assert "threshold band" not in text and "Pathogenic band" not in text
    assert not re.search(r"\(pathogenic \d", text) and "components not available" not in text


@pytest.mark.parametrize("renderer", sorted(LINE_RENDERERS))
@pytest.mark.parametrize("case", ["valid_positive", "valid_negative", "valid_fraction", "zero_int", "zero_float"])
def test_option_b_valid_total_keeps_the_band_wording(renderer, case):
    text = LINE_RENDERERS[renderer](_net_acmg(NET_CASES[case][0]))
    assert text.count("-- threshold band: Pathogenic band") == 1


@pytest.mark.parametrize("renderer", sorted(LINE_RENDERERS))
@pytest.mark.parametrize("case", ["both_unavailable", "pathogenic_unavailable", "component_wrong_types"])
def test_option_b_a_valid_total_with_unavailable_components_keeps_the_band(renderer, case):
    text = LINE_RENDERERS[renderer](COMPONENT_CASES[case][0])
    assert "4 (components not available) -- threshold band: Pathogenic band" in text


@pytest.mark.parametrize("case", ["missing", "none"])
@pytest.mark.parametrize("renderer", sorted(LINE_RENDERERS))
def test_option_b_absent_or_none_total_has_no_net_points_text(renderer, case):
    text = LINE_RENDERERS[renderer](_net_acmg(NET_CASES[case][0]))
    assert "Net points" not in text and "threshold band" not in text


@pytest.mark.parametrize("case", ["valid_positive", "string", "none"])
def test_option_b_the_classification_label_is_unchanged_in_every_case(case):
    acmg = _net_acmg(NET_CASES[case][0])
    baseline = _variant_text(dict(POINTS))
    assert "Pathogenic" in _variant_text(acmg)
    assert "Pathogenic" in _summary_cell(acmg) and "Pathogenic" in _short_cell(acmg)
    # the classification line is the same whatever happens to the points line
    assert _variant_text(acmg).startswith(baseline.split("Net points")[0])
