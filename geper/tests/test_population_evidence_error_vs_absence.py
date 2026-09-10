"""
`report/clinical_report_builder.py::_population_evidence` and
`::_indian_population_frequency` -- the same "a failed lookup must not
render as a confirmed absence/never-attempted lookup" property this
floor has now fixed five separate times (UniProt/InterPro/ClinVar/
ClinGen/AlphaFold in `test_report_consistency.py`, BLAST in
`_run_blast_stage`, RNA-FM/ESM-2/AlphaMissense/MMSplice's own
POSITIVE-POLARITY TRUTHINESS fixes), found a sixth and seventh time
here.

THE DEFECT (pre-2026-09-11 fix). Both producer functions computed
`queried`/`gnomad_sas_queried` as `not (skipped or error is not None)`
-- so an error genuinely collapsed `queried` to `False`, exactly like a
deliberate skip -- but the error TEXT itself was never put anywhere in
the output dict, and `skip_reason` is only populated `if skipped`
(never on an error). The renderer (`report/report_generator.py`'s
"## 10. Population Evidence" and the gnomAD-SAS half of "## 11. Indian
Population Frequency") therefore saw `queried=False, skip_reason=None`
identically for "gnomAD/dbSNP was never asked" and "gnomAD/dbSNP was
asked and the lookup broke", and printed the same generic "lookup
unavailable for this variant." for both -- in the CLINICIAN-FACING
summary section, while the Annotation Detail audit trail
(`_render_gnomad`/`_render_dbsnp`) already distinguished the two
correctly for the exact same underlying stage result.

Sibling producer functions feeding the SAME 17-section clinical report
(`_protein_knowledge`, `_structural_knowledge`, `_clinical_evidence`,
`_sequence_context`) were all deliberately fixed for this distinction
already -- `_clinical_evidence`'s own docstring says so in words. This
file is the sixth/seventh instance of the identical shape, in the two
producer functions that sat right next to the fixed ones, untouched.

UNTESTED, NOT JUST WRONG, before this file: grepping `tests/` for
`population_evidence`/`gnomad_sas_queried` combined with error/fail
returned zero hits.

MITIGATED-NOT-EXCUSED, stated here so a future reader does not
conclude the bug was invisible: `pipeline/orchestrator.py`'s
`_run_gnomad_stage`/`_run_dbsnp_stage` already push the same failure
to the per-variant `errors` list, which `_render_variant_section`
renders unconditionally under "### Stage Warnings / Errors" when
non-empty. A technically-present disclosure elsewhere does not make a
wrong statement in the primary reading path correct -- this file tests
the primary reading path, not whether the failure is visible SOMEWHERE.

THE CONTROLS MATTER AS MUCH AS THE DANGEROUS CASE, same discipline as
`test_gnomad_lookup_failure_not_absent.py`: a genuinely skipped lookup
and a genuinely completed absence must still render exactly as they do
today. Fixing the error case must not blur either of those.
"""

import unittest

from report.clinical_report_builder import _indian_population_frequency, _population_evidence
from report.report_generator import ReportGenerator

# A lookup that genuinely FAILED.
_GNOMAD_ERROR = {"skipped": False, "found": False, "error": "gnomAD API timeout after 3 retries"}
_DBSNP_ERROR = {"skipped": False, "found": False, "error": "Ensembl dbSNP endpoint returned 503"}

# CONTROL: a lookup that was never attempted (deliberate skip) --
# must keep rendering its own `skip_reason`, not the error text.
_GNOMAD_SKIPPED = {
    "skipped": True,
    "reason": "gnomAD integration disabled via GEPER_ENABLE_GNOMAD=false",
    "found": False,
}

# CONTROL: a lookup that genuinely completed and found the variant.
_GNOMAD_FOUND = {"skipped": False, "found": True, "error": None, "global_af": 0.02}

# CONTROL: a lookup that genuinely completed and found nothing --
# a real PM2-relevant confirmed absence, must still read that way.
_GNOMAD_CONFIRMED_ABSENT = {"skipped": False, "found": False, "error": None}


