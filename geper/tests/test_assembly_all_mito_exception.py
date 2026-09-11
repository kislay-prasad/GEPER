"""
The all-mitochondrial exception to the undetectable-build refusal.

*** THE RULING (human, 2026-09-11, card #15): "(B), NARROW ALL-MITO EXCEPTION
WITH THE REASON ON THE REPORT. (A) is correct in principle and breaks a
legitimate case for a build that genuinely doesn't apply. THE EXCEPTION MUST
BE EXACTLY ALL-MITO, and the report must say the build check was skipped and
why." ***

WHY THERE IS AN EXCEPTION AT ALL: `4ce58a5` made `validate_assembly` REFUSE a
run whose build nobody established -- no build in the VCF header and no
--assembly -- because otherwise Ensembl's default silently becomes the answer
and every HGVS c. coordinate can be confidently wrong. A mitochondrial-only
VCF (`##contig=<ID=chrM,length=16569>`, no `##reference`) detects no build
and is therefore refused. But its coordinates COULD NOT HAVE BEEN WRONG: the
revised Cambridge Reference Sequence (rCRS) numbers chrM identically in
GRCh37 and GRCh38 -- which kim_pipeline's own runner already records as the
reason it stores `build=None` for exactly this input. And the FASTQ->report
bridge is the one caller that can legitimately send no --assembly.

*** THE NEGATIVE IS THE WHOLE CARD. An exception that admits "mostly mito" is
the refusal with a hole in it. So every test below that proves the exception
OPENS is paired with one that proves it stays SHUT the moment a single nuclear
chromosome appears -- in a record, or in a header contig. ***

WHAT "EXACTLY ALL-MITO" MEANS HERE, AND WHY IT CHECKS RECORDS AS WELL AS THE
HEADER: the card's option (B) was worded in terms of header contigs, but a
header can declare only chrM while the body carries a nuclear record. Header
contigs alone would admit that file. So the exception requires ALL THREE:
at least one record; every record on the mitochondrion; and every contig the
header declares on the mitochondrion. The "at least one record" clause is not
decoration: `all(...)` over nothing is True, and `_INDETERMINATE_VCF` in the
wiring test has no ##contig lines at all -- so a header-only rule would pass
an empty file as "all mito".

HOW THE PROOF IS SPLIT:
  * `ThroughTheRealRun` -- through `GeperPipeline.run()`, the surface a user
    reaches. THE ALL-MITO RUN IS RED BEFORE THE FIX (refused) and GREEN AFTER
    (proceeds to the Ensembl fetch). THE ONE-NUCLEAR-RECORD RUN IS REFUSED
    BOTH BEFORE AND AFTER -- that is the negative, shown to hold across the
    change rather than asserted only after it.
  * `TheValidatorItself` -- the rule's edges, unit-level.
  * `TheReportSaysWhy` -- every rendered surface that shows the build shows
    the reason in its place. RED BEFORE, GREEN AFTER.
"""

import os
import re
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pipeline.assembly_validator as assembly_validator  # noqa: E402
import pipeline.orchestrator as orchestrator  # noqa: E402
from pipeline.sequence_context import SequenceContextGenerator  # noqa: E402
from report.report_generator import ReportGenerator  # noqa: E402
from report.summary import generate_pdf  # noqa: E402
from report.summary_short import generate_short_pdf  # noqa: E402
from utils.exceptions import AssemblyMismatchError, PipelineError  # noqa: E402

try:
    from pypdf import PdfReader

    _PYPDF_AVAILABLE = True
except ImportError:
    _PYPDF_AVAILABLE = False

_HEADER = "##fileformat=VCFv4.2\n"
_COLS = "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"

#: Mitochondrial-only: the one case the ruling exempts.
_ALL_MITO_VCF = (
    _HEADER
    + "##contig=<ID=chrM,length=16569>\n"
    + _COLS
    + "chrM\t3243\t.\tA\tG\t100\tPASS\tDP=50\n"
    + "chrM\t8344\t.\tA\tG\t100\tPASS\tDP=50\n"
)

#: THE NEGATIVE: the same file with ONE nuclear record added. It must still be
#: refused -- the header is still mito-only, which is exactly why the rule
#: cannot look at the header alone.
_ONE_NUCLEAR_AMONG_MITO_VCF = (
    _HEADER
    + "##contig=<ID=chrM,length=16569>\n"
    + _COLS
    + "chrM\t3243\t.\tA\tG\t100\tPASS\tDP=50\n"
    + "chrM\t8344\t.\tA\tG\t100\tPASS\tDP=50\n"
    + "17\t7674220\t.\tC\tT\t100\tPASS\tDP=50\n"
)

_MITO_HEADER_LINES = ["##fileformat=VCFv4.2", "##contig=<ID=chrM,length=16569>", _COLS.rstrip("\n")]


