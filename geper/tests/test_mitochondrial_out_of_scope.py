"""
Report review round 8: chrM variants (e.g. m.3243A>G, MT-TL1, the
classic MELAS variant) must never reach the 28-criterion ACMG engine.
`pipeline/orchestrator.py::GeperPipeline._process_variant` gates on
`pipeline.hgvs_utils.is_mitochondrial_chrom` before anything else runs
and returns a minimal `{"variant": ..., "out_of_scope": {...}}` result
instead (`_mitochondrial_out_of_scope_result`) -- option (a) of the two
considered (see ROUND_CANDIDATES.md for option (b), deferred).

Two things these tests prove, on real data, not just pass/fail counts:

1. `is_mitochondrial_chrom` correctly separates chrM from the nuclear
   genome on `test_data/nuclear_test_with_mt.vcf` -- 5 real nuclear
   fixture variants (BRCA1 PS3/BS3, TP53 PS3/BS3, PRNP PM1) plus
   MT:3243 A>G, the real ClinVar-pathogenic m.3243A>G MELAS variant --
   parsed with the same `VCFParser` the orchestrator itself uses.

2. Every report renderer (Markdown, full PDF's per-finding section AND
   its one-page Clinician Summary table, short PDF) renders an
   out-of-scope result distinctly from every other state a variant can
   be in -- never "Pathogenic"/"Uncertain significance", never "Not
   classified"/"Pending" (today's rendering for a variant GEPER tried
   and failed to classify, a different claim), and always names the
   actual mechanism (gnomAD's separate mitochondrial callset, the
   nuclear-trained splice models, nuclear transcript resolution) rather
   than a bare "skipped".

`pipeline.orchestrator` itself is NOT imported here -- confirmed by
hand (see this round's investigation notes) to pull in a heavy
TensorFlow/absl import chain at module level that risks the OOM this
machine has already hit once. The `_mitochondrial_out_of_scope_result`/
`_classify_variant_result` shape is instead reproduced by hand from the
orchestrator source (kept in sync manually -- see
`_expected_out_of_scope_result` below) so the renderer-facing contract
can still be exercised locally. Verifying the orchestrator's own gate
wiring end-to-end (a real run producing this exact dict) is Colab's
job, not this file's.
"""

import os
import unittest

from pipeline.hgvs_utils import is_mitochondrial_chrom
from pipeline.vcf_parser import VCFParser
from report import report_generator as report_generator_module
from report import summary as summary_module
from report import summary_short as summary_short_module

_FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "..", "test_data", "nuclear_test_with_mt.vcf")

# Reproduced by hand from `pipeline/orchestrator.py`'s
# `_MITOCHONDRIAL_OUT_OF_SCOPE_REASON` -- see this file's module
# docstring for why it isn't imported directly. Keep the three named
# mechanisms (gnomAD's separate callset, the nuclear-trained models,
# transcript resolution) in sync if the source wording changes.
_REASON = (
    "Mitochondrial variants are out of scope for this GEPER build. GEPER's evidence sources have "
    "no validity on chrM: gnomAD's queryable callset here is the nuclear one, a separate resource "
    "from gnomAD's mitochondrial callset (never queried by this pipeline); the nuclear-trained "
    "sequence/splicing models (MMSplice, Enformer, Borzoi, SpliceFormer, SpliceBERT) are out of "
    "distribution on mtDNA, which has no spliceosome; and transcript resolution depends on nuclear "
    "GTF/Ensembl data that does not cover chrM. This variant was not evaluated against any ACMG/AMP "
    "criterion -- it is reported here as an explicit out-of-scope entry, never as a Variant of "
    "Uncertain Significance assembled from criteria that should not have been able to fire."
)


def _expected_out_of_scope_result(variant_dict):
    """Mirrors `GeperPipeline._mitochondrial_out_of_scope_result` exactly."""
    return {
        "variant": variant_dict,
        "out_of_scope": {"scope": "mitochondrial_genome", "reason": _REASON},
        "errors": [],
    }


