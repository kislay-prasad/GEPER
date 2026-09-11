# *** THE VERDICT BELOW IS UNDATED. ***
#
# WHAT THIS IS, in its own words: Verification harness for the ClinVar / dbSNP genome-build fix
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
Verification harness for the ClinVar / dbSNP genome-build fix.

WHY A MOCK HTTP LAYER
----------------------
This sandbox's outbound network does not reach eutils.ncbi.nlm.nih.gov
(only package registries are allowlisted), so a genuine live call
can't be made here. Everything EXCEPT the raw HTTP request is real:
the actual ClinVarClient/DbSNPClient query-building code, the actual
GeperPipeline orchestrator stages that thread the resolved genome
build through to them, and the actual VCFParser + assembly_validator
preflight that detects GRCh37 from the test VCF's header.

Only `_request_json` (the one method that calls `requests.get`) is
replaced, with a fake NCBI responder that mimics the real, documented
behavior of these two Entrez endpoints:

  * dbSNP: the bare `POSITION` field is GRCh38-only; GRCh37 coordinates
    must use `POSITION_GRCH37` instead. A GRCh37 position submitted
    through `POSITION` does NOT coincidentally match some other real
    dbSNP record at that exact chrom/pos combination for these test
    variants (GRCh37 vs GRCh38 coordinates differ by tens of thousands
    of bases for chr1/11/12), so the old code path is modeled as a
    genuine miss, matching what would happen against the live API.
  * ClinVar: same idea with `chrpos37` vs `chrpos38`.

The three variants below are real, extensively documented ClinVar +
dbSNP entries (sources checked via web search before writing this
script):
  * 1:169519049 T>C  = rs6025  (F5 c.1601G>A, Factor V Leiden)
  * 11:5248232  T>A  = rs334   (HBB c.20A>T, sickle cell / HbS)
  * 12:112241766 G>A = rs671   (ALDH2 c.1510G>A, ALDH2*2)
