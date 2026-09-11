"""
The report records the versions of the tools that produced it -- read from
the tools, never copied from the image's package pin.

The human's ruling, 2026-09-11: "Reports not recording which htslib version
produced them is the same gap as the model-hash -- the thing that determines
the output isn't captured alongside the output." And the acceptance, via god:
"A version copied from the Dockerfile is a claim; a version read from the
binary that ran is a measurement."

WHERE EACH VERSION IS MEASURED, AND WHY IT IS NOT ALL `--version` AT RUN TIME:
this pipeline runs exactly one of these tools itself -- tabix, for the
AlphaMissense and local-gnomAD lookups. samtools, bcftools and the variant
caller ran UPSTREAM, in whatever produced the input VCF (Kim, in the bridge
path; a laboratory's own pipeline otherwise). Running `samtools --version`
here would record a binary that did not produce this report -- the exact
claim-not-measurement the ruling rules out -- and would be flatly wrong for a
supplied VCF. So:
  - tabix/htslib: `--version` of the binary this run invokes, at run time.
  - creating software (##source), bcftools, htslib-used-by-bcftools: the VCF's
    header lines, which those tools write about themselves as they produce
    the file (`##source=freeBayes v1.3.10`, `##bcftools_viewVersion=
    1.16+htslib-1.16`). Kim's final VCF passes through `bcftools view`, so it
    carries them.
  - samtools: runs on the alignment, which this pipeline never receives, and
    writes nothing into a VCF -- so it is recorded as NOT RECORDED, with the
    reason. Never a default.

THE CONTROLS THE CARD ASKED FOR: a stub tabix that reports a different version
is reported as that version, and a VCF stamped by a different bcftools is
reported as that version -- otherwise these fields could be constants that
happen to match the shipped image.
"""

import os
import stat
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from pipeline.provenance import (
    KNOWN_SOURCES,
    TOOL_SOURCE_BCFTOOLS,
    TOOL_SOURCE_CALLER,
    TOOL_SOURCE_HTSLIB,
    TOOL_SOURCE_SAMTOOLS,
    TOOL_SOURCE_TABIX,
    RunProvenanceCollector,
    capture_tool_version,
    parse_vcf_tool_stamps,
)

_KIM_HEADER = [
    "##fileformat=VCFv4.2",
    "##source=freeBayes v1.3.10",
    "##reference=/data/ref/GRCh38.fa",
    "##bcftools_normVersion=1.16+htslib-1.16",
    "##bcftools_normCommand=norm -m-any -O v -o x.norm.vcf x.vcf",
    "##bcftools_viewVersion=1.16+htslib-1.16",
    "##bcftools_viewCommand=view -f PASS -O v -o x.pass.vcf x.soft.vcf",
]
_BARE_HEADER = ["##fileformat=VCFv4.2", "##reference=GRCh38"]
_OTHER_BCFTOOLS_HEADER = [
    "##fileformat=VCFv4.2",
    "##source=freeBayes v1.3.10",
    "##bcftools_viewVersion=1.19+htslib-1.19",
]


def _stub_binary(directory, name, version_line, exit_code=0):
    """An executable that prints one line and exits `exit_code`, on this platform."""
    if sys.platform.startswith("win"):
        path = os.path.join(directory, name + ".bat")
        with open(path, "w", encoding="ascii") as fh:
            fh.write(f"@echo {version_line}\r\n@exit /b {exit_code}\r\n")
    else:
        path = os.path.join(directory, name)
        with open(path, "w", encoding="ascii") as fh:
            fh.write(f"#!/bin/sh\necho '{version_line}'\nexit {exit_code}\n")
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _collector(header_lines, am_tabix, gnomad_tabix):
    """Runs the orchestrator's own capture method against a duck-typed self."""
    from pipeline.orchestrator import GeperPipeline

    fake_self = SimpleNamespace(provenance=RunProvenanceCollector())
    fake_config = SimpleNamespace(
        alphamissense=SimpleNamespace(TABIX_BINARY=am_tabix),
        gnomad=SimpleNamespace(TABIX_BINARY=gnomad_tabix),
    )
    with mock.patch("pipeline.orchestrator.CONFIG", fake_config):
        GeperPipeline._capture_tool_provenance(fake_self, header_lines)
    return fake_self.provenance


def _captured(header_lines, am_tabix, gnomad_tabix):
    return {r["source"]: r for r in _collector(header_lines, am_tabix, gnomad_tabix).to_list()}


