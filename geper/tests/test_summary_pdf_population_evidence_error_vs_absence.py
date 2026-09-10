"""
`report/summary.py::_build_indian_population_frequency_flowables` --
the PDF-renderer sibling of `test_population_evidence_error_vs_absence.py`.

THE DEFECT (pre-2026-09-11 fix, found on a card auditing all four
sites the population-evidence/AI-splicing-ensemble error-vs-absence
fixes touched, checking whether the two PDF renderers
(`summary.py`/`summary_short.py`) inherited them). The producer,
`_indian_population_frequency`, already carries `gnomad_sas_error`
(added by the Markdown fix in `test_population_evidence_error_vs_absence.py`) --
but this PDF renderer's gnomAD-SAS bullet never read it, only
`gnomad_af_sas`/`gnomad_sas_queried`. A genuine gnomAD lookup failure
and a genuine never-queried variant both fell through to the same
final `else` and printed the byte-identical
"gnomAD (South Asian, SAS): not queried for this variant." line --
proven by rendering both through the real function and comparing
`.getPlainText()` output, not by reading the source.

THE NEAR MISS THIS FILE IS A CONTROL AGAINST: the immediately adjacent
sub-block in the same parent function, `_build_1000_genomes_sas_flowables`
(reads the sibling `sas_error` field), was ALREADY correct -- a shared
producer inheriting a fix is evidence about a renderer's neighbouring
branches, not assurance about all of them. This file exists so a
future change to this function cannot silently regress the gnomAD-SAS
branch back to the collapsed state while `_build_1000_genomes_sas_flowables`
keeps passing.

THE CONTROLS MATTER AS MUCH AS THE DANGEROUS CASE: a genuinely
never-queried variant must still say "not queried"; a genuine
completed absence must still say "not found in this source"; a
genuine hit must still print its AF. Fixing the error case must not
blur any of the three.
"""

import unittest

from report.summary import _build_indian_population_frequency_flowables, _build_stylesheet

_STYLES = _build_stylesheet()


def _render_gnomad_sas_bullet(ipf):
    """Render the real function and return just the gnomAD-SAS bullet's
    plain text (the second flowable: index 0 is the section's leading
    Spacer, index 1 is its "Indian Population Frequency:" heading)."""
    flow = _build_indian_population_frequency_flowables({"indian_population_frequency": ipf}, _STYLES)
    return flow[2].getPlainText()


# A lookup that genuinely FAILED.
_GNOMAD_SAS_ERROR = "gnomAD API timeout after 3 retries"


class TestPdfGnomadSasBulletThreadsTheRealError(unittest.TestCase):
    """THE DANGEROUS DIRECTION: a genuine failure must not render as
    'not queried for this variant.'"""

    def test_error_case_states_lookup_failed(self):
        text = _render_gnomad_sas_bullet(
            {"gnomad_af_sas": None, "gnomad_sas_queried": False, "gnomad_sas_error": _GNOMAD_SAS_ERROR}
        )
        self.assertIn(f"Lookup failed (external service issue: {_GNOMAD_SAS_ERROR})", text)
        self.assertNotIn("not queried for this variant", text)

    def test_error_case_is_not_byte_identical_to_never_queried(self):
        error_text = _render_gnomad_sas_bullet(
            {"gnomad_af_sas": None, "gnomad_sas_queried": False, "gnomad_sas_error": _GNOMAD_SAS_ERROR}
        )
        never_queried_text = _render_gnomad_sas_bullet(
            {"gnomad_af_sas": None, "gnomad_sas_queried": False, "gnomad_sas_error": None}
        )
        self.assertNotEqual(error_text, never_queried_text)


class TestPdfGnomadSasBulletControlsUnchanged(unittest.TestCase):
    """CONTROLS: the three non-error cases must still render exactly
    as they did before this fix."""

    def test_never_queried_still_says_not_queried(self):
        text = _render_gnomad_sas_bullet({"gnomad_af_sas": None, "gnomad_sas_queried": False, "gnomad_sas_error": None})
        self.assertIn("not queried for this variant", text)
        self.assertNotIn("Lookup failed", text)

    def test_confirmed_absent_still_says_not_found(self):
        text = _render_gnomad_sas_bullet({"gnomad_af_sas": None, "gnomad_sas_queried": True, "gnomad_sas_error": None})
        self.assertIn("variant not found in this source", text)
        self.assertNotIn("Lookup failed", text)

    def test_confirmed_found_still_prints_its_af(self):
        text = _render_gnomad_sas_bullet(
            {"gnomad_af_sas": 0.0012, "gnomad_sas_queried": True, "gnomad_sas_error": None}
        )
        self.assertIn("AF = 1.20e-03", text)
        self.assertNotIn("Lookup failed", text)


if __name__ == "__main__":
    unittest.main()