class _ReachedTheFetcher(BaseException):
    """Raised in place of the Ensembl prefetch. BaseException, because the
    orchestrator swallows `Exception` around best-effort steps -- a sentinel
    it could swallow would make "the run got here" unobservable."""


class _ReachedTheDocument(BaseException):
    """Raised in place of constructing the report document, after its
    arguments have been captured."""


def _normalised(text):
    """PDF text extraction wraps long cells; compare on collapsed whitespace."""
    return re.sub(r"\s+", " ", text)


class _Harness(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.reached = {}

    def _write(self, name, text):
        path = os.path.join(self._tmp.name, name)
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        return path

    def _pipeline(self):
        """A real GeperPipeline built without loading any model -- the same
        construction-time patches as test_assembly_preflight_is_wired_into_the_run.py."""
        with (
            mock.patch.dict(orchestrator.MODEL_REGISTRY, {}, clear=True),
            mock.patch.object(orchestrator.GeperPipeline, "_probe_standalone_plugins_at_startup", lambda self: None),
            mock.patch.object(orchestrator, "log_environment_versions", lambda: None),
        ):
            return orchestrator.GeperPipeline(
                output_dir=os.path.join(self._tmp.name, "out"), assembly=None, ai_only=True
            )

    def _run_to_the_fetcher(self, vcf_text):
        def _capture(seq_ctx, *args, **kwargs):
            self.reached["called"] = True
            self.reached["assembly"] = seq_ctx.assembly
            raise _ReachedTheFetcher()

        pipeline = self._pipeline()
        with mock.patch.object(SequenceContextGenerator, "prefetch_regions", _capture):
            pipeline.run(self._write("in.vcf", vcf_text), resume=False)


# ─────────────────────────────────────────────────────────────────────────────


class TestThroughTheRealRun(_Harness):
    def test_an_all_mito_run_is_no_longer_refused(self):
        """RED BEFORE THE FIX: `4ce58a5` refuses this run with a PipelineError.
        GREEN AFTER: it proceeds to the Ensembl fetch."""
        with self.assertRaises(_ReachedTheFetcher):
            self._run_to_the_fetcher(_ALL_MITO_VCF)
        self.assertTrue(self.reached.get("called"), "the all-mito run never reached the fetcher")

    def test_one_nuclear_record_among_mito_is_still_refused(self):
        """*** THE NEGATIVE. GREEN BEFORE AND AFTER. *** Before, because
        everything undetectable was refused; after, because the exception
        must not admit a file that is merely MOSTLY mitochondrial. Asserted
        through run() so it holds regardless of how the validator is called."""
        with self.assertRaises(PipelineError) as caught:
            self._run_to_the_fetcher(_ONE_NUCLEAR_AMONG_MITO_VCF)
        self.assertIn("Could not determine", str(caught.exception))
        self.assertNotIn("called", self.reached, "a mostly-mito run reached the fetcher -- the exception has a hole")

    def test_the_skip_reason_reaches_the_report_document(self):
        """The reason must travel from the preflight into the document every
        renderer reads -- and the machine-facing `assembly` must stay None,
        because Ensembl lookups and the LIMS export expect a build token, not
        a sentence."""
        captured = {}

        def _capture_builder(*args, **kwargs):
            captured.update(kwargs)
            raise _ReachedTheDocument()

        pipeline = self._pipeline()
        with (
            mock.patch.object(SequenceContextGenerator, "prefetch_regions", lambda *a, **k: None),
            mock.patch.object(orchestrator.GeperPipeline, "_prefetch_mmsplice_results", lambda self, v: None),
            mock.patch.object(orchestrator.GeperPipeline, "_run_startup_validation", lambda self: None),
            mock.patch.object(orchestrator, "JSONResultBuilder", _capture_builder),
        ):
            with self.assertRaises(_ReachedTheDocument):
                pipeline.run(self._write("in.vcf", _ALL_MITO_VCF), resume=False)

        self.assertIsNone(captured.get("assembly"), "the machine-facing assembly must stay None")
        note = captured.get("assembly_note") or ""
        self.assertIn(
            "skipped", note.lower(), f"the report document does not say the build check was skipped: {note!r}"
        )
        self.assertIn("rCRS", note, f"the report document does not say WHY: {note!r}")


class TestTheValidatorItself(unittest.TestCase):
    """The rule's edges. Post-fix pins: these call the three-argument form."""

    def _reason(self, header_lines, record_chroms):
        fn = getattr(assembly_validator, "mito_only_exemption_reason", None)
        self.assertIsNotNone(fn, "assembly_validator has no mito_only_exemption_reason -- the exception does not exist")
        return fn(header_lines, record_chroms)

    def test_all_mito_returns_none_instead_of_raising(self):
        self.assertIsNone(
            assembly_validator.validate_assembly(_MITO_HEADER_LINES, None, record_chroms=["chrM", "chrM"])
        )

    def test_one_nuclear_record_still_raises(self):
        with self.assertRaises(AssemblyMismatchError):
            assembly_validator.validate_assembly(_MITO_HEADER_LINES, None, record_chroms=["chrM", "chrM", "17"])

    def test_a_nuclear_header_contig_still_raises_even_with_mito_records(self):
        """The records are all mito but the header declares chr7. Not exactly
        all-mito, so refused -- the caller can state the build."""
        header = ["##fileformat=VCFv4.2", "##contig=<ID=chrM,length=16569>", "##contig=<ID=chr7>", _COLS.rstrip("\n")]
        with self.assertRaises(AssemblyMismatchError):
            assembly_validator.validate_assembly(header, None, record_chroms=["chrM"])

    def test_no_records_is_not_all_mito(self):
        """*** THE VACUOUS-TRUTH GUARD. `all()` over nothing is True. ***"""
        for chroms in ([], None):
            with self.subTest(record_chroms=chroms):
                with self.assertRaises(AssemblyMismatchError):
                    assembly_validator.validate_assembly(_MITO_HEADER_LINES, None, record_chroms=chroms)

    def test_every_common_spelling_of_the_mitochondrion_is_recognised(self):
        for name in ("chrM", "MT", "chrMT", "M", "mt"):
            with self.subTest(name=name):
                self.assertIsNotNone(self._reason([], [name]), f"{name!r} was not recognised as mitochondrial")

    def test_a_near_miss_name_is_not_mitochondrial(self):
        """`chrM` must not be matched as a prefix -- a contig like `chrMito_alt`
        or `chr17` must not slip through a startswith()."""
        for name in ("chrMito_alt", "chr17", "MT2", "chrX"):
            with self.subTest(name=name):
                self.assertIsNone(self._reason([], [name]))

    def test_an_explicit_assembly_is_still_honoured_unchanged(self):
        self.assertEqual(
            assembly_validator.validate_assembly(_MITO_HEADER_LINES, "GRCh38", record_chroms=["chrM"]), "GRCh38"
        )

    def test_the_nuclear_refusal_message_is_not_weakened(self):
        with self.assertRaises(AssemblyMismatchError) as caught:
            assembly_validator.validate_assembly(_MITO_HEADER_LINES, None, record_chroms=["chrM", "1"])
        self.assertIn("--assembly", str(caught.exception))


def _document(note):
    return {
        "geper_version": "test",
        "generated_at": "2026-01-01T00:00:00+00:00",
        "input_vcf": "synthetic.vcf",
        "assembly": None,
        "assembly_note": note,
        "vcf_samples": ["SAMPLE01"],
        "variant_count": 1,
        "variants": [
            {
                "variant": {"chrom": "chrM", "pos": 3243, "ref": "A", "alt": "G"},
                "interpretation": {},
                "candidate_interpretation": None,
                "ai_model_status": {},
                "errors": [],
            }
        ],
    }


class TestTheReportSaysWhy(unittest.TestCase):
    """Every rendered surface that shows the build shows the reason in its
    place -- not "Not specified", which says nothing about why. RED BEFORE."""

    NOTE = (
        "Not applicable: mitochondrial-only run (build check skipped; rCRS "
        "positions are identical in GRCh37 and GRCh38)"
    )

    def _assert_says_why(self, text, where):
        flat = _normalised(text)
        self.assertIn("build check skipped", flat, f"{where} does not say the build check was skipped")
        self.assertIn("rCRS", flat, f"{where} does not say why")

    def test_markdown(self):
        md = ReportGenerator().generate(_document(self.NOTE))
        self.assertIn("**Genome reference build:** Not applicable", md)
        self._assert_says_why(md, "the markdown report")

    @unittest.skipUnless(_PYPDF_AVAILABLE, "pypdf not installed in this environment")
    def test_full_pdf_both_slots(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "r.pdf")
            generate_pdf(_document(self.NOTE), out)
            text = "\n".join(p.extract_text() for p in PdfReader(out).pages)
        self._assert_says_why(text, "the full PDF")
        # Both slots (the table row and the clinician-summary line), per
        # test_genome_build_disclosure.py's count of two.
        self.assertEqual(_normalised(text).count("build check skipped"), 2, "the reason must fill BOTH build slots")

    @unittest.skipUnless(_PYPDF_AVAILABLE, "pypdf not installed in this environment")
    def test_short_pdf(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "r_short.pdf")
            generate_short_pdf(_document(self.NOTE), out)
            text = "\n".join(p.extract_text() for p in PdfReader(out).pages)
        self._assert_says_why(text, "the short PDF")


if __name__ == "__main__":
    unittest.main()
