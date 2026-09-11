# *** THE VERDICT BELOW IS UNDATED. ***
#
# WHAT THIS IS, in its own words: End-to-end verification for the report-consistency fixes:
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
End-to-end verification for the report-consistency fixes:
  1. `report/clinical_report_builder.py` / `report/json_builder.py` --
     the "Clinical Interpretation Report" section must never contradict
     the "Annotation Detail" audit trail for the same variant (the
     UniProt "no entry resolved" bug found via a real Colab run).
  2. `pipeline/interpro/provider.py` -- InterPro's real HTTP 204
     No Content response for a valid, domain-less accession must
     resolve immediately, not retry 3 times and report a false
     failure.

WHY THIS EXISTS / WHAT IT DOES AND DOES NOT PROVE
----------------------------------------------------
Unlike the original `dry_run_harness.py` (written when this
environment's outbound network access was limited to package
registries), this environment can now reach every evidence-source API
GEPER integrates -- Ensembl, NCBI E-utilities, gnomAD, ClinGen,
ClinGen Evidence Repository, UniProt, InterPro, AlphaFold DB, HPO,
Orphanet, MaveDB -- confirmed individually with real HTTP 200s
immediately before writing this script. This harness therefore runs
the REAL, unmodified evidence-source clients against real network
responses for real, well-known variants (`testdata/known_variants_grch37.vcf`:
F5 rs6025 / Factor V Leiden, HBB rs334 / sickle-cell HbS, ALDH2 rs671
-- the same fixture `verify_alphamissense_integration.py` and
`verify_blast_optimization.py` already trust). Only the genuinely
heavy/slow pieces are faked, exactly as `dry_run_harness.py` already
established: each foundation model's `_load_impl`/`_infer_impl` (real
weights are multi-GB HuggingFace downloads, irrelevant to what this
script verifies) and the sequence-context/remote-BLAST calls (real
GRCh37 Ensembl sequence retrieval works, but isn't what's under test
here and would add an unrelated dependency on exact reference-genome
matching).