class TestPopulationEvidenceThreadsTheRealError(unittest.TestCase):
    """THE DANGEROUS DIRECTION: the producer must expose the error, not
    silently fold it into 'not queried, no reason given'."""

    def test_gnomad_error_is_present_and_not_swallowed(self):
        section = _population_evidence({"gnomad": _GNOMAD_ERROR, "dbsnp": {}})
        self.assertFalse(section["gnomad"]["queried"])
        self.assertIsNone(section["gnomad"]["skip_reason"])
        self.assertEqual(section["gnomad"]["error"], "gnomAD API timeout after 3 retries")

    def test_dbsnp_error_is_present_and_not_swallowed(self):
        section = _population_evidence({"gnomad": {}, "dbsnp": _DBSNP_ERROR})
        self.assertFalse(section["dbsnp"]["queried"])
        self.assertIsNone(section["dbsnp"]["skip_reason"])
        self.assertEqual(section["dbsnp"]["error"], "Ensembl dbSNP endpoint returned 503")

    def test_gnomad_sas_error_is_present_and_not_swallowed(self):
        section = _indian_population_frequency({"gnomad": _GNOMAD_ERROR}, None)
        self.assertFalse(section["gnomad_sas_queried"])
        self.assertEqual(section["gnomad_sas_error"], "gnomAD API timeout after 3 retries")

    def test_markdown_section_10_states_lookup_failed_for_gnomad_error(self):
        pop = _population_evidence({"gnomad": _GNOMAD_ERROR, "dbsnp": {}})
        clinical_report = _minimal_clinical_report(population_evidence=pop)
        text = "\n".join(ReportGenerator._render_clinical_report(clinical_report, {}))
        self.assertIn("lookup failed (external service issue: gnomAD API timeout after 3 retries)", text)
        self.assertNotIn("gnomAD:** lookup unavailable for this variant.", text)

    def test_markdown_section_10_states_lookup_failed_for_dbsnp_error(self):
        pop = _population_evidence({"gnomad": {}, "dbsnp": _DBSNP_ERROR})
        clinical_report = _minimal_clinical_report(population_evidence=pop)
        text = "\n".join(ReportGenerator._render_clinical_report(clinical_report, {}))
        self.assertIn("lookup failed (external service issue: Ensembl dbSNP endpoint returned 503)", text)
        self.assertNotIn("dbSNP:** lookup unavailable for this variant.", text)

    def test_markdown_section_11_states_lookup_failed_for_gnomad_sas_error(self):
        ipf = _indian_population_frequency({"gnomad": _GNOMAD_ERROR}, None)
        clinical_report = _minimal_clinical_report(indian_population_frequency=ipf)
        text = "\n".join(ReportGenerator._render_clinical_report(clinical_report, {}))
        self.assertIn(
            "gnomAD (South Asian, SAS):** _lookup failed (external service issue: gnomAD API timeout after 3 retries)",
            text,
        )
        self.assertNotIn("gnomAD (South Asian, SAS):** lookup unavailable for this variant.", text)


class TestSkippedAndAbsentControlsUnchanged(unittest.TestCase):
    """CONTROLS: a genuinely disabled lookup and a genuinely completed
    absence must still render exactly as they did before this fix --
    turning a completed negative into a failure is the same defect
    pointing the other way."""

    def test_skipped_gnomad_still_renders_its_own_skip_reason(self):
        pop = _population_evidence({"gnomad": _GNOMAD_SKIPPED, "dbsnp": {}})
        self.assertIsNone(pop["gnomad"]["error"])
        self.assertEqual(pop["gnomad"]["skip_reason"], "gnomAD integration disabled via GEPER_ENABLE_GNOMAD=false")
        clinical_report = _minimal_clinical_report(population_evidence=pop)
        text = "\n".join(ReportGenerator._render_clinical_report(clinical_report, {}))
        self.assertIn("gnomAD integration disabled via GEPER_ENABLE_GNOMAD=false", text)
        self.assertNotIn("lookup failed", text)

    def test_confirmed_absent_gnomad_still_renders_not_found(self):
        pop = _population_evidence({"gnomad": _GNOMAD_CONFIRMED_ABSENT, "dbsnp": {}})
        self.assertIsNone(pop["gnomad"]["error"])
        self.assertTrue(pop["gnomad"]["queried"])
        clinical_report = _minimal_clinical_report(population_evidence=pop)
        text = "\n".join(ReportGenerator._render_clinical_report(clinical_report, {}))
        self.assertIn("- **gnomAD:** variant not found (absent from gnomAD)", text)
        self.assertNotIn("- **gnomAD:** lookup failed", text)
        self.assertNotIn("- **gnomAD:** lookup unavailable", text)

    def test_confirmed_found_gnomad_still_renders_its_af(self):
        pop = _population_evidence({"gnomad": _GNOMAD_FOUND, "dbsnp": {}})
        clinical_report = _minimal_clinical_report(population_evidence=pop)
        text = "\n".join(ReportGenerator._render_clinical_report(clinical_report, {}))
        self.assertIn("- **gnomAD:** found, AF=0.02", text)
        self.assertNotIn("- **gnomAD:** lookup failed", text)
        self.assertNotIn("- **gnomAD:** lookup unavailable", text)


def _minimal_clinical_report(**overrides):
    """The minimal `build_clinical_report()`-shaped dict
    `_render_clinical_report` needs to reach section 10/11 without
    crashing on an earlier section -- every OTHER section is given its
    honest-empty default, matching what `build_clinical_report` itself
    would produce for a variant with no other evidence."""
    base = {
        "executive_summary": "",
        "acmg_classification": {},
        "confidence": {"pending": True},
        "priority": {"pending": True},
        "supporting_evidence": [],
        "conflicting_evidence": [],
        "conflict_resolution": {},
        "ai_consensus": {},
        "protein_knowledge": {},
        "structural_knowledge": {},
        "population_evidence": {
            "gnomad": {"queried": False, "found": False, "skip_reason": None, "error": None},
            "dbsnp": {"queried": False, "found": False, "skip_reason": None, "error": None},
        },
        "indian_population_frequency": {},
        "clinical_evidence": {},
        "sequence_context": {"blast": {}},
        "recommendations": [],
        "limitations": [],
        "references": [],
        "evidence_sources": [],
        "explainability": None,
    }
    base.update(overrides)
    return base


if __name__ == "__main__":
    unittest.main()
