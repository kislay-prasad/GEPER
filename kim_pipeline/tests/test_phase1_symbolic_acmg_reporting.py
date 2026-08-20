"""
tests/test_phase1_symbolic_acmg_reporting.py
─────────────────────────────────────────────
Regression tests for GEPER Phase 1 stabilization:

  Issue 5 — Symbolic spanning-deletion placeholder alleles (ALT=*) were not
            filtered before annotation/ACMG and could be misclassified.
  Issue 6 — ACMG criteria that could not be evaluated (missing gene
            annotation, disabled ClinVar/gnomAD, no pedigree data, etc.)
            were reported as a bare "Not Met", indistinguishable from a
            criterion that was actually evaluated and found not to apply.
  Issue 7 — Reports silently rendered `gene = null` / blank ClinVar/gnomAD
            cells with no explanation.
"""

from __future__ import annotations

import logging

# ─── Issue 5: ALT=* symbolic-allele filtering ─────────────────────────────────


class TestSymbolicAltFiltering:
    def _write_vcf(self, tmp_path):
        vcf = tmp_path / "symbolic.vcf"
        vcf.write_text(
            "##fileformat=VCFv4.2\n"
            "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
            "chr1\t100\t.\tA\tT\t50\tPASS\t.\n"
            "chr1\t200\t.\tAGCT\t*\t50\tPASS\t.\n"
            "chr1\t300\t.\tG\tC\t50\tPASS\t.\n"
            "chr2\t400\t.\tT\tG,*\t50\tPASS\t.\n"  # multi-allelic: one real, one symbolic
        )
        return str(vcf)

    def test_iter_vcf_filters_alt_star(self, tmp_path):
        from pipeline.annotation.stage import _iter_vcf

        vcf_path = self._write_vcf(tmp_path)
        skipped = []
        variants = list(_iter_vcf(vcf_path, skipped=skipped))

        # Real variants (including the real allele from the multi-allelic
        # record) must all be kept; ALT=* records must never appear.
        kept = [(v.chrom, v.pos, v.ref, v.alt) for v in variants]
        assert ("chr1", 100, "A", "T") in kept
        assert ("chr1", 300, "G", "C") in kept
        assert ("chr2", 400, "T", "G") in kept
        assert all(v.alt != "*" for v in variants)

        # Skipped records must be tracked with a documented reason.
        assert len(skipped) == 2
        assert all(s["alt"] == "*" for s in skipped)
        assert all("symbolic" in s["reason"].lower() for s in skipped)

    def test_iter_vcf_without_skipped_list_still_filters(self, tmp_path):
        """Filtering must not depend on the caller opting in to tracking."""
        from pipeline.annotation.stage import _iter_vcf

        vcf_path = self._write_vcf(tmp_path)
        variants = list(_iter_vcf(vcf_path))
        assert all(v.alt != "*" for v in variants)

    def test_alt_star_never_reaches_annotation_stage_output(self, tmp_path):
        from pipeline.annotation.stage import AnnotationStage

        vcf_path = self._write_vcf(tmp_path)
        stage = AnnotationStage(cfg={})
        result = stage.run(
            filtered_vcf_path=vcf_path,
            output_dir=str(tmp_path / "out"),
            sample_id="S1",
        )
        assert all(v.alt != "*" for v in result.variants)
        assert result.skipped_symbolic, (
            "symbolic ALT=* records must be recorded, not dropped silently"
        )
        assert (
            result.total_variants == 3
        )  # 2 single-allelic + 1 real allele from multi-allelic record

    def test_annotation_result_to_dict_exposes_skip_reason(self, tmp_path):
        from pipeline.annotation.stage import AnnotationStage

        vcf_path = self._write_vcf(tmp_path)
        stage = AnnotationStage(cfg={})
        result = stage.run(
            filtered_vcf_path=vcf_path,
            output_dir=str(tmp_path / "out"),
            sample_id="S1",
        )
        d = result.to_dict()
        assert d["skipped_symbolic_count"] == len(result.skipped_symbolic)
        assert all("reason" in s for s in d["skipped_symbolic"])

    def test_alt_star_logs_skip_reason(self, tmp_path, caplog):
        from pipeline.annotation.stage import _iter_vcf

        vcf_path = self._write_vcf(tmp_path)
        with caplog.at_level(logging.INFO, logger="geper.pipeline.annotation.stage"):
            list(_iter_vcf(vcf_path))
        assert any("symbolic" in rec.message.lower() for rec in caplog.records)