This proves: the full `GeperPipeline.run()` completes successfully
end to end with the fixes in place, real database lookups execute (as
opposed to synthetic fixtures), the generated JSON/Markdown report
never contradicts itself between its summary and audit-trail sections,
and a real InterPro accession that previously triggered "lookup failed
after 3 attempts" now resolves cleanly. It is not a substitute for a
full Colab run with real model weights -- see README.md for that.
"""

import os as _os
import sys

_SCRIPT_DIR = _os.path.dirname(_os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

# Sandbox robustness only (see _fake_heavy_deps.py's own docstring): if
# this environment's local torch/transformers install is missing or
# broken, stub them so the model-registry imports below don't fail
# before this script even reaches its own, real, unmocked evidence-
# source code paths. A no-op when a real, working torch is installed.
import _fake_heavy_deps  # noqa: E402

_fake_heavy_deps.install()

from report.clinical_report_builder import candidate_interpretation_of
from unittest import mock

_OUTPUT_DIR = _os.path.join(_SCRIPT_DIR, "_verify_report_fixes_e2e_output")

_PASS = "PASS"
_FAIL = "FAIL"


def _check(label: str, condition: bool, detail: str = "") -> bool:
    status = _PASS if condition else _FAIL
    print(f"[{status}] {label}" + (f" -- {detail}" if detail else ""))
    return condition


def _install_heavy_model_and_sequence_fakes():
    """Exactly `dry_run_harness.py`'s existing, established fakes --
    reused verbatim so this script's only real difference is leaving
    every evidence-source client (ClinVar, dbSNP, gnomAD, ClinGen,
    UniProt, InterPro, AlphaFold, HPO, Orphanet) real and unfaked."""
    from models import MODEL_REGISTRY
    from models.base_model import BaseGenomicModel
    from pipeline.sequence_context import SequenceContext, SequenceContextGenerator
    from database.blast_client import BLASTClient

    patches = []

    def _fake_load_impl(self):
        self.tokenizer = "fake-tokenizer"
        self.model = "fake-model"

    def _fake_infer_impl(self, sequence, **kwargs):
        return {"embedding_mean": [0.0] * 8, "embedding_dim": 8, "num_tokens": max(1, len(sequence) // 4)}

    def _fake_verify_materialized(self):
        return None

    def _fake_build_context(self, variant, flank_size=None):
        flank = flank_size if flank_size is not None else 500
        ref_seq = "ACGT" * 50
        return SequenceContext(
            chrom=variant.chrom,
            window_start=max(1, variant.pos - flank),
            window_end=variant.pos + flank,
            flank_size=flank,
            ref_sequence=ref_seq,
            alt_sequence=ref_seq,
            variant_offset=flank,
        )

    for model_cls in MODEL_REGISTRY.values():
        patches.append(mock.patch.object(model_cls, "_load_impl", _fake_load_impl))
        patches.append(mock.patch.object(model_cls, "_infer_impl", _fake_infer_impl))
    patches.append(mock.patch.object(BaseGenomicModel, "_verify_materialized", _fake_verify_materialized))
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
    return patches


def main() -> int:
    results = []

    print("=" * 70)
    print("PART 1: real InterPro accession that previously failed after 3 attempts")
    print("=" * 70)
    from pipeline.interpro.provider import LiveAPIInterProProvider

    interpro_result = LiveAPIInterProProvider().query("A0A3G1DJQ2")
    results.append(
        _check(
            "InterPro query completes without error", interpro_result.error is None, detail=str(interpro_result.error)
        )
    )
    results.append(
        _check(
            "InterPro reports a definitive result (found=False, zero domains -- real biology, not a failure)",
            interpro_result.found is False and interpro_result.domains == [],
        )
    )

    print()
    print("=" * 70)
    print("PART 2: full GeperPipeline.run() end to end, real evidence-source lookups")
    print("=" * 70)
    # Scoped down for a fast, bounded verification run: the first
    # attempt at this script took 40+ minutes, not because anything
    # hung, but because it forgot to fake the *plugin* model family
    # (Enformer/Borzoi/SpliceFormer/SpliceBERT -- a separate registry
    # from MODEL_REGISTRY, loaded through ModelManager/EnsembleManager)
    # and so genuinely downloaded/loaded multi-GB real weights, on top
    # of one real (bounded, 3-retry, 30s-timeout) Ensembl hiccup. None
    # of that is what this script verifies -- disabling those plugins
    # and limiting to 1 variant keeps every real evidence-source lookup
    # this script actually cares about (ClinVar, dbSNP, gnomAD,
    # ClinGen, HPO, Orphanet, UniProt, InterPro, AlphaFold) real and
    # unfaked, while cutting the runtime from 40+ minutes to under 2.
    _os.environ["GEPER_ENABLE_ENFORMER"] = "false"
    _os.environ["GEPER_ENABLE_BORZOI"] = "false"
    _os.environ["GEPER_ENABLE_SPLICEFORMER"] = "false"
    _os.environ["GEPER_ENABLE_SPLICEBERT"] = "false"

    patches = _install_heavy_model_and_sequence_fakes()
    for p in patches:
        p.start()
    try:
        from pipeline.orchestrator import GeperPipeline

        vcf_path = _os.path.join(_SCRIPT_DIR, "testdata", "known_variants_grch37.vcf")
        pipeline = GeperPipeline(output_dir=_OUTPUT_DIR, enable_profiling=False, assembly="GRCh37")
        json_document = pipeline.run(vcf_path, max_variants=1, resume=False)
    finally:
        for p in patches:
            p.stop()

    results.append(_check("Pipeline run completed and returned a JSON document", json_document is not None))
    variants = json_document.get("variants", [])
    results.append(_check("Variant(s) processed", len(variants) == 1, detail=f"got {len(variants)}"))

    print()
    print("=" * 70)
    print("PART 3: clinical_report never contradicts the Annotation Detail trail")
    print("=" * 70)
    report_path = _os.path.join(_OUTPUT_DIR, "geper_report.md")
    with open(report_path, "r", encoding="utf-8") as fh:
        markdown = fh.read()

    for i, vr in enumerate(variants, start=1):
        cr = candidate_interpretation_of(vr) or {}
        prot = cr.get("protein_knowledge", {})
        uniprot_top = vr.get("uniprot") or {}
        interpro_top = vr.get("interpro") or {}

        # The exact class of bug found in Colab: clinical_report saying
        # "not resolved" while the raw stage output (same JSON document)
        # says otherwise.
        if uniprot_top.get("found"):
            results.append(
                _check(
                    f"Variant {i}: clinical_report agrees UniProt resolved ({uniprot_top.get('accession')})",
                    prot.get("uniprot_available") is True,
                )
            )
        else:
            results.append(
                _check(
                    f"Variant {i}: clinical_report agrees UniProt did not resolve",
                    prot.get("uniprot_available") is False,
                )
            )

        if interpro_top.get("found"):
            results.append(
                _check(
                    f"Variant {i}: clinical_report agrees InterPro found domain(s)",
                    prot.get("interpro_available") is True,
                )
            )

        gnomad_top = vr.get("gnomad") or {}
        pop = cr.get("population_evidence", {}).get("gnomad", {})
        results.append(
            _check(
                f"Variant {i}: population_evidence.gnomad.found matches top-level gnomad.found",
                pop.get("found") == bool(gnomad_top.get("found")),
            )
        )

        clinvar_top = vr.get("clinvar") or {}
        clin = cr.get("clinical_evidence", {})
        top_has_records = bool(clinvar_top.get("records"))
        results.append(
            _check(
                f"Variant {i}: clinical_evidence.clinvar_available matches top-level clinvar records",
                clin.get("clinvar_available") == top_has_records,
            )
        )

    results.append(
        _check(
            "Markdown report contains no literal 'no entry resolved' contradiction of a resolved UniProt entry",
            not any(v.get("uniprot", {}).get("found") for v in variants) or "no entry resolved" not in markdown,
        )
    )

    print()
    print("=" * 70)
    print("PART 4: ACMG classification, AI models, and evidence sources ran")
    print("=" * 70)
    for i, vr in enumerate(variants, start=1):
        interp = vr.get("interpretation", {})
        acmg = interp.get("acmg_evaluation", {})
        results.append(
            _check(
                f"Variant {i}: ACMG classification produced",
                acmg.get("classification") is not None,
                detail=str(acmg.get("classification")),
            )
        )
        ai_status = vr.get("ai_model_status", {})
        results.append(_check(f"Variant {i}: AI model status table populated", bool(ai_status)))

    print()
    print("=" * 70)
    passed = sum(1 for r in results if r)
    total = len(results)
    print(f"SUMMARY: {passed}/{total} checks passed")
    print(f"Full report: {report_path}")
    print(f"Full JSON:   {_os.path.join(_OUTPUT_DIR, 'geper_results.json')}")
    print("=" * 70)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