class TestRegistered(unittest.TestCase):
    def test_every_tool_source_is_registered(self):
        # An unregistered name is dropped by the collector with only a log
        # warning -- the missing-vs-empty failure this registry guards.
        for source in (
            TOOL_SOURCE_TABIX,
            TOOL_SOURCE_CALLER,
            TOOL_SOURCE_BCFTOOLS,
            TOOL_SOURCE_HTSLIB,
            TOOL_SOURCE_SAMTOOLS,
        ):
            self.assertIn(source, KNOWN_SOURCES)


class TestVcfStamps(unittest.TestCase):
    def test_kim_shaped_header_yields_caller_bcftools_and_htslib(self):
        stamps = parse_vcf_tool_stamps(_KIM_HEADER)
        self.assertEqual(stamps["caller"], ["freeBayes v1.3.10"])
        self.assertEqual(stamps["bcftools"], ["1.16"])
        self.assertEqual(stamps["htslib"], ["1.16"])
        self.assertEqual(stamps["samtools"], [])

    def test_control_a_different_bcftools_is_reported_as_that_version(self):
        stamps = parse_vcf_tool_stamps(_OTHER_BCFTOOLS_HEADER)
        self.assertEqual(stamps["bcftools"], ["1.19"])
        self.assertEqual(stamps["htslib"], ["1.19"])

    def test_two_different_versions_in_one_header_are_both_kept(self):
        stamps = parse_vcf_tool_stamps(_KIM_HEADER + ["##bcftools_annotateVersion=1.19+htslib-1.19"])
        self.assertEqual(stamps["bcftools"], ["1.16", "1.19"])
        self.assertEqual(stamps["htslib"], ["1.16", "1.19"])

    def test_bare_header_yields_nothing_not_a_default(self):
        stamps = parse_vcf_tool_stamps(_BARE_HEADER)
        self.assertEqual(stamps, {"caller": [], "bcftools": [], "htslib": [], "samtools": []})


class TestCaptureToolVersion(unittest.TestCase):
    def test_control_a_stub_binary_is_reported_as_its_own_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            stub = _stub_binary(tmp, "tabix", "tabix (htslib) 9.99-stub")
            result = capture_tool_version(stub)
        self.assertEqual(result["version"], "tabix (htslib) 9.99-stub")
        self.assertIsNone(result["error"])

    def test_usage_text_from_a_failing_binary_is_not_a_version(self):
        # A tabix too old to know `--version` prints its usage and exits 1.
        with tempfile.TemporaryDirectory() as tmp:
            stub = _stub_binary(tmp, "tabix", "Usage: tabix [OPTIONS] FILE", exit_code=1)
            result = capture_tool_version(stub)
        self.assertIsNone(result["version"])
        self.assertIn("exited 1", result["error"])

    def test_missing_binary_is_an_error_not_a_version(self):
        result = capture_tool_version(os.path.join(tempfile.gettempdir(), "no-such-tabix-binary-xyz"))
        self.assertIsNone(result["version"])
        self.assertTrue(result["error"])


class TestOrchestratorCapture(unittest.TestCase):
    def test_tabix_version_is_read_from_the_binary_the_run_invokes(self):
        with tempfile.TemporaryDirectory() as tmp:
            stub = _stub_binary(tmp, "tabix", "tabix (htslib) 9.99-stub")
            recs = _captured(_KIM_HEADER, stub, stub)
        rec = recs[TOOL_SOURCE_TABIX]
        self.assertEqual(rec["status"], "version_known")
        self.assertEqual(rec["version"], "tabix (htslib) 9.99-stub")

    def test_two_different_tabix_binaries_are_both_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = _stub_binary(tmp, "tabix_a", "tabix (htslib) 1.16")
            b = _stub_binary(tmp, "tabix_b", "tabix (htslib) 1.19")
            rec = _captured(_KIM_HEADER, a, b)[TOOL_SOURCE_TABIX]
        self.assertIn("1.16", rec["version"])
        self.assertIn("1.19", rec["version"])

    def test_absent_tabix_is_an_honest_marker(self):
        missing = os.path.join(tempfile.gettempdir(), "no-such-tabix-binary-xyz")
        rec = _captured(_KIM_HEADER, missing, missing)[TOOL_SOURCE_TABIX]
        self.assertEqual(rec["status"], "unknown")
        self.assertIsNone(rec["version"])
        self.assertIn("could not be read", rec["notes"])

    def test_vcf_declared_tools_are_recorded_with_where_they_came_from(self):
        missing = os.path.join(tempfile.gettempdir(), "no-such-tabix-binary-xyz")
        recs = _captured(_KIM_HEADER, missing, missing)
        self.assertEqual(recs[TOOL_SOURCE_CALLER]["version"], "freeBayes v1.3.10")
        self.assertEqual(recs[TOOL_SOURCE_BCFTOOLS]["version"], "1.16")
        self.assertEqual(recs[TOOL_SOURCE_HTSLIB]["version"], "1.16")
        self.assertIn("input VCF's own header", recs[TOOL_SOURCE_BCFTOOLS]["notes"])

    def test_undeclared_tools_are_honest_markers_never_defaults(self):
        missing = os.path.join(tempfile.gettempdir(), "no-such-tabix-binary-xyz")
        recs = _captured(_BARE_HEADER, missing, missing)
        for source in (TOOL_SOURCE_CALLER, TOOL_SOURCE_BCFTOOLS, TOOL_SOURCE_HTSLIB):
            self.assertEqual(recs[source]["status"], "unknown", source)
            self.assertIsNone(recs[source]["version"], source)
            self.assertIn("declares no", recs[source]["notes"], source)

    def test_samtools_is_not_recorded_and_says_why(self):
        missing = os.path.join(tempfile.gettempdir(), "no-such-tabix-binary-xyz")
        rec = _captured(_KIM_HEADER, missing, missing)[TOOL_SOURCE_SAMTOOLS]
        self.assertEqual(rec["status"], "unknown")
        self.assertIsNone(rec["version"])
        self.assertIn("does not receive", rec["notes"])

    def test_run_wires_the_capture_to_the_parsed_header(self):
        # Structural, and stated as such: constructing a real GeperPipeline
        # loads model registries. This asserts that `run()` itself calls the
        # capture with the header lines of the VCF it parsed.
        import ast
        import inspect
        import textwrap

        from pipeline.orchestrator import GeperPipeline

        tree = ast.parse(textwrap.dedent(inspect.getsource(GeperPipeline.run)))
        calls = [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "_capture_tool_provenance"
        ]
        self.assertEqual(len(calls), 1)
        self.assertEqual(ast.unparse(calls[0].args[0]), "parser.header_lines")


