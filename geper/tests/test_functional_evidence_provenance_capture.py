"""
Tests for `pipeline/orchestrator.py::GeperPipeline._capture_stage_provenance`'s
functional-evidence branch -- the MaveDB provenance masking fix (report
review round 6).

Before this fix, "Functional evidence (MaveDB)" / "Functional evidence
(ClinGen ERepo)" provenance only recorded TIMESTAMP_ONLY when that
source's own gene index produced the variant's PS3/BS3 record
(`functional_evidence_result["source"] == "mavedb"` / `"clingen_erepo"`).
When a source was genuinely queried this run but simply had nothing for
this exact variant (`source == "none"`), no provenance record fired at
all, so it stayed at its seeded NOT_CONSULTED default -- indistinguishable
from a source that was never touched. The fix reads
`functional_evidence_result["consulted_sources"]` (see
`pipeline/functional_evidence/models.py::FunctionalEvidenceResult`)
instead, which `lookup.py` now populates whenever a source's gene index
fetch actually succeeds, regardless of whether it matched this variant.

Exercises `_capture_stage_provenance` directly against a bare
`GeperPipeline` instance (the same `__new__`-bypass pattern
`tests/test_ps3_bs3.py::TestOrchestratorFunctionalEvidenceStage` uses) --
no pipeline run, no model weights.
"""

import importlib
import unittest

import _fake_heavy_deps

_fake_heavy_deps.install()

from pipeline.provenance import RunProvenanceCollector, VersionStatus  # noqa: E402

orchestrator_module = importlib.import_module("pipeline.orchestrator")


def _bare_pipeline():
    pipeline = orchestrator_module.GeperPipeline.__new__(orchestrator_module.GeperPipeline)
    pipeline.provenance = RunProvenanceCollector()
    return pipeline


def _capture(pipeline, functional_evidence_result):
    pipeline._capture_stage_provenance(
        clinvar_result={},
        dbsnp_result={},
        gnomad_result=None,
        uniprot_result=None,
        interpro_result=None,
        alphafold_result=None,
        functional_evidence_result=functional_evidence_result,
    )


class TestFunctionalEvidenceProvenanceThreeStates(unittest.TestCase):
    def test_never_consulted_stays_not_consulted(self):
        # No functional-evidence stage ran at all this variant (e.g. no
        # gene symbol resolved) -- both sources must stay seeded
        # NOT_CONSULTED.
        pipeline = _bare_pipeline()
        _capture(pipeline, None)
        self.assertEqual(
            pipeline.provenance.get("Functional evidence (ClinGen ERepo)").status, VersionStatus.NOT_CONSULTED
        )
        self.assertEqual(pipeline.provenance.get("Functional evidence (MaveDB)").status, VersionStatus.NOT_CONSULTED)

    def test_erepo_short_circuit_leaves_mavedb_not_consulted(self):
        # ERepo hit this variant; MaveDB was never queried (short-
        # circuited in lookup.py) -- MaveDB must stay NOT_CONSULTED,
        # ERepo must be recorded.
        pipeline = _bare_pipeline()
        _capture(
            pipeline,
            {
                "found": True,
                "source": "clingen_erepo",
                "records": [{"call": "PS3"}],
                "consulted_sources": ["ClinGen ERepo"],
            },
        )
        self.assertEqual(
            pipeline.provenance.get("Functional evidence (ClinGen ERepo)").status, VersionStatus.TIMESTAMP_ONLY
        )
        self.assertEqual(pipeline.provenance.get("Functional evidence (MaveDB)").status, VersionStatus.NOT_CONSULTED)

    def test_both_queried_and_empty_records_both_as_timestamp_only(self):
        # The exact bug scenario: source == "none", found == False, but
        # both gene indexes were genuinely fetched this variant. Before
        # the fix, this recorded nothing for either source.
        pipeline = _bare_pipeline()
        _capture(
            pipeline,
            {
                "found": False,
                "source": "none",
                "records": [],
                "consulted_sources": ["ClinGen ERepo", "MaveDB"],
            },
        )
        self.assertEqual(
            pipeline.provenance.get("Functional evidence (ClinGen ERepo)").status, VersionStatus.TIMESTAMP_ONLY
        )
        self.assertEqual(pipeline.provenance.get("Functional evidence (MaveDB)").status, VersionStatus.TIMESTAMP_ONLY)

    def test_mavedb_match_also_credits_erepo_as_consulted(self):
        # ERepo was queried and empty on the way to the MaveDB fallback
        # match -- both must be recorded, not just the one that hit.
        pipeline = _bare_pipeline()
        _capture(
            pipeline,
            {
                "found": True,
                "source": "mavedb",
                "records": [{"call": "BS3"}],
                "consulted_sources": ["ClinGen ERepo", "MaveDB"],
            },
        )
        self.assertEqual(
            pipeline.provenance.get("Functional evidence (ClinGen ERepo)").status, VersionStatus.TIMESTAMP_ONLY
        )
        self.assertEqual(pipeline.provenance.get("Functional evidence (MaveDB)").status, VersionStatus.TIMESTAMP_ONLY)

    def test_error_does_not_downgrade_an_already_consulted_source(self):
        # A later variant's outright query failure (UNKNOWN, priority 1)
        # must never downgrade a source this run already proved was
        # genuinely consulted (TIMESTAMP_ONLY, priority 2) -- guarded by
        # RunProvenanceCollector.record's own priority ordering, exercised
        # here across two successive calls the way two variants would.
        pipeline = _bare_pipeline()
        _capture(
            pipeline,
            {
                "found": False,
                "source": "none",
                "records": [],
                "consulted_sources": ["ClinGen ERepo", "MaveDB"],
            },
        )
        _capture(
            pipeline,
            {
                "found": False,
                "source": "none",
                "records": [],
                "consulted_sources": [],
                "error": "MaveDB: connection reset",
            },
        )
        self.assertEqual(
            pipeline.provenance.get("Functional evidence (ClinGen ERepo)").status, VersionStatus.TIMESTAMP_ONLY
        )
        self.assertEqual(pipeline.provenance.get("Functional evidence (MaveDB)").status, VersionStatus.TIMESTAMP_ONLY)

    def test_error_on_first_variant_is_upgraded_by_later_success(self):
        # The reverse order: an early failure (UNKNOWN) must be
        # upgradeable by a later variant's genuine consultation
        # (TIMESTAMP_ONLY) -- an upgrade, not a downgrade, so it's
        # allowed.
        pipeline = _bare_pipeline()
        _capture(
            pipeline,
            {"found": False, "source": "none", "records": [], "consulted_sources": [], "error": "boom"},
        )
        self.assertEqual(pipeline.provenance.get("Functional evidence (MaveDB)").status, VersionStatus.UNKNOWN)

        _capture(
            pipeline,
            {"found": False, "source": "none", "records": [], "consulted_sources": ["MaveDB"]},
        )
        self.assertEqual(pipeline.provenance.get("Functional evidence (MaveDB)").status, VersionStatus.TIMESTAMP_ONLY)


if __name__ == "__main__":
    unittest.main()
