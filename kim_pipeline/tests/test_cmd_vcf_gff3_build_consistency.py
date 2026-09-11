"""
tests/test_cmd_vcf_gff3_build_consistency.py
──────────────────────────────────────────────
`kim vcf` (main.py::cmd_vcf) is the OTHER standalone entry point that
builds a GFF3-derived HGVS `c.` coordinate -- via the exact same
AnnotationStage/RnaTranscriptAnalyser machinery `analyze --mode full`
(PipelineRunner.run) uses -- and unlike that path (fixed at `88c7351`,
see test_vcf_gff3_build_disagreement_blocks.py), it never called
`check_vcf_gff3_build_consistency()` at all.

Confirmed live before this fix (hive card
DEFECT-standalone-kim-mode-full-builds-a-GFF3-derived-c-coordinate...):
one VCF, one untouched chr1:250 A>G variant, two GFF3s differing ONLY
in where the CDS is anchored (100 vs 200 -- the same shape a real
GRCh37/38 liftover shift takes for a real gene) produced TWO DIFFERENT,
EQUALLY WELL-FORMED coordinates through the real `AnnotationStage.run()`
`cmd_vcf` calls: `TX1:c.151A>G` (agreeing) and `TX1:c.51A>G`
(disagreeing) -- both "success", no warning either way. A wrong `c.` is
the worst output this system can produce because it is well-formed.

Ruling: this is not a new decision about what the pipeline refuses --
`88c7351` already established that a definite VCF/GFF3 disagreement is
refused, not warned. `cmd_vcf` was an unimplemented site of that same
ruling, not a new one. Fix: call the same, already-tested
`check_vcf_gff3_build_consistency()` inside `cmd_vcf`, mirroring
`runner.py`'s placement (immediately before the GFF3's transcript model
is first used, i.e. before Stage 1 VEP/Annotation) and its own
established `ConfigValidationError` -> exit-code-2 handling (already
used for the identical exception type in `cmd_analyze`, main.py:241-243).

`runner.py` is byte-unchanged by this fix -- it was already correct and
already tested by the 12 tests in test_vcf_gff3_build_disagreement_blocks.py,
which must stay green.

The parity class below closes the "two call sites can drift apart"
risk named when this fix was authorized: it (a) asserts runner.py's own
call site is still present verbatim (guards against silent removal --
its BEHAVIOUR is already covered by the 12 existing tests, this only
guards its continued existence), and (b) calls the exact same function
runner.py calls, with the exact same disagreeing pair this file drives
through `cmd_vcf`, and shows it is refused there too -- same pair, same
underlying check, two independently-reachable call sites, one proof.
"""

import json
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

_GRCH38_CHR1 = 248956422
_GRCH37_CHR1 = 249250621


def _vcf_with_variant(tmp, chr1_length):
    path = tmp / "in.vcf"
    path.write_text(
        "##fileformat=VCFv4.2\n"
        f"##contig=<ID=chr1,length={chr1_length}>\n"
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tSAMPLE\n"
        "chr1\t250\t.\tA\tG\t99\tPASS\t.\tGT\t0/1\n"
    )
    return path


def _gff_with_cds(tmp, name, chr1_length, cds_start, cds_end=1000):
    path = tmp / name
    exon_end = cds_end + 50
    path.write_text(
        "##gff-version 3\n"
        f"##sequence-region chr1 1 {chr1_length}\n"
        f"chr1\tRefSeq\tmRNA\t1\t{exon_end}\t.\t+\t.\tID=TX1;gene_id=GENE1;Parent=GENE1\n"
        f"chr1\tRefSeq\texon\t1\t{exon_end}\t.\t+\t.\tParent=TX1\n"
        f"chr1\tRefSeq\tCDS\t{cds_start}\t{cds_end}\t.\t+\t0\tParent=TX1\n"
    )
    return path


def _write_config(tmp, gff_path):
    path = tmp / "config.yaml"
    path.write_text(f"annotation:\n  refseq_gff: {gff_path.as_posix()}\nvep:\n  enabled: false\n")
    return path