# --- EVERY RENDERED SURFACE THAT CARRIES PROVENANCE: JSON, Markdown, full PDF.
# (The short PDF carries no provenance section; compare_reports.py consumes
# the list but renders no report.) Denominator: 3 surfaces.


def _document_with(provenance):
    from report.clinical_report_builder import build_clinical_report
    from tests.test_indian_population_frequency import _TGS_FOUND, _ir, _patch_threshold

    _patch_threshold(0.01)
    raw = {"gnomad": {"skipped": False, "found": True, "population_breakdown": {"sas": {"af": 0.02}}}}
    cr = build_clinical_report(_ir(), raw_evidence=raw, thousand_genomes_sas_result=_TGS_FOUND)
    return {
        "geper_version": "t",
        "generated_at": "2026-01-01T00:00:00Z",
        "input_vcf": "x.vcf",
        "assembly": "GRCh38",
        "vcf_samples": ["S1"],
        "variant_count": 1,
        "run_complete": True,
        "provenance": provenance,
        "variants": [
            {
                "variant": {"chrom": "1", "pos": 100, "ref": "A", "alt": "T"},
                "interpretation": {},
                "candidate_interpretation": cr,
                "ai_model_status": {},
                "errors": [],
            }
        ],
    }


class TestEverySurface(unittest.TestCase):
    def setUp(self):
        self.addCleanup(mock.patch.stopall)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        stub = _stub_binary(self.tmp.name, "tabix", "tabix (htslib) 9.99-stub")
        self.collector = _collector(_KIM_HEADER, stub, stub)
        self.provenance = self.collector.to_list()

    def _assert_carries_tools(self, text):
        for needle in (
            TOOL_SOURCE_TABIX,
            "9.99-stub",
            TOOL_SOURCE_CALLER,
            "freeBayes v1.3.10",
            TOOL_SOURCE_BCFTOOLS,
            TOOL_SOURCE_HTSLIB,
            TOOL_SOURCE_SAMTOOLS,
            "does not receive",
        ):
            self.assertIn(needle, text)

    def test_json(self):
        import json

        from report.json_builder import JSONResultBuilder

        builder = JSONResultBuilder(input_vcf_path="x.vcf", provenance_collector=self.collector)
        self._assert_carries_tools(json.dumps(builder.build()))

    def test_markdown(self):
        from report.report_generator import ReportGenerator

        self._assert_carries_tools(ReportGenerator().generate(_document_with(self.provenance)))

    def test_pdf(self):
        try:
            from pypdf import PdfReader
        except ImportError:
            self.skipTest("pypdf not installed in this environment")
        from report.summary import generate_pdf

        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "r.pdf")
            generate_pdf(_document_with(self.provenance), out)
            text = " ".join("\n".join(p.extract_text() for p in PdfReader(out).pages).split())
        self._assert_carries_tools(text)


if __name__ == "__main__":
    unittest.main()
