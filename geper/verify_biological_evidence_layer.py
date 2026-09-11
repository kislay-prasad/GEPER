# *** THE VERDICT BELOW IS UNDATED. ***
#
# WHAT THIS IS, in its own words: Verification harness for the biological evidence layer: UniProt ->
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
Verification harness for the biological evidence layer: UniProt ->
InterPro/Pfam -> AlphaFold DB.

WHY THIS EXISTS / WHAT IT DOES AND DOES NOT PROVE
----------------------------------------------------
Same sandbox network constraint documented in
`verify_alphamissense_integration.py` / `dry_run_harness.py` applies
here: this environment cannot reach `rest.uniprot.org`,
`www.ebi.ac.uk`, or `alphafold.ebi.ac.uk`.

PART 1 proves graceful degradation for real: the actual, unmodified
`CompositeUniProtProvider` / `CompositeInterProProvider` /
`CompositeAlphaFoldProvider` -> `Live API...Provider` code path is
exercised against this sandbox's real (blocked) network, with no
mocking at all, and is shown to degrade to a clean `found=False` /
`error=<network failure>` result rather than raising or hanging the
pipeline.

PART 2 proves the full parse -> aggregate -> JSON -> Markdown path
end-to-end with REAL, unmodified code, using each integration's
documented local-dataset override (`GEPER_UNIPROT_LOCAL_FILE` /
`GEPER_INTERPRO_LOCAL_FILE` / `GEPER_ALPHAFOLD_LOCAL_FILE`) as the
network stand-in -- the same technique
`verify_alphamissense_integration.py` uses (a local file standing in
for a network-hosted catalogue this sandbox cannot reach), not a mock
of GEPER's own code. The fixture data below matches each service's
real, current, publicly documented response schema.

This is not a substitute for running the pipeline with a real network
connection to the actual UniProt/InterPro/AlphaFold DB APIs; see
README.md's "Biological Evidence Layer" section for what to run
yourself to confirm that.
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pipeline.alphafold.lookup import AlphaFoldLookup
from pipeline.interpro.lookup import InterProLookup
from pipeline.uniprot.lookup import UniProtLookup
from report.json_builder import build_variant_result
from report.report_generator import ReportGenerator

PASS = "PASS"
FAIL = "FAIL"


def part1_graceful_fallback_against_real_blocked_network():
    print("=" * 78)
    print("PART 1 -- graceful fallback against this sandbox's real (blocked) network")
    print("=" * 78)

    uniprot = UniProtLookup()
    interpro = InterProLookup()
    alphafold = AlphaFoldLookup()

    uniprot_result = uniprot.query_gene("TP53")
    interpro_result = interpro.query_accession("P04637")
    alphafold_result = alphafold.query_accession("P04637")

    ok = True
    for name, result in (("UniProt", uniprot_result), ("InterPro", interpro_result), ("AlphaFold", alphafold_result)):
        no_crash = isinstance(result, dict)
        degraded = no_crash and not result.get("found") and (result.get("error") is not None or result.get("skipped"))
        print(f"  {name}: found={result.get('found')} error={result.get('error')!r} skipped={result.get('skipped')}")
        if not degraded:
            ok = False

    status = PASS if ok else FAIL
    print(
        f"\nPART 1: {status} -- every live-API path degrades to found=False/error set, no exception raised, no pipeline crash."
    )
    return ok


_UNIPROT_ENTRY = {
    "primaryAccession": "P04637",
    "uniProtkbId": "P53_HUMAN",
    "entryType": "UniProtKB reviewed (Swiss-Prot)",
    "proteinDescription": {"recommendedName": {"fullName": {"value": "Cellular tumor antigen p53"}}},
    "organism": {"scientificName": "Homo sapiens"},
    "sequence": {"length": 393},
    "comments": [
        {
            "commentType": "FUNCTION",
            "texts": [
                {"value": "Acts as a tumor suppressor in many tumor types; induces cell cycle arrest or apoptosis."}
            ],
        },
        {
            "commentType": "DISEASE",
            "disease": {
                "diseaseId": "Li-Fraumeni syndrome 1",
                "description": "An autosomal dominant cancer predisposition syndrome.",
            },
        },
    ],
    "features": [
        {"type": "Domain", "description": "DNA-binding", "location": {"start": {"value": 94}, "end": {"value": 289}}},
        {
            "type": "Region",
            "description": "Interaction with WWOX",
            "location": {"start": {"value": 66}, "end": {"value": 110}},
        },
    ],
}

_INTERPRO_PAYLOAD = {
    "results": [
        {
            "metadata": {
                "accession": "IPR002117",
                "name": "p53 tumour suppressor family",
                "type": "family",
                "source_database": "interpro",
            },
            "proteins": [{"entry_protein_locations": [{"fragments": [{"start": 94, "end": 289}]}]}],
        },
        {
            "metadata": {
                "accession": "PF00870",
                "name": "P53",
                "type": "domain",
                "source_database": "pfam",
                "integrated": "IPR002117",
            },
            "proteins": [{"entry_protein_locations": [{"fragments": [{"start": 100, "end": 280}]}]}],
        },
    ]
}


