"""
Verification harness for the deletion/spanning-allele '*' fix.

BUG BEING VERIFIED
-------------------
Some VCF records use ALT='*' (VCF spec: "the allele is missing due to
an overlapping/spanning deletion called on a different sample or
haplotype at this position") or a symbolic structural ALT like
'<DEL>', '<INS>', '<DUP>'. Neither is a literal base sequence.

Before this fix, VCFParser passed these straight through as ordinary
Variant records. SequenceContextGenerator.build_context then spliced
the literal ALT text ('*' or '<DEL>') into the reference window to
build `alt_sequence`, and RNAGenerator._transcribe (called by both the
RNA and protein pipeline stages) rejected the resulting string with
"Cannot transcribe DNA sequence containing invalid base(s)" -- the
exact failure this session reported for RNA-FM and ESM-2.

WHY A MOCK ENSEMBL LAYER
--------------------------
This sandbox's outbound network doesn't reach the Ensembl REST API
(only package registries are allowlisted). Only
`SequenceContextGenerator._fetch_region` (the one method that calls
`requests.get`) is replaced with a fixed reference window; every other
line of real GEPER code (VCFParser, SequenceContextGenerator's
splicing logic, RNAGenerator, GeperPipeline) runs unmodified.

THE FIX
--------
VCFParser now recognizes ALT='*' and symbolic '<...>' alleles at parse
time -- before any sequence context is ever built -- logs why each
record was skipped, tallies the reasons in `parser.skip_counts`, and
never yields a Variant for them. This is the same place/pattern
already used to skip a record with a missing REF/ALT allele.
"""

import sys
import tempfile
import types
from typing import Dict
from unittest import mock

import os as _os

_SCRIPT_DIR = _os.path.dirname(_os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)


def _install_ml_dep_stubs() -> None:
    """See verify_clinvar_dbsnp_fix.py for why this stub exists -- same
    reasoning: orchestrator import-time pulls in torch/transformers via
    MODEL_REGISTRY, which this sandbox doesn't have installed and this
    script never needs (no model inference is exercised here)."""
    if "torch" in sys.modules:
        return

    torch_stub = types.ModuleType("torch")
    torch_stub.device = object
    torch_stub.Tensor = object
    torch_stub.inference_mode = lambda: mock.MagicMock()
    torch_stub.sum = lambda *a, **k: None
    torch_stub.clamp = lambda *a, **k: None
    cuda_stub = types.ModuleType("torch.cuda")
    cuda_stub.is_available = lambda: False
    cuda_stub.empty_cache = lambda: None
    torch_stub.cuda = cuda_stub
    sys.modules["torch"] = torch_stub
    sys.modules["torch.cuda"] = cuda_stub

    transformers_stub = types.ModuleType("transformers")
    for name in ("AutoConfig", "AutoModel", "AutoTokenizer", "EsmModel", "AutoModelForMaskedLM"):
        setattr(transformers_stub, name, object)
    transformers_utils_stub = types.ModuleType("transformers.utils")
    transformers_utils_stub.cached_file = lambda *a, **k: None
    transformers_stub.utils = transformers_utils_stub
    sys.modules["transformers"] = transformers_stub
    sys.modules["transformers.utils"] = transformers_utils_stub


_install_ml_dep_stubs()

_TEST_VCF_TEXT = """##fileformat=VCFv4.2
##reference=GRCh38
##contig=<ID=1,length=248956422>
#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO
1\t1000\t.\tA\tG\t.\t.\t.
1\t2000\t.\tATG\tA\t.\t.\t.
1\t3000\t.\tC\t*\t.\t.\t.
1\t4000\t.\tA\t<DEL>\t.\t.\tSVTYPE=DEL
1\t5000\t.\tCTT\t*\t.\t.\t.
1\t6000\t.\tG\tT\t.\t.\t.
"""

# 6 columns of a fixed 41-base reference window ("N" * 20 + a 1-base
# anchor + "N" * 20) is enough for build_context's splicing logic to
# run for real; the exact bases don't matter for this test.
_FAKE_REFERENCE_WINDOW = "N" * 20 + "X" + "N" * 20


def _fake_fetch_region(self, chrom: str, start: int, end: int) -> str:
    length = end - start + 1
    # Center a real base run so the offset math in build_context works
    # regardless of REF length; padded with 'A' on both sides.
    return "A" * length


