"""
Extracts an offline-evidence fixture from a real, verified
`geper_results.json`.

WHY THIS EXISTS
----------------
Round 10 (`tests/test_acmg_net_points.py`) proved `_combine`'s
arithmetic against the five real, Colab-verified nuclear variants in
`test_data/nuclear_test_with_mt.vcf` -- but did so by hand-copying
*which ACMG criteria triggered* for each variant into
`_REAL_RUN_CASES`, rather than deriving that from the real evidence.
That test could never fail if a per-criterion rule method (`_pm2`,
`_pvs1`, ...) changed behavior, because the "triggered" list was typed
by a human, not computed -- the exact class of bug round 8 shipped
once already (a hand-copied dict shape production never produced,
caught only by a Colab diff).

This script closes that gap. `InterpretationEngine.interpret()`
(`pipeline/interpretation.py`) and `ACMGRuleEngine.evaluate()`
(`pipeline/acmg_rules.py`) are pure functions of plain dicts -- no
model instance, no network client, ever appears in their call graph.
Every one of those input dicts is *also* independently serialized
verbatim as a top-level per-variant key in `geper_results.json` (see
`report/json_builder.py::build_variant_result`), so a real, already-
verified run's output already contains everything needed to replay
`interpret()`/`build_variant_result()` for real, offline, with no
network and no model weights -- this script just re-keys those top-
level fields to the kwarg names `interpret()`/`build_variant_result()`
expect.

WHAT THIS DOES NOT COVER
-------------------------
A variant that took GEPER's mitochondrial out-of-scope early exit
(`pipeline/orchestrator.py::_process_variant`, `is_mitochondrial_chrom`
branch) never reached `interpret()` at all -- its record has an
`out_of_scope` key and no `interpretation` key, so there is nothing to
extract. Such variants are skipped here and reported at the end; they
are already covered by `tests/test_mitochondrial_out_of_scope.py`.

This fixture also cannot cover a *change to an evidence-gathering
stage itself* (ClinVar/gnomAD/Ensembl/model-inference code) -- only a
fresh real run can re-derive what those stages should have returned in
the first place. See `tests/test_acmg_net_points.py`'s module
docstring for this same scope boundary stated at the test-file level.

USAGE
-----
    python extract_fixture.py \\
        --source /path/to/geper_results.json \\
        --output nuclear_test_with_mt_evidence.json \\
        --commit 3c854c9 \\
        --run-id GEPER-RUN-20260813

See README.md in this directory for the full regeneration procedure
and current fixture provenance.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple


class SourceMismatchError(RuntimeError):
    """
    Raised when `--source` doesn't look like the run it was claimed to
    be -- e.g. `--vcf-name nuclear_test_with_mt.vcf` pointed at a
    `geper_results.json` from some other, unrelated run.

    Real incident this guards against (round 11): the first file
    tried against this script was an older, unrelated 2-variant
    chr1/chr11 run with no `acmg_net_points` at all. Nothing before
    this check stopped that file from extracting "successfully" --
    it would have silently produced a fixture that looks superficially
    fine (valid JSON, non-empty `variants`) but tests the wrong thing
    forever, passing green regardless of what the ACMG engine actually
    does. This is exactly the class of silent-drift risk the fixture
    mechanism as a whole exists to close (see this script's module
    docstring and `loader.py`'s schema guard) -- so a source/target
    mismatch must fail LOUDLY here too, not just at the schema layer.
    """


class IncompleteAcmgResultError(RuntimeError):
    """
    Raised when a variant that should have gone through the real ACMG
    engine (i.e. not `out_of_scope`) has no usable ACMG evaluation --
    e.g. no `acmg_evaluation.classification` at all. A run from before
    the ACMG engine existed, or a run where `interpret()` raised for
    every variant, would otherwise extract "successfully" into a
    fixture with an `expected` block silently full of `None`s -- see
    `SourceMismatchError`'s docstring for why that must be a hard
    failure here instead of a quietly wrong fixture.
    """


# Top-level `geper_results.json` per-variant key -> the kwarg name
# `InterpretationEngine.interpret()` / `report.json_builder.build_variant_result()`
# expect it under. Verified against `report/json_builder.py::build_variant_result`
# (the function that produces these top-level keys in the first place)
# and `pipeline/interpretation.py::InterpretationEngine.interpret`'s
# signature (the function that consumes them) -- see this script's
# module docstring.
_RESULT_KEY_TO_EVIDENCE_KWARG = {
    "sequence_context": "sequence_context",
    "dna_model_results": "dna_model_results",
    "rna_analysis": "rna_result",
    "protein_analysis": "protein_result",
    "blast": "blast_result",
    "clinvar": "clinvar_result",
    "dbsnp": "dbsnp_result",
    "alphamissense": "alphamissense_result",
    "mmsplice": "mmsplice_result",
    "gnomad": "gnomad_result",
    "indigenomes": "indigenomes_result",
    "thousand_genomes_sas": "thousand_genomes_sas_result",
    "conservation": "conservation_result",
    "clingen": "clingen_result",
    "uniprot": "uniprot_result",
    "interpro": "interpro_result",
    "alphafold": "alphafold_result",
    "transcript": "transcript_result",
    "clinvar_codon_matches": "clinvar_codon_result",
    "hpo": "hpo_result",
    "orphanet": "orphanet_result",
    "functional_evidence": "functional_evidence_result",
    "normalization": "normalization_result",
    "spliceformer": "spliceformer_result",
    "splicebert": "splicebert_result",
    "ai_splicing_ensemble": "ensemble_result",
    "ai_model_status": "ai_model_status",
    "errors": "errors",
}

# `phenotype_result` is a RUN-level input (from --hpo-terms/
# --phenotype-file), never serialized per-variant in geper_results.json
# -- see `pipeline/orchestrator.py::GeperPipeline.run`, `self.phenotype_result`.
# The nuclear_test_with_mt.vcf run this fixture was captured from used a
# bare `--vcf` invocation (no HPO flags -- confirmed against
# GEPER-RUN-20260813's recorded command), so it is always `None` here.
# A future fixture captured from a run that DID pass --hpo-terms/
# --phenotype-file would need to record the actual phenotype_result
# dict separately; this script does not attempt to reconstruct it.
_PHENOTYPE_RESULT = None


def _label_for(variant_record: Dict[str, Any]) -> str:
    """Human-readable fixture key: '<gene>_<pos>' if a gene symbol was
    resolved, else '<chrom>_<pos>'. Cosmetic only -- never read for
    correctness, only for a readable diff/error message."""
    v = variant_record.get("variant") or {}
    gene = (variant_record.get("clingen") or {}).get("gene_symbol")
    if gene:
        return f"{gene}_{v.get('pos')}"
    return f"{v.get('chrom')}_{v.get('pos')}"


def _extract_variant(variant_record: Dict[str, Any], label: str) -> Optional[Dict[str, Any]]:
    if "out_of_scope" in variant_record:
        return None  # never reached interpret() -- nothing to extract, see module docstring

    evidence: Dict[str, Any] = {"phenotype_result": _PHENOTYPE_RESULT}
    for result_key, kwarg_name in _RESULT_KEY_TO_EVIDENCE_KWARG.items():
        evidence[kwarg_name] = variant_record.get(result_key)

    evidence["variant_dict"] = variant_record.get("variant")
    evidence["dna_models_used"] = sorted((variant_record.get("dna_model_results") or {}).keys())

    # Expected outputs, read verbatim from the same verified record --
    # not hand-typed, so a transcription error here can't silently
    # create a wrong "known-correct" target for the fixture's own
    # consumer tests to chase.
    interpretation = variant_record.get("interpretation") or {}
    acmg_evaluation = interpretation.get("acmg_evaluation") or {}
    classification = acmg_evaluation.get("classification")
    net_points = acmg_evaluation.get("net_points")
    # `net_points` is legitimately `None` only on the BA1 stand-alone-
    # benign short-circuit (`_combine`'s own documented behavior --
    # see `pipeline/acmg_rules.py::CombineResult`), which is always
    # classification == "Benign". Any OTHER combination of a missing
    # classification or a None net_points on a non-Benign call means
    # this record never went through a real ACMG evaluation at all --
    # see `IncompleteAcmgResultError`'s docstring.
    if classification is None or (net_points is None and classification != "Benign"):
        raise IncompleteAcmgResultError(
            f"variant '{label}' has no usable ACMG evaluation "
            f"(classification={classification!r}, net_points={net_points!r}) -- "
            "this source does not look like a real ACMG-engine run; refusing to "
            "extract a fixture from it. See IncompleteAcmgResultError's docstring."
        )
    expected = {
        "acmg_net_points": net_points,
        "acmg_pathogenic_points": acmg_evaluation.get("pathogenic_points"),
        "acmg_benign_points": acmg_evaluation.get("benign_points"),
        "acmg_classification": classification,
    }

    return {"evidence": evidence, "expected": expected}


def _validate_source(source: Dict[str, Any], expected_vcf_name: str) -> None:
    """
    Refuses to extract from a source that doesn't look like the run it
    was claimed to be. See `SourceMismatchError`'s docstring for the
    real incident this closes.
    """
    input_vcf = source.get("input_vcf")
    if not input_vcf:
        raise SourceMismatchError(
            "source has no 'input_vcf' field at all -- this doesn't look like a "
            "real geper_results.json (JSONResultBuilder.build() always sets it); "
            "refusing to extract."
        )
    actual_name = os.path.basename(str(input_vcf))
    if actual_name != expected_vcf_name:
        raise SourceMismatchError(
            f"source was run against '{actual_name}', not the expected "
            f"'{expected_vcf_name}' (pass --vcf-name to override if this is "
            "intentional -- e.g. capturing a fixture from a different VCF). "
            "Refusing to extract from a run that doesn't match what was asked for."
        )
    variants = source.get("variants", [])
    declared_count = source.get("variant_count")
    if declared_count is not None and declared_count != len(variants):
        raise SourceMismatchError(
            f"source declares variant_count={declared_count} but its own "
            f"'variants' list has {len(variants)} entries -- internally "
            "inconsistent; refusing to trust this file."
        )


def extract(source: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    fixture: Dict[str, Any] = {}
    skipped: List[str] = []
    for variant_record in source.get("variants", []):
        label = _label_for(variant_record)
        entry = _extract_variant(variant_record, label)
        if entry is None:
            skipped.append(label)
            continue
        fixture[label] = entry
    return fixture, skipped


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", required=True, help="Path to a real, already-run geper_results.json.")
    parser.add_argument("--output", required=True, help="Path to write the extracted evidence fixture JSON to.")
    parser.add_argument("--commit", required=True, help="Git commit SHA the source run was produced at.")
    parser.add_argument("--run-id", required=True, help="The run's own identifier (e.g. GEPER-RUN-20260813).")
    parser.add_argument(
        "--vcf-name",
        default="nuclear_test_with_mt.vcf",
        help="Basename of the input VCF the source run was given (default: nuclear_test_with_mt.vcf).",
    )
    args = parser.parse_args(argv)

    with open(args.source, "r", encoding="utf-8") as fh:
        source = json.load(fh)

    try:
        _validate_source(source, args.vcf_name)
        variants, skipped = extract(source)
    except (SourceMismatchError, IncompleteAcmgResultError) as exc:
        print(f"Refusing to extract a fixture from '{args.source}': {exc}", file=sys.stderr)
        return 1

    if not variants:
        print("No usable (non-out-of-scope) variants found in source; nothing written.", file=sys.stderr)
        return 1

    document = {
        "_provenance": {
            "extracted_at": datetime.now(timezone.utc).isoformat(),
            "source_commit": args.commit,
            "source_run_id": args.run_id,
            "source_vcf": args.vcf_name,
            "note": (
                "Every 'evidence' dict below is copied verbatim from a real, network- and "
                "model-backed GEPER run's own geper_results.json -- never hand-authored. "
                "'expected' values are copied from that same verified run's own output, not "
                "independently re-derived. See README.md in this directory for how to "
                "regenerate this file and confirm it is still current."
            ),
        },
        "variants": variants,
    }

    with open(args.output, "w", encoding="utf-8") as fh:
        json.dump(document, fh, indent=2, sort_keys=True)

    print(f"Wrote {len(variants)} variant(s) to '{args.output}'.")
    if skipped:
        print(f"Skipped {len(skipped)} out-of-scope variant(s) (never reached interpret()): {', '.join(skipped)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