class TestIsMitochondrialChromOnRealFixture(unittest.TestCase):
    """Real variants, real fixture -- not synthetic chrom strings."""

    def setUp(self):
        parser = VCFParser(_FIXTURE_PATH)
        self.variants = list(parser.iter_variants())

    def test_fixture_has_six_variants(self):
        self.assertEqual(len(self.variants), 6)

    def test_only_the_mt_record_is_mitochondrial(self):
        results = {(v.chrom, v.pos): is_mitochondrial_chrom(v.chrom) for v in self.variants}
        expected = {
            ("17", 43106534): False,  # BRCA1 PS3
            ("17", 43094298): False,  # BRCA1 BS3
            ("17", 7674221): False,  # TP53 PS3
            ("17", 7676152): False,  # TP53 BS3
            ("20", 4699525): False,  # PRNP PM1
            ("MT", 3243): True,  # MT-TL1, m.3243A>G, MELAS
        }
        self.assertEqual(results, expected)

    def test_mt_3243_variant_identity(self):
        mt_variant = next(v for v in self.variants if v.chrom == "MT")
        self.assertEqual(mt_variant.pos, 3243)
        self.assertEqual(mt_variant.ref, "A")
        self.assertEqual(mt_variant.alt, "G")
        self.assertEqual(mt_variant.info.get("GENE"), "MT-TL1")
        self.assertTrue(is_mitochondrial_chrom(mt_variant.chrom))

    def test_nuclear_variants_all_pass_the_gate(self):
        nuclear = [v for v in self.variants if v.chrom != "MT"]
        self.assertEqual(len(nuclear), 5)
        for v in nuclear:
            with self.subTest(chrom=v.chrom, pos=v.pos, gene=v.info.get("GENE")):
                self.assertFalse(is_mitochondrial_chrom(v.chrom))


class TestMarkdownRendering(unittest.TestCase):
    def setUp(self):
        parser = VCFParser(_FIXTURE_PATH)
        self.mt_variant = next(v for v in parser.iter_variants() if v.chrom == "MT")
        self.result = _expected_out_of_scope_result(self.mt_variant.to_dict())
        self.gen = report_generator_module.ReportGenerator()

    def test_renders_out_of_scope_status_not_a_classification(self):
        lines = self.gen._render_variant_section(1, self.result)
        text = "\n".join(lines)
        self.assertIn("Out of scope", text)
        self.assertIn("mitochondrial_genome", text)
        self.assertNotIn("Pathogenic", text)
        self.assertNotIn("Uncertain significance", text)
        self.assertNotIn("Not classified", text)

    def test_reason_names_the_three_mechanisms(self):
        lines = self.gen._render_variant_section(1, self.result)
        text = "\n".join(lines)
        self.assertIn("gnomAD", text)
        self.assertIn("nuclear-trained", text)
        self.assertIn("transcript resolution", text)

    def test_never_calls_it_a_vus(self):
        lines = self.gen._render_variant_section(1, self.result)
        text = "\n".join(lines)
        self.assertIn("not a Variant of Uncertain Significance", text)

    def test_annotation_detail_audit_trail_is_not_rendered(self):
        # The normal per-variant path renders ~15 raw-stage sections
        # ("Annotation Detail (Audit Trail)") -- none of that ran for
        # an out-of-scope variant, so none of it should appear.
        lines = self.gen._render_variant_section(1, self.result)
        text = "\n".join(lines)
        self.assertNotIn("Annotation Detail", text)

    def test_full_generate_includes_the_out_of_scope_finding(self):
        document = {
            "generated_at": "2026-08-13T00:00:00+00:00",
            "input_vcf": "nuclear_test_with_mt.vcf",
            "variant_count": 1,
            "variants": [self.result],
        }
        markdown = self.gen.generate(document)
        self.assertIn("MT:3243", markdown)
        self.assertIn("Out of scope", markdown)