def _run_cmd_vcf(tmp, vcf_path, config_path):
    import os

    out_dir = tmp / "out"
    # shared/ (repo root, sibling of kim_pipeline/) must be on PYTHONPATH
    # for a bare subprocess the same way the Dockerfile's own
    # `ENV PYTHONPATH=/app:${PYTHONPATH}` and this test tree's own
    # conftest.py already both do for the in-process case -- otherwise an
    # unrelated `ModuleNotFoundError: shared` (via
    # pipeline/annotation/codon_provider.py's own import of
    # shared.process_control) would surface here as a false failure that
    # has nothing to do with what this file is testing.
    env = dict(os.environ)
    repo_root = str(REPO_ROOT.parent)
    env["PYTHONPATH"] = repo_root + os.pathsep + env.get("PYTHONPATH", "")

    return subprocess.run(
        [
            sys.executable,
            "main.py",
            "vcf",
            "--input",
            str(vcf_path),
            "--output-dir",
            str(out_dir),
            "--config",
            str(config_path),
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    ), out_dir


class TheDisagreeingPairIsRefusedByCmdVcf(unittest.TestCase):
    """The live gap this file exists for: a real `kim vcf` run, driven
    end to end through the actual CLI subprocess, not a synthetic unit
    call."""

    def test_the_disagreeing_pair_is_refused(self):
        """GRCh38 VCF against a GRCh37-anchored GFF3 for the same
        gene/transcript -- exactly the pair that silently produced
        `TX1:c.51A>G` before this fix."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            vcf = _vcf_with_variant(tmp, _GRCH38_CHR1)
            gff = _gff_with_cds(tmp, "disagreeing.gff3", _GRCH37_CHR1, cds_start=200)
            cfg = _write_config(tmp, gff)

            proc, out_dir = _run_cmd_vcf(tmp, vcf, cfg)

        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        combined = proc.stdout + proc.stderr
        self.assertIn("GRCh38", combined)
        self.assertIn("GRCh37", combined)

    def test_the_agreeing_pair_still_succeeds_with_the_correct_coordinate(self):
        """THE OPPOSITE FAILURE, AND IT IS THE ONE A HASTY FIX PRODUCES:
        a check that refuses a CORRECT run. Same variant, same
        transcript, a GFF3 that actually agrees with the VCF's stated
        build -- must succeed, and must still produce the same
        `TX1:c.151A>G` this pair produced before this fix existed."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            vcf = _vcf_with_variant(tmp, _GRCH38_CHR1)
            gff = _gff_with_cds(tmp, "agreeing.gff3", _GRCH38_CHR1, cds_start=100)
            cfg = _write_config(tmp, gff)

            proc, out_dir = _run_cmd_vcf(tmp, vcf, cfg)

            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            # annotation.json (AnnotationStage's own output, written before
            # the ACMG-merge stage) carries the field this test is actually
            # about -- "hgvs" -- directly and unambiguously.
            annotation_json = out_dir / "in" / "annotation.json"
            self.assertTrue(annotation_json.exists(), proc.stdout + proc.stderr)
            data = json.loads(annotation_json.read_text())

        variants = data["variants"]
        self.assertEqual(len(variants), 1)
        self.assertEqual(variants[0].get("hgvs"), "TX1:c.151A>G")


class TheSameDisagreeingPairIsAlsoRefusedAtRunnersExistingCallSite(unittest.TestCase):
    """Parity, not duplication: `runner.py` is byte-unchanged and its own
    12 tests already prove its call site's behaviour in full. This class
    only proves (a) that call site is still THERE, and (b) the identical
    pair driven through `cmd_vcf` above is refused by the exact same
    function `runner.py` calls -- one mechanism, two independently-
    reachable call sites, closing the "two sites can drift apart" risk
    without re-deriving coverage the 12 existing tests already own."""

    def test_runner_py_call_site_is_present_and_unchanged(self):
        import inspect

        from pipeline.orchestration import runner as runner_mod

        source = inspect.getsource(runner_mod.PipelineRunner.run)
        self.assertIn(
            "check_vcf_gff3_build_consistency(filtered_vcf, _gff_for_build_check)", source
        )

    def test_the_identical_pair_is_refused_directly(self):
        import tempfile

        from pipeline.config_validator import ConfigValidationError
        from pipeline.utils.genome_build import check_vcf_gff3_build_consistency

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            vcf = _vcf_with_variant(tmp, _GRCH38_CHR1)
            gff = _gff_with_cds(tmp, "disagreeing.gff3", _GRCH37_CHR1, cds_start=200)

            with self.assertRaises(ConfigValidationError) as caught:
                check_vcf_gff3_build_consistency(str(vcf), str(gff))

        message = str(caught.exception)
        self.assertIn("GRCh38", message)
        self.assertIn("GRCh37", message)


if __name__ == "__main__":
    unittest.main()