All three GRCh37 positions/alleles are taken directly from NCBI
ClinVar variant pages (accessed via web search).
"""

import sys
import types
from typing import Any, Dict
from unittest import mock

import os as _os

# Resolve relative to this file's own directory, not the process's
# current working directory -- "." only works when the script happens
# to be launched with cwd == the geper/ project root (breaks under
# Colab cells, `%run`, or `python -m` invoked from elsewhere).
_SCRIPT_DIR = _os.path.dirname(_os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)


def _install_ml_dep_stubs() -> None:
    """
    pipeline.orchestrator imports models.MODEL_REGISTRY at module load,
    which transitively imports torch and transformers in all 5 model
    wrapper files. This sandbox doesn't have those installed (and
    doesn't need them for this audit -- no model code is invoked by
    this script). Stub just enough of each module's surface for the
    *imports* to succeed; every attribute here is either a plain class
    used only in type annotations, or a name that's only ever called
    inside a model's `_load_impl`/`_infer_impl`, which this script
    never exercises.
    """
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

# ---------------------------------------------------------------------
# Fake NCBI data, keyed by (db, exact Entrez term).
# ---------------------------------------------------------------------

_DBSNP_HITS = {
    "1[CHR] AND 169519049[POSITION_GRCH37]": ["6025"],
    "11[CHR] AND 5248232[POSITION_GRCH37]": ["334"],
    "12[CHR] AND 112241766[POSITION_GRCH37]": ["671"],
}

_CLINVAR_HITS = {
    "1[chr] AND 169519049[chrpos37]": ["12564"],
    "11[chr] AND 5248232[chrpos37]": ["15333"],
    "12[chr] AND 112241766[chrpos37]": ["18396"],
    # The orchestrator prefers an rsID (from a successful dbSNP lookup)
    # over a positional search when querying ClinVar -- see
    # ClinVarClient._build_search_term. These entries cover that path.
    "rs6025[rs]": ["12564"],
    "rs334[rs]": ["15333"],
    "rs671[rs]": ["18396"],
}

_CLINVAR_SUMMARY = {
    "12564": {
        "title": "NM_000130.4(F5):c.1601G>A (p.Arg534Gln)",
        "germline_classification": {
            "description": "Pathogenic",
            "review_status": "criteria provided, multiple submitters, no conflicts",
            "trait_set": [{"trait_name": "Thrombophilia due to factor V Leiden"}],
        },
        "accession": "VCV000012564",
    },
    "15333": {
        "title": "NM_000518.5(HBB):c.20A>T (p.Glu7Val)",
        "germline_classification": {
            "description": "Pathogenic",
            "review_status": "reviewed by expert panel",
            "trait_set": [{"trait_name": "Sickle cell disease and related diseases"}],
        },
        "accession": "VCV000015333",
    },
    "18396": {
        "title": "NM_000690.4(ALDH2):c.1510G>A (p.Glu504Lys)",
        "germline_classification": {
            "description": "drug response",
            "review_status": "criteria provided, single submitter",
            "trait_set": [{"trait_name": "Alcohol sensitivity, acute"}],
        },
        "accession": "VCV000018396",
    },
}


def _fake_request_json(self, url: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """Stand-in for ClinVarClient/DbSNPClient._request_json (mocks only requests.get)."""
    is_dbsnp = "snp" in getattr(self, "db", "")
    if url.endswith("esearch.fcgi"):
        term = params["term"]
        table = _DBSNP_HITS if is_dbsnp else _CLINVAR_HITS
        ids = table.get(term, [])
        return {"esearchresult": {"idlist": ids}}
    if url.endswith("esummary.fcgi"):
        ids = params["id"].split(",")
        if is_dbsnp:
            # Only exercised via the rsid-detail path in this script;
            # not needed for the found/not-found assertions below.
            return {"result": {"uids": ids, **{i: {} for i in ids}}}
        result = {"uids": ids}
        for i in ids:
            result[i] = _CLINVAR_SUMMARY.get(i, {})
        return {"result": result}
    if "/refsnp/" in url:
        return {"primary_snapshot_data": {}}
    raise AssertionError(f"Unexpected URL in fake responder: {url}")


def main() -> int:
    from database.clinvar_client import ClinVarClient
    from database.dbsnp_client import DbSNPClient
    from pipeline.orchestrator import GeperPipeline

    print("=" * 78)
    print("PART 1 -- Unit-level: query construction + build sensitivity")
    print("=" * 78)

    with (
        mock.patch.object(DbSNPClient, "_request_json", _fake_request_json),
        mock.patch.object(ClinVarClient, "_request_json", _fake_request_json),
    ):
        from pipeline.vcf_parser import Variant

        cases = [
            Variant("1", 169519049, ".", "T", "C", None, None, {}),
            Variant("11", 5248232, ".", "T", "A", None, None, {}),
            Variant("12", 112241766, ".", "G", "A", None, None, {}),
        ]

        dbsnp = DbSNPClient()
        clinvar = ClinVarClient()

        all_ok = True
        for variant in cases:
            print(f"\n--- {variant.chrom}:{variant.pos} {variant.ref}>{variant.alt} ---")

            # OLD behavior, reproduced with the REAL fixed client code by
            # calling it exactly as the pre-fix orchestrator always did:
            # with no assembly information at all. The client's fallback
            # for an unresolved build is dbSNP/ClinVar's own GRCh38-default
            # field -- the same ambiguous field the pre-fix code hardcoded
            # unconditionally, so this reproduces the original bug faithfully.
            old_dbsnp = dbsnp.lookup_variant(variant, assembly=None)
            old_clinvar = clinvar.query_variant(variant, rsid=None, assembly=None)
            print(
                f"  [OLD -- no assembly passed, i.e. pre-fix orchestrator] "
                f"dbSNP found={old_dbsnp['found']}  ClinVar found={old_clinvar['found']}"
            )

            # NEW behavior: assembly correctly resolved and threaded through
            # as GRCh37 (what the fixed orchestrator now does).
            new_dbsnp = dbsnp.lookup_variant(variant, assembly="GRCh37")
            new_clinvar = clinvar.query_variant(variant, rsid=None, assembly="GRCh37")

            print(
                f"  [NEW -- assembly='GRCh37' passed] "
                f"dbSNP found={new_dbsnp['found']} rsid={new_dbsnp.get('rsid')}  "
                f"ClinVar found={new_clinvar['found']} "
                f"significance={[r['clinical_significance'] for r in new_clinvar.get('records', [])]}"
            )

            if old_dbsnp["found"] or old_clinvar["found"]:
                print("  UNEXPECTED: old (no-assembly) query matched -- check fixture data")
                all_ok = False
            if not new_dbsnp["found"] or not new_clinvar["found"]:
                print("  FAILURE: fixed client did not find a known variant")
                all_ok = False

        if not all_ok:
            print("\nPART 1: FAILED")
            return 1
        print("\nPART 1: PASSED -- old queries miss (bug reproduced), new queries hit for all 3 known variants.")

    print()
    print("=" * 78)
    print("PART 2 -- Integration: full orchestrator run on the test VCF")
    print("=" * 78)

    with (
        mock.patch.object(DbSNPClient, "_request_json", _fake_request_json),
        mock.patch.object(ClinVarClient, "_request_json", _fake_request_json),
        mock.patch.object(GeperPipeline, "_run_startup_validation", lambda self: None),
    ):
        # Stub every stage except VCF parsing, assembly preflight, and the
        # dbSNP/ClinVar stages -- those three are exactly what this audit
        # is about, so they run for real (only the HTTP layer is faked).
        pipeline = GeperPipeline(output_dir="/home/claude/work/verify_output")

        def _noop_sequence_context(self, variant, flank_size=None):
            return None

        def _noop_route(self, variant, sequence_context):
            return []

        def _noop_rna(self, variant, sequence_context, errors):
            return {"skipped": True}

        def _noop_protein(self, variant, sequence_context, errors):
            return {"skipped": True}

        def _noop_blast(self, sequence_context, errors):
            return {"hits": [], "hit_count": 0, "skipped": True}

        with (
            mock.patch.object(type(pipeline), "_run_rna_stage", _noop_rna),
            mock.patch.object(type(pipeline), "_run_protein_stage", _noop_protein),
            mock.patch.object(type(pipeline), "_run_blast_stage", _noop_blast),
        ):
            from pipeline.sequence_context import SequenceContextGenerator
            from utils.exceptions import SequenceGenerationError

            def _unavailable_context(self, variant, flank_size=None):
                raise SequenceGenerationError(
                    "sequence context intentionally unavailable in this test "
                    "(Ensembl isn't reachable from this sandbox; irrelevant to "
                    "the ClinVar/dbSNP fix under test)"
                )

            with mock.patch.object(SequenceContextGenerator, "build_context", _unavailable_context):
                result = pipeline.run(_os.path.join(_SCRIPT_DIR, "testdata", "known_variants_grch37.vcf"), resume=False)

    ok = True
    for entry in result["variants"]:
        v = entry["variant"]
        dbsnp_r = entry["dbsnp"]
        clinvar_r = entry["clinvar"]
        found_both = dbsnp_r.get("found") and clinvar_r.get("found")
        status = "OK" if found_both else "MISSING RECORD (regression!)"
        print(
            f"  {v['chrom']}:{v['pos']} {v['ref']}>{v['alt']}  "
            f"dbSNP found={dbsnp_r.get('found')} rsid={dbsnp_r.get('rsid')}  "
            f"ClinVar found={clinvar_r.get('found')}  [{status}]"
        )
        if not found_both:
            ok = False

    print()
    if ok:
        print(
            "PART 2: PASSED -- orchestrator resolves GRCh37 from the VCF header and "
            "retrieves both ClinVar and dbSNP records for all 3 known variants."
        )
    else:
        print("PART 2: FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