class TestFullPdfVariantSection(unittest.TestCase):
    def setUp(self):
        parser = VCFParser(_FIXTURE_PATH)
        self.mt_variant = next(v for v in parser.iter_variants() if v.chrom == "MT")
        self.result = _expected_out_of_scope_result(self.mt_variant.to_dict())
        self.styles = summary_module._build_stylesheet()

    def _flow_text(self, flowables):
        texts = []
        for f in flowables:
            if hasattr(f, "text"):
                texts.append(f.text)
        return "\n".join(texts)

    def test_variant_section_renders_out_of_scope(self):
        flow = summary_module._build_variant_section(1, self.result, self.styles)
        text = self._flow_text(flow)
        self.assertIn("Out of scope", text)
        self.assertIn("mitochondrial_genome", text)
        self.assertIn("gnomAD", text)
        self.assertNotIn("ACMG/AMP Classification", text)

    def test_variant_section_never_shows_pathogenic_or_pending_confidence(self):
        flow = summary_module._build_variant_section(1, self.result, self.styles)
        text = self._flow_text(flow)
        self.assertNotIn("Pathogenic", text)
        self.assertNotIn("Evidence Completeness", text)


class TestClinicianSummaryTable(unittest.TestCase):
    """The one-page dashboard row -- the very first thing a clinician
    sees. Must not render as "Not classified" / "Pending" (today's
    rendering for a genuinely-attempted-but-inconclusive variant)."""

    def setUp(self):
        parser = VCFParser(_FIXTURE_PATH)
        variants = list(parser.iter_variants())
        self.mt_variant = next(v for v in variants if v.chrom == "MT")
        self.mt_result = _expected_out_of_scope_result(self.mt_variant.to_dict())
        self.styles = summary_module._build_stylesheet()

    def _cell_text(self, paragraph):
        return getattr(paragraph, "text", str(paragraph))

    def test_out_of_scope_row_renders_distinctly(self):
        table = summary_module._build_clinician_summary_table([self.mt_result], self.styles)
        # rows[0] is the header; rows[1] is our one variant.
        row = table._cellvalues[1]
        classification_text = self._cell_text(row[2])
        confidence_text = self._cell_text(row[3])
        self.assertEqual(classification_text, "Out of scope (mitochondrial)")
        self.assertEqual(confidence_text, "N/A")
        self.assertNotEqual(classification_text, "Not classified")
        self.assertNotEqual(confidence_text, "Pending")

    def test_out_of_scope_row_carries_no_reviewer_flag(self):
        table = summary_module._build_clinician_summary_table([self.mt_result], self.styles)
        row = table._cellvalues[1]
        flags_text = self._cell_text(row[-1])
        self.assertEqual(flags_text, "—")

    def test_mixed_run_nuclear_row_unaffected_by_out_of_scope_row(self):
        # A minimal "no clinical_report at all" nuclear-style stand-in
        # (this suite can't run the real 28-criterion engine locally --
        # heavy imports, see module docstring) -- proves the two rows
        # render independently, not that the nuclear classification
        # itself is correct (Colab's job).
        nuclear_stub = {"variant": {"chrom": "17", "pos": 43106534, "ref": "C", "alt": "A"}}
        table = summary_module._build_clinician_summary_table([nuclear_stub, self.mt_result], self.styles)
        nuclear_row = table._cellvalues[1]
        mt_row = table._cellvalues[2]
        self.assertEqual(self._cell_text(nuclear_row[2]), "Not classified")
        self.assertEqual(self._cell_text(mt_row[2]), "Out of scope (mitochondrial)")


class TestShortPdfVariantBlock(unittest.TestCase):
    def setUp(self):
        parser = VCFParser(_FIXTURE_PATH)
        self.mt_variant = next(v for v in parser.iter_variants() if v.chrom == "MT")
        self.result = _expected_out_of_scope_result(self.mt_variant.to_dict())
        self.styles = summary_short_module._build_short_stylesheet()

    def _flow_text(self, flowables):
        return "\n".join(getattr(f, "text", "") for f in flowables if hasattr(f, "text"))

    def test_short_block_renders_out_of_scope(self):
        flow = summary_short_module._build_variant_block(1, self.result, self.styles)
        text = self._flow_text(flow)
        self.assertIn("Out of scope", text)
        self.assertIn("mitochondrial_genome", text)
        self.assertIn("MT:3243", text)

    def test_short_block_never_shows_classification_strip(self):
        flow = summary_short_module._build_variant_block(1, self.result, self.styles)
        text = self._flow_text(flow)
        self.assertNotIn("Not classified", text)
        self.assertNotIn("Pending", text)


if __name__ == "__main__":
    unittest.main()
