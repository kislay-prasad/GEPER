"""
The assembly preflight's CALL SITE, not the preflight.

*** THE DEFECT, MEASURED RATHER THAN ARGUED (2026-09-11): the whole
preflight block was deleted from `pipeline/orchestrator.py::run()` --
the `try/except` that refuses AND the write-back that hands the
detected build to the fetcher -- replaced with `pass`, and the entire
suite still passed. 2640 passed / 22 skipped / 29 deselected / 661
subtests BEFORE, and exactly the same numbers with the preflight gone.
NOT ONE TEST OUTCOME CHANGED. *** The mutation was confirmed still
applied at the END of that run, because a mutation that silently
reverted would produce that same result for the opposite reason.

WHY THE EXISTING COVERAGE DOES NOT SEE IT: all nine tests in
`test_assembly_validator_blocks_when_indeterminate.py` call
`validate_assembly` directly. They prove the function refuses, that it
refuses for the right reason, and that it can fail -- and every one of
them passes just as happily on a pipeline that never calls it. *** A
FUNCTION PROVEN CORRECT AND PROVEN CAPABLE OF FAILING, WIRED IN BY A
LINE PROVEN BY NOBODY. *** Delete that line in a refactor and the only
signal is a clinician reading a confident wrong HGVS c. coordinate.

SO EVERY TEST HERE DRIVES THE REAL `GeperPipeline.run()`, and each one
must die with the call site rather than with the validator.

*** THE BLOCK DOES TWO SEPARABLE THINGS AND THIS FILE PINS THEM
SEPARATELY. It REFUSES when nobody established the build, and it WRITES
THE RESOLVED BUILD BACK onto the sequence-context generator. A test
that only pins the refusal survives deleting the write-back -- and a
lost write-back is the SILENT wrong-coordinate path (a GRCh37 VCF
annotated against whatever Ensembl defaults to) rather than the loud
one. ***

ON THE TWO PIECES OF MACHINERY BELOW, both of which exist because a
weaker version of them failed while this was being measured:

  1. `_ReachedTheFetcher` derives from **BaseException, not Exception**.
     The orchestrator wraps its Ensembl prefetch in `except Exception`
     on purpose ("optimization only, must never block a run"), so a
     sentinel raised as an ordinary Exception is SWALLOWED and the run
     sails on into live network calls. *** A CONTROL THAT SIGNALS BY
     RAISING A GENERIC EXCEPTION CANNOT SURVIVE A CODEBASE THAT
     DELIBERATELY SWALLOWS GENERIC EXCEPTIONS. ***
  2. Each fixture's own premise is asserted AT RUN TIME -- that the
     indeterminate header really is undetectable, and that the GRCh37
     header really is detected -- so a change to the detector cannot
     quietly turn these tests into no-ops that pass by never reaching
     the thing they are about.
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pipeline.orchestrator as orchestrator  # noqa: E402
from pipeline.assembly_validator import detect_vcf_assembly  # noqa: E402
from pipeline.sequence_context import SequenceContextGenerator  # noqa: E402
from utils.exceptions import PipelineError  # noqa: E402

#: A header with no build tag and no chr1 contig length. Nobody --
#: not the VCF, not the caller -- establishes a build for this file.
_INDETERMINATE_VCF = """##fileformat=VCFv4.2
##source=SomeCaller
##FILTER=<ID=PASS,Description="All filters passed">
#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO
17\t7674220\t.\tC\tT\t100\tPASS\tDP=50
"""

#: The same variant in a file that names its build. The detected value
#: is GRCh37 deliberately: it is NOT what Ensembl defaults to, so a
#: lost write-back changes the answer instead of coinciding with it.
_GRCH37_VCF = """##fileformat=VCFv4.2
##reference=file:///refs/GRCh37.fasta
#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO
17\t7674220\t.\tC\tT\t100\tPASS\tDP=50
"""


class _ReachedTheFetcher(BaseException):
    """Raised in place of the Ensembl prefetch -- see this module's docstring."""


def _header_lines(vcf_text):
    return [line for line in vcf_text.splitlines() if line.startswith("#")]


