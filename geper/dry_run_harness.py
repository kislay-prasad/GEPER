# *** THE VERDICT BELOW IS UNDATED. ***
#
# WHAT THIS IS, in its own words: Dry-run integration harness for GEPER
#
# NOTHING RE-RUNS THIS FILE. Measured 2026-09-11 across the 17 files matching
# geper/verify_*.py, geper/benchmark_*.py and dry_run_harness.py: ZERO are
# referenced in .github/workflows, and pytest does not collect any of them,
# because they are not named test_*. So whatever this script printed, it
# printed on the day somebody ran it by hand -- AND WHICH DAY THAT WAS IS
# RECORDED NOWHERE. `git log` on this file gives the date it was EDITED,
# which is a different fact and must not be quoted as if it were this one.
#
# WHY THE NOTE RATHER THAN A FIX: the file is not broken. Its stubs are
# honest -- it fakes everything EXCEPT the thing it verifies, and it
# propagates its exit code -- and all 59 stub targets across these harnesses
# were still defined when this was written, so they can all still be applied.
# The risk is CITATION: 'verify' is in the filename, which invites someone to
# quote this file's green as evidence. That is the `docker history` shape --
# something that reads as a record and is not one.
#
# IF YOU ARE ABOUT TO CITE THIS FILE: run it, and say when you ran it.
#
"""
Dry-run integration harness for GEPER.

WHY THIS EXISTS
----------------
This sandbox's outbound network access is limited to package registries
(pypi.org, github.com, npmjs.com, etc). It does NOT include
huggingface.co, rest.ensembl.org, eutils.ncbi.nlm.nih.gov, or NCBI
BLAST -- all of which the real pipeline needs for actual model weight
downloads and sequence/annotation lookups. A genuine end-to-end
`python main.py --vcf ...` run cannot complete in this environment for
that reason, independent of the HyenaDNA/RNA-FM bug this change fixes.

This script exercises the REAL orchestrator, router, and dependency
availability/auto-install code (the code these fixes touch) unmodified
-- including the real Biopython auto-install and import in
BLASTClient -- and stubs out ONLY the actual network calls this
sandbox cannot reach (Ensembl sequence lookup, the NCBI qblast BLAST
submission, ClinVar, dbSNP, and each model's `_load_impl`/`_infer_impl`
weight loading + forward pass) with lightweight local fakes. This
proves the *integration* is fixed -- HyenaDNA/RNA-FM/Biopython are no
longer reported "missing / not installed" and the run summary shows 3
successful / 0 failed -- without requiring internet access this
sandbox does not have.

This is NOT a substitute for running the real command with real model
weights; see the accompanying summary for what to run yourself to get
that final confirmation.
"""

import sys

import os as _os

# Resolve relative to this file's own directory, not the process's
# current working directory -- "." only works when the script happens
# to be launched with cwd == the geper/ project root (breaks under
# Colab cells, `%run`, or `python -m` invoked from elsewhere).
_SCRIPT_DIR = _os.path.dirname(_os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from unittest import mock  # noqa: E402

from models import MODEL_REGISTRY  # noqa: E402
from pipeline.sequence_context import SequenceContext  # noqa: E402


def _fake_load_impl(self):
    self.tokenizer = "fake-tokenizer"
    self.model = "fake-model"


def _fake_infer_impl(self, sequence, **kwargs):
    return {
        "embedding_mean": [0.0] * 8,
        "embedding_dim": 8,
        "num_tokens": max(1, len(sequence) // 4),
    }


def _fake_verify_materialized(self):
    return None


def main():
    patches = []
    for model_cls in MODEL_REGISTRY.values():
        patches.append(mock.patch.object(model_cls, "_load_impl", _fake_load_impl))
        patches.append(mock.patch.object(model_cls, "_infer_impl", _fake_infer_impl))

    from models.base_model import BaseGenomicModel

    patches.append(mock.patch.object(BaseGenomicModel, "_verify_materialized", _fake_verify_materialized))

    from pipeline.sequence_context import SequenceContextGenerator
    from database.blast_client import BLASTClient
    from database.clinvar_client import ClinVarClient
    from database.dbsnp_client import DbSNPClient

    def _fake_build_context(self, variant, flank_size=None):
        flank = flank_size if flank_size is not None else 500
        ref_seq = "ACGT" * 50
        alt_seq = ref_seq  # not biologically exact; irrelevant for a plumbing test
        return SequenceContext(
            chrom=variant.chrom,
            window_start=max(1, variant.pos - flank),
            window_end=variant.pos + flank,
            flank_size=flank,
            ref_sequence=ref_seq,
            alt_sequence=alt_seq,
            variant_offset=flank,
        )

    patches.append(mock.patch.object(SequenceContextGenerator, "build_context", _fake_build_context))
    patches.append(
        mock.patch.object(
            BLASTClient,
            "_search_remote",
            lambda self, seq, program, database, max_hits: {
                "mode": "remote",
                "database": database,
                "hits": [],
                "hit_count": 0,
            },
        )
    )
    patches.append(
        mock.patch.object(
            ClinVarClient,
            "query_variant",
            lambda self, variant, rsid=None, assembly=None: {"query": None, "found": False},
        )
    )
    patches.append(
        mock.patch.object(
            DbSNPClient,
            "lookup_variant",
            lambda self, variant, assembly=None: {"rsid": None, "found": False},
        )
    )

    for p in patches:
        p.start()
    try:
        import main as geper_main

        sys.argv = [
            "main.py",
            "--vcf",
            "/home/claude/work/testdata/known_variants_grch37.vcf",
            "--max-variants",
            "3",
            "--output-dir",
            "/home/claude/work/dry_run_output",
        ]
        return geper_main.main()
    finally:
        for p in patches:
            p.stop()


if __name__ == "__main__":
    sys.exit(main())