# ─── Issue 6: ACMG Unknown / Insufficient-Data state ──────────────────────────


class TestAcmgUnknownState:
    def _ev(self, **overrides):
        from pipeline.acmg.classifier import VariantEvidence

        base = dict(chrom="1", pos=100, ref="A", alt="T")
        base.update(overrides)
        return VariantEvidence(**base)

    def _clf(self, cfg=None):
        from pipeline.acmg.classifier import AcmgClassifier

        return AcmgClassifier(cfg=cfg or {})

    def test_pvs1_not_evaluated_when_gene_missing(self):
        from pipeline.acmg.classifier import STATUS_NOT_EVALUATED

        ev = self._ev(gene=None, is_lof=True, lof_gene_intolerant=True)
        result = self._clf().classify(ev)
        pvs1 = next(c for c in result.all_criteria if c.code == "PVS1")
        assert pvs1.status == STATUS_NOT_EVALUATED
        assert pvs1.met is False
        assert "PVS1" in result.criteria_unknown
        assert "PVS1" not in result.criteria_met
        assert "PVS1" not in result.criteria_not_met

    def test_pvs1_evaluated_normally_when_gene_present(self):
        from pipeline.acmg.classifier import STATUS_MET

        ev = self._ev(gene="BRCA1", is_lof=True, lof_gene_intolerant=True)
        result = self._clf().classify(ev)
        pvs1 = next(c for c in result.all_criteria if c.code == "PVS1")
        assert pvs1.status == STATUS_MET
        assert "PVS1" in result.criteria_met

    def test_pm2_not_evaluated_when_gnomad_unavailable(self):
        """gnomAD disabled/unreachable → Unknown, NOT a confident 'not met'."""
        from pipeline.acmg.classifier import STATUS_NOT_EVALUATED

        ev = self._ev(gnomad_af=None, gnomad_af_popmax=None, gnomad_af_absent=False)
        result = self._clf().classify(ev)
        pm2 = next(c for c in result.all_criteria if c.code == "PM2")
        assert pm2.status == STATUS_NOT_EVALUATED
        assert "PM2" in result.criteria_unknown

    def test_pm2_confirmed_absent_still_fires(self):
        from pipeline.acmg.classifier import STATUS_MET

        ev = self._ev(gnomad_af=None, gnomad_af_popmax=None, gnomad_af_absent=True)
        result = self._clf().classify(ev)
        pm2 = next(c for c in result.all_criteria if c.code == "PM2")
        assert pm2.status == STATUS_MET

    def test_pp5_not_evaluated_when_clinvar_significance_missing(self):
        from pipeline.acmg.classifier import STATUS_NOT_EVALUATED

        ev = self._ev(clinvar_significance=None)
        result = self._clf().classify(ev)
        pp5 = next(c for c in result.all_criteria if c.code == "PP5")
        assert pp5.status == STATUS_NOT_EVALUATED

    def test_ps4_always_not_evaluated(self):
        """PS4 requires case/control data never available automatically —
        must never be reported as a confirmed 'not met'."""
        from pipeline.acmg.classifier import STATUS_NOT_EVALUATED

        ev = self._ev()
        result = self._clf().classify(ev)
        ps4 = next(c for c in result.all_criteria if c.code == "PS4")
        assert ps4.status == STATUS_NOT_EVALUATED

    def test_pp3_not_evaluated_with_no_insilico_scores(self):
        from pipeline.acmg.classifier import STATUS_NOT_EVALUATED

        ev = self._ev(
            cadd_phred=None, revel_score=None, spliceai_score=None, alphamissense_score=None
        )
        result = self._clf().classify(ev)
        pp3 = next(c for c in result.all_criteria if c.code == "PP3")
        assert pp3.status == STATUS_NOT_EVALUATED

    def test_unknown_criteria_never_affect_classification(self):
        """Marking a criterion 'not evaluated' instead of 'not met' must not
        change the ACMG classification outcome — only reporting."""
        ev_gene = self._ev(gene="BRCA1", is_missense=True, missense_constrained=True)
        ev_no_gene = self._ev(gene=None, is_missense=True, missense_constrained=True)
        clf = self._clf()
        r1 = clf.classify(ev_gene)
        r2 = clf.classify(ev_no_gene)
        # PP2 doesn't gate on gene (existing contract), so both classify
        # identically; this test documents that criteria_unknown is purely
        # additive and doesn't perturb classification math.
        assert r1.classification == r2.classification

    def test_criteria_result_status_and_met_stay_consistent(self):
        from pipeline.acmg.classifier import STATUS_MET, STATUS_NOT_EVALUATED, CriteriaResult

        r = CriteriaResult(
            code="X",
            met=False,
            status=STATUS_MET,
            strength="supporting",
            direction="pathogenic",
            reason="test",
        )
        assert r.met is True
        r2 = CriteriaResult(
            code="Y",
            met=False,
            status=STATUS_NOT_EVALUATED,
            strength="supporting",
            direction="pathogenic",
            reason="test",
        )
        assert r2.met is False