def part2_full_pipeline_with_local_dataset_fixtures():
    print()
    print("=" * 78)
    print("PART 2 -- real parse -> aggregate -> JSON -> Markdown, via local-dataset fixtures")
    print("=" * 78)

    from pipeline.alphafold.provider import CompositeAlphaFoldProvider, LocalDatasetAlphaFoldProvider
    from pipeline.interpro.provider import CompositeInterProProvider, LocalDatasetInterProProvider
    from pipeline.uniprot.provider import CompositeUniProtProvider, LocalDatasetUniProtProvider

    with tempfile.TemporaryDirectory() as tmp:
        uniprot_path = os.path.join(tmp, "uniprot.jsonl")
        with open(uniprot_path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"gene_symbol": "TP53", "entry": _UNIPROT_ENTRY}) + "\n")

        interpro_path = os.path.join(tmp, "interpro.jsonl")
        with open(interpro_path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"accession": "P04637", "payload": _INTERPRO_PAYLOAD}) + "\n")

        alphafold_path = os.path.join(tmp, "alphafold.jsonl")
        residue_plddt = {i: (95.0 if 94 <= i <= 289 else 55.0) for i in range(1, 394)}
        with open(alphafold_path, "w", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "accession": "P04637",
                        "summary": {
                            "latestVersion": 4,
                            "pdbUrl": "https://alphafold.ebi.ac.uk/files/AF-P04637-F1-model_v4.pdb",
                            "uniprotStart": 1,
                            "uniprotEnd": 393,
                        },
                        "residue_plddt": residue_plddt,
                    }
                )
                + "\n"
            )

        # Explicit dependency injection of each integration's REAL
        # `LocalDatasetXxxProvider` (not a mock of GEPER's own code) --
        # the same "local file stands in for the network catalogue"
        # technique `verify_alphamissense_integration.py` uses. CONFIG's
        # `LOCAL_DATASET_FILE` fields are read once, at process start,
        # from the environment (a plain frozen dataclass), so setting
        # `os.environ` mid-process would not retroactively affect the
        # already-constructed singleton `CONFIG` -- injecting the
        # provider directly is the correct way to point this harness at
        # the fixture files without editing any pipeline code.
        uniprot = UniProtLookup(
            provider=CompositeUniProtProvider(local_provider=LocalDatasetUniProtProvider(dataset_path=uniprot_path))
        )
        interpro = InterProLookup(
            provider=CompositeInterProProvider(local_provider=LocalDatasetInterProProvider(dataset_path=interpro_path))
        )
        alphafold = AlphaFoldLookup(
            provider=CompositeAlphaFoldProvider(
                local_provider=LocalDatasetAlphaFoldProvider(dataset_path=alphafold_path)
            )
        )

        uniprot_result = uniprot.query_gene("TP53")
        protein_position = (
            150  # falls within both the InterPro domain (94-289) and the "high confidence" residue band above
        )
        interpro_result = interpro.query_variant(uniprot_result, protein_position=protein_position)
        alphafold_result = alphafold.query_variant(uniprot_result, protein_position=protein_position)

    checks = {
        "UniProt found reviewed entry": uniprot_result.get("found") and uniprot_result.get("reviewed"),
        "UniProt function text present": bool(uniprot_result.get("function")),
        "UniProt disease comment present": bool(uniprot_result.get("disease_comments")),
        "InterPro domains found": interpro_result.get("found") and len(interpro_result.get("domains", [])) == 2,
        "InterPro affected_domains populated at position 150": len(interpro_result.get("affected_domains", [])) == 2,
        "AlphaFold mean pLDDT computed": alphafold_result.get("mean_plddt") is not None,
        "AlphaFold residue-level pLDDT at position 150": alphafold_result.get("affected_residue_plddt") == 95.0,
        "AlphaFold residue band is very_high": alphafold_result.get("affected_residue_band") == "very_high",
    }
    for label, passed in checks.items():
        print(f"  [{'OK' if passed else 'MISSING'}] {label}")

    # Build the real per-variant JSON result the orchestrator itself builds,
    # then render it through the real Markdown report generator.
    variant_result = build_variant_result(
        variant_dict={"chrom": "17", "pos": 7676154, "ref": "G", "alt": "A", "id": "."},
        sequence_context={"skipped": True},
        dna_model_results={"skipped": True},
        rna_result={"skipped": True},
        protein_result={"skipped": True},
        blast_result={"skipped": True},
        clinvar_result={"found": False},
        dbsnp_result={"found": False},
        interpretation={},
        errors=[],
        uniprot_result=uniprot_result,
        interpro_result=interpro_result,
        alphafold_result=alphafold_result,
    )
    json_ok = all(k in variant_result for k in ("uniprot", "interpro", "alphafold"))
    print(f"  [{'OK' if json_ok else 'MISSING'}] build_variant_result includes uniprot/interpro/alphafold keys")

    report_lines = []
    report_lines.extend(ReportGenerator._render_uniprot(variant_result["uniprot"]))
    report_lines.extend(ReportGenerator._render_interpro(variant_result["interpro"]))
    report_lines.extend(ReportGenerator._render_alphafold(variant_result["alphafold"]))
    markdown = "\n".join(report_lines)
    markdown_ok = (
        "UniProt (Protein Annotation)" in markdown and "InterPro / Pfam" in markdown and "AlphaFold DB" in markdown
    )
    print(f"  [{'OK' if markdown_ok else 'MISSING'}] Markdown report renders all three new sections")
    print()
    print(markdown)

    all_ok = all(checks.values()) and json_ok and markdown_ok
    status = PASS if all_ok else FAIL
    print(f"\nPART 2: {status}")
    return all_ok


if __name__ == "__main__":
    ok1 = part1_graceful_fallback_against_real_blocked_network()
    ok2 = part2_full_pipeline_with_local_dataset_fixtures()

    print()
    print("=" * 78)
    overall = PASS if (ok1 and ok2) else FAIL
    print(f"OVERALL: {overall}")
    print("=" * 78)
    sys.exit(0 if (ok1 and ok2) else 1)