class AssemblyPreflightIsWiredIntoTheRun(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.reached = {}

    def _write(self, name, text):
        path = os.path.join(self._tmp.name, name)
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        return path

    def _pipeline(self, assembly=None):
        """
        A real `GeperPipeline`, constructed without loading any model.

        The three patches are construction-time only and none of them
        can reach the preflight, which runs before any model is
        touched: an empty MODEL_REGISTRY skips the per-model
        `is_available()` probe (which really loads ESM2 on this box),
        and the plugin probe and the startup version table are pure
        logging.
        """
        with (
            mock.patch.dict(orchestrator.MODEL_REGISTRY, {}, clear=True),
            mock.patch.object(orchestrator.GeperPipeline, "_probe_standalone_plugins_at_startup", lambda self: None),
            mock.patch.object(orchestrator, "log_environment_versions", lambda: None),
        ):
            return orchestrator.GeperPipeline(
                output_dir=os.path.join(self._tmp.name, "out"),
                assembly=assembly,
                ai_only=True,
            )

    def _run_to_the_fetcher(self, pipeline, vcf_path):
        """
        Run until the first Ensembl call, recording the assembly the
        fetcher is holding at that moment, then stop the run dead.
        """

        def _capture(seq_ctx, *args, **kwargs):
            self.reached["assembly"] = seq_ctx.assembly
            self.reached["called"] = True
            raise _ReachedTheFetcher()

        with mock.patch.object(SequenceContextGenerator, "prefetch_regions", _capture):
            pipeline.run(vcf_path, resume=False)

    # --- the refusal half ------------------------------------------------

    def test_an_unidentifiable_build_stops_the_run_itself(self):
        """
        *** THE ORCHESTRATOR, NOT THE VALIDATOR. This is the assertion
        that goes red when the call site is deleted, and the nine
        existing tests do not. ***
        """
        self.assertIsNone(
            detect_vcf_assembly(_header_lines(_INDETERMINATE_VCF)),
            "fixture premise: this header must be undetectable, or this test proves nothing",
        )
        vcf = self._write("indeterminate.vcf", _INDETERMINATE_VCF)
        pipeline = self._pipeline(assembly=None)

        with self.assertRaises(PipelineError) as caught:
            self._run_to_the_fetcher(pipeline, vcf)

        message = str(caught.exception)
        self.assertIn("Could not determine", message)
        self.assertIn("--assembly", message)
        # *** AND IT MUST STOP BEFORE ANY REFERENCE SEQUENCE IS FETCHED.
        # A refusal that arrives after the first Ensembl call is the
        # same defect one layer down -- the run has already asked the
        # wrong build for data. ***
        self.assertNotIn("called", self.reached, "the run reached the Ensembl fetcher before refusing")
        self.assertIsNone(pipeline.sequence_context_gen.assembly)

    def test_a_stated_build_that_contradicts_the_header_stops_the_run_itself(self):
        """The mismatch branch, also asserted through `run()` rather than the function."""
        self.assertEqual(detect_vcf_assembly(_header_lines(_GRCH37_VCF)), "GRCh37")
        vcf = self._write("grch37.vcf", _GRCH37_VCF)
        pipeline = self._pipeline(assembly="GRCh38")

        with self.assertRaises(PipelineError) as caught:
            self._run_to_the_fetcher(pipeline, vcf)

        self.assertIn("Assembly mismatch", str(caught.exception))
        self.assertNotIn("called", self.reached, "the run reached the Ensembl fetcher before refusing")

    # --- the write-back half ---------------------------------------------

    def test_the_detected_build_reaches_the_fetcher_before_any_variant_is_processed(self):
        """
        *** THE HALF A REFUSAL TEST DOES NOT COVER. `:742-743` writes the
        detected build onto the sequence-context generator; delete only
        those two lines and every refusal above still passes while a
        GRCh37 VCF is quietly annotated against Ensembl's default. ***

        Asserted at the moment of the first fetch rather than after the
        run, because that is when the value is actually used.
        """
        self.assertEqual(
            detect_vcf_assembly(_header_lines(_GRCH37_VCF)),
            "GRCh37",
            "fixture premise: this header must detect as GRCh37, or this test proves nothing",
        )
        vcf = self._write("grch37.vcf", _GRCH37_VCF)
        pipeline = self._pipeline(assembly=None)
        self.assertIsNone(pipeline.sequence_context_gen.assembly, "precondition: nothing has set a build yet")

        with self.assertRaises(_ReachedTheFetcher):
            self._run_to_the_fetcher(pipeline, vcf)

        # Not `assertEqual(self.reached.get("assembly"), ...)`: a
        # missing key would compare as None and could not be told from
        # a fetcher that was never reached.
        self.assertTrue(self.reached.get("called"), "the run never reached the fetcher, so nothing was measured")
        self.assertEqual(self.reached["assembly"], "GRCh37")

    def test_a_build_the_caller_stated_reaches_the_fetcher_too(self):
        """
        The caller-supplied path, which takes a different branch:
        `:742`'s `not self._cli_assembly` guard means the write-back
        deliberately does NOT fire here, and the value must already be
        in place from construction.
        """
        vcf = self._write("indeterminate.vcf", _INDETERMINATE_VCF)
        pipeline = self._pipeline(assembly="GRCh37")

        with self.assertRaises(_ReachedTheFetcher):
            self._run_to_the_fetcher(pipeline, vcf)

        self.assertTrue(self.reached.get("called"), "the run never reached the fetcher, so nothing was measured")
        self.assertEqual(self.reached["assembly"], "GRCh37")


if __name__ == "__main__":
    unittest.main()