# ─── Issue 7: Reporting transparency for unavailable annotation ──────────────


class TestReportingTransparency:
    def test_variants_table_states_reason_for_missing_gene(self):
        from pipeline.reporting.stage import _variants_to_html_table

        variants = [{"chrom": "1", "pos": 100, "ref": "A", "alt": "T", "gene_name": None}]
        html = _variants_to_html_table(variants, gene_unavailable_reason="VEP disabled")
        assert "Gene annotation unavailable" in html
        assert "VEP disabled" in html
        assert "null" not in html.lower()

    def test_variants_table_shows_gene_when_present(self):
        from pipeline.reporting.stage import _variants_to_html_table

        variants = [{"chrom": "1", "pos": 100, "ref": "A", "alt": "T", "gene_name": "BRCA1"}]
        html = _variants_to_html_table(variants, gene_unavailable_reason="VEP disabled")
        assert "BRCA1" in html
        assert "unavailable" not in html.lower()

    def test_acmg_table_states_reason_for_missing_gene(self):
        from pipeline.reporting.stage import _acmg_to_html_table

        rows = [
            {
                "chrom": "1",
                "pos": 100,
                "ref": "A",
                "alt": "T",
                "gene": None,
                "gene_unavailable_reason": "VEP disabled",
                "classification": "Uncertain_Significance",
                "criteria_met": [],
                "criteria_unknown": ["PVS1", "PM1"],
            }
        ]
        html = _acmg_to_html_table(rows)
        assert "Gene annotation unavailable" in html
        assert "VEP disabled" in html
        assert "PVS1" in html and "PM1" in html  # criteria_unknown surfaced

    def test_acmg_table_states_clinvar_and_gnomad_disabled_reasons(self):
        from pipeline.reporting.stage import _acmg_to_html_table

        rows = [
            {
                "chrom": "1",
                "pos": 100,
                "ref": "A",
                "alt": "T",
                "gene": "BRCA1",
                "classification": "Uncertain_Significance",
                "criteria_met": [],
                "criteria_unknown": [],
                "clinvar_unavailable_reason": "ClinVar lookup skipped (disabled)",
                "gnomad_unavailable_reason": "gnomAD lookup skipped (disabled)",
            }
        ]
        html = _acmg_to_html_table(rows)
        assert "ClinVar lookup skipped (disabled)" in html
        assert "gnomAD lookup skipped (disabled)" in html

    def test_annotation_summary_surfaces_skipped_symbolic_variants(self):
        from pipeline.reporting.stage import _annotation_summary_to_html

        ann_d = {
            "gff3_source": "",
            "total_variants": 3,
            "skipped_symbolic": [
                {
                    "chrom": "1",
                    "pos": 200,
                    "ref": "AGCT",
                    "alt": "*",
                    "reason": "Symbolic VCF placeholder (ALT=*)",
                },
            ],
        }
        html = _annotation_summary_to_html(ann_d)
        assert "Symbolic VCF placeholder" in html
        assert "1 variant(s) skipped" in html