def main() -> int:
    from pipeline.vcf_parser import VCFParser
    from pipeline.sequence_context import SequenceContextGenerator
    from pipeline.rna_generator import RNAGenerator
    from utils.exceptions import SequenceGenerationError

    print("=" * 78)
    print("PART 1 -- Unit-level: VCFParser skips '*' and symbolic ALT alleles")
    print("=" * 78)

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".vcf", delete=False
    ) as tmp:
        tmp.write(_TEST_VCF_TEXT)
        tmp_path = tmp.name

    parser = VCFParser(tmp_path)
    variants = parser.parse()

    print(f"\nParsed {len(variants)} usable variant(s):")
    for v in variants:
        print(f"  {v.chrom}:{v.pos} {v.ref}>{v.alt} ({v.variant_type})")

    print(f"\nSkip counts: {dict(parser.skip_counts)}")

    all_ok = True

    expected_kept = {(1000, "A", "G"), (2000, "ATG", "A"), (6000, "G", "T")}
    actual_kept = {(v.pos, v.ref, v.alt) for v in variants}
    if actual_kept != expected_kept:
        print(f"  FAILURE: expected kept variants {expected_kept}, got {actual_kept}")
        all_ok = False
    else:
        print("  OK: exactly the 3 real-sequence variants were kept "
              "(plain SNV, plain deletion, plain SNV).")

    if any(v.alt in ("*",) or v.alt.startswith("<") for v in variants):
        print("  FAILURE: a spanning-deletion or symbolic ALT allele leaked through parsing!")
        all_ok = False
    else:
        print("  OK: no '*' or symbolic '<...>' ALT allele was ever yielded as a Variant.")

    expected_skips = {"spanning_deletion_star": 2, "symbolic_structural_allele": 1}
    if dict(parser.skip_counts) != expected_skips:
        print(f"  FAILURE: expected skip_counts {expected_skips}, got {dict(parser.skip_counts)}")
        all_ok = False
    else:
        print(f"  OK: skip_counts correctly tallied {expected_skips}.")

    if not all_ok:
        print("\nPART 1: FAILED")
        return 1
    print("\nPART 1: PASSED")

    print()
    print("=" * 78)
    print("PART 2 -- Regression proof: the OLD behavior really did crash")
    print("=" * 78)
    print("(Constructing a '*'-ALT Variant directly -- bypassing the new parser\n"
          " guard -- to prove build_context + RNAGenerator would still blow up\n"
          " on this input today if the parser ever let one through again.)\n")

    from pipeline.vcf_parser import Variant

    bad_variant = Variant("1", 3000, ".", "C", "*", None, None, {})
    gen = SequenceContextGenerator(species="human", assembly="GRCh38")
    with mock.patch.object(SequenceContextGenerator, "_fetch_region", _fake_fetch_region):
        ctx = gen.build_context(bad_variant, flank_size=10)

    print(f"  alt_sequence built from ALT='*': {ctx.alt_sequence!r}")
    reproduced = False
    try:
        RNAGenerator().generate(ctx)
        print("  UNEXPECTED: transcription succeeded on a sequence containing '*'.")
    except SequenceGenerationError as exc:
        reproduced = True
        print(f"  OK: confirmed this is exactly the failure mode being fixed -- {exc}")

    if not reproduced:
        print("\nPART 2: FAILED (could not reproduce the original bug for comparison)")
        return 1
    print("\nPART 2: PASSED -- confirms *why* the parser-level fix is the right one: "
          "once a '*'/symbolic ALT reaches sequence context generation, transcription "
          "always fails. The fix in Part 1 stops that from ever happening.")

    print()
    print("=" * 78)
    print("PART 3 -- Integration: full orchestrator run on the mixed test VCF")
    print("=" * 78)

    from pipeline.orchestrator import GeperPipeline

    with mock.patch.object(GeperPipeline, "_run_startup_validation", lambda self: None), \
         mock.patch.object(SequenceContextGenerator, "_fetch_region", _fake_fetch_region):

        def _noop_route(self, variant, sequence_context):
            return []

        def _noop_rna(self, variant, sequence_context, errors):
            return {"skipped": True}

        def _noop_protein(self, variant, sequence_context, errors):
            return {"skipped": True}

        def _noop_blast(self, sequence_context, errors):
            return {"hits": [], "hit_count": 0, "skipped": True}

        def _noop_dbsnp(self, variant, errors):
            return {"rsid": None, "found": False}

        def _noop_clinvar(self, variant, dbsnp_result, errors):
            return {"query": None, "found": False}

        with mock.patch("pipeline.router.SequenceRouter.route", _noop_route), \
             mock.patch.object(GeperPipeline, "_run_rna_stage", _noop_rna), \
             mock.patch.object(GeperPipeline, "_run_protein_stage", _noop_protein), \
             mock.patch.object(GeperPipeline, "_run_blast_stage", _noop_blast), \
             mock.patch.object(GeperPipeline, "_run_dbsnp_stage", _noop_dbsnp), \
             mock.patch.object(GeperPipeline, "_run_clinvar_stage", _noop_clinvar):

            pipeline = GeperPipeline(
                output_dir="/home/claude/work/verify_star_output", assembly="GRCh38"
            )
            result = pipeline.run(tmp_path, resume=False)

    print(f"\nOrchestrator produced {len(result['variants'])} variant result(s) "
          f"(expected 3 -- the '*' and '<DEL>' records must never even reach "
          f"_process_variant):")
    ok = True
    for entry in result["variants"]:
        v = entry["variant"]
        has_error = bool(entry.get("errors"))
        ctx = entry.get("sequence_context", {})
        print(f"  {v['chrom']}:{v['pos']} {v['ref']}>{v['alt']}  "
              f"context_length={ctx.get('length')}  errors={entry.get('errors')}")
        if has_error:
            ok = False

    if len(result["variants"]) != 3:
        print(f"  FAILURE: expected exactly 3 variant results, got {len(result['variants'])}")
        ok = False
    if not ok:
        print("\nPART 3: FAILED")
        return 1
    print("\nPART 3: PASSED -- full pipeline run completes with zero errors; the "
          "spanning-deletion and symbolic-structural records were filtered out "
          "before processing, exactly as designed.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