# ─── Issue 1 (exact log wording) ──────────────────────────────────────────────


class TestGnomadConstraintOfflineMode:
    """FIX (Issue 1 extension): GnomadConstraintLookup queries the same
    gnomAD GraphQL host as GnomadLookup for gene constraint (pLI/LOEUF)
    data used by PVS1/PP2. `gnomad.enabled: false` must disable this
    traffic too, not just the allele-frequency lookup.
    """

    def test_disabled_gnomad_prevents_constraint_api_calls(self):
        from unittest.mock import patch

        from pipeline.constraint.lookup import GnomadConstraintLookup

        lkp = GnomadConstraintLookup(cfg={"gnomad": {"enabled": False}})
        assert lkp._backend == "disabled"
        with patch("requests.post") as mock_post, patch("requests.get") as mock_get:
            # No OMIM fallback exists any more (retired) -- with gnomAD
            # disabled and no local data, both methods must fall back to
            # their documented "unavailable" default (False) rather than
            # raising or silently querying the network.
            assert lkp.is_lof_intolerant("BRCA1") is False
            assert lkp.is_missense_constrained("BRCA1") is False
            mock_post.assert_not_called()
            mock_get.assert_not_called()

    def test_local_tsv_still_used_even_if_gnomad_disabled(self, tmp_path):
        """A local constraint TSV is not a live API call, so it should
        still be usable even with gnomad.enabled: false."""
        from pipeline.constraint.lookup import GnomadConstraintLookup

        tsv = tmp_path / "constraint.tsv"
        tsv.write_text("gene\ttranscript\tpli\toe_lof_upper\nBRCA1\tNM_1\t0.99\t0.1\n")
        lkp = GnomadConstraintLookup(
            cfg={"gnomad": {"enabled": False}, "gnomad_constraint": {"tsv_path": str(tsv)}}
        )
        assert lkp._backend == "local"
        assert lkp.is_lof_intolerant("BRCA1") is True


class TestExactSkipLogWording:
    def test_clinvar_disabled_logs_exact_message(self, caplog):
        from pipeline.clinvar.lookup import ClinVarLookup

        with caplog.at_level(logging.INFO):
            ClinVarLookup(cfg={"clinvar": {"enabled": False}})
        assert any(r.message == "ClinVar lookup skipped (disabled)" for r in caplog.records)

    def test_gnomad_disabled_logs_exact_message(self, caplog):
        from pipeline.gnomad.lookup import GnomadLookup

        with caplog.at_level(logging.INFO):
            GnomadLookup(cfg={"gnomad": {"enabled": False}})
        assert any(r.message == "gnomAD lookup skipped (disabled)" for r in caplog.records)

    def test_clinvar_enabled_property(self):
        from pipeline.clinvar.lookup import ClinVarLookup

        assert ClinVarLookup(cfg={"clinvar": {"enabled": False}}).enabled is False
        assert ClinVarLookup(cfg={"clinvar": {"enabled": True}}).enabled is True

    def test_gnomad_enabled_property(self):
        from pipeline.gnomad.lookup import GnomadLookup

        assert GnomadLookup(cfg={"gnomad": {"enabled": False}}).enabled is False
        assert GnomadLookup(cfg={"gnomad": {"enabled": True}}).enabled is True
