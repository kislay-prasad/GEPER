# *** THE VERDICT BELOW IS UNDATED. ***
#
# WHAT THIS IS, in its own words: Live-network verification script for the PS3/BS3 functional-evidence
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
Live-network verification script for the PS3/BS3 functional-evidence
integration (`pipeline/functional_evidence/`, `pipeline/acmg_rules.py::
ACMGRuleEngine._ps3`/`_bs3`).

WHY THIS EXISTS / WHAT IT DOES AND DOES NOT PROVE
----------------------------------------------------
Unlike most of GEPER's other external integrations, this environment
*can* reach both `erepo.clinicalgenome.org` and `api.mavedb.org`
directly (confirmed during feasibility research and again here) --
this script is therefore a genuine, unmocked, real-network proof, not
a fixture-replay of `tests/test_ps3_bs3.py`'s frozen data. It exercises
the real `ErepoFunctionalEvidenceProvider`/`MaveDBFunctionalEvidenceProvider`/
`FunctionalEvidenceLookup` code, against real ClinGen/MaveDB responses,
for three genes chosen to cover every real outcome this integration can
produce:

  - BRCA1: a real ClinGen ERepo PS3-Met curation exists for
    NC_000017.11:g.43106534C>A (NM_007294.4:c.135-1G>T) -- confirms the
    primary source path end to end.
  - BRCA1 (a different variant): no ERepo curation, but a real MaveDB
    saturation-genome-editing score set covers it -- confirms the
    secondary-source fallback and score-calibration bucketing.
  - CFTR: neither source has any curated/calibrated data for this gene
    as of this integration -- confirms the honest "not_evaluated"
    fallback actually fires for a real, known gap, rather than crashing
    or fabricating a call.

This is not a substitute for `tests/test_ps3_bs3.py` (deterministic,
network-free, run in CI) -- it is the live counterpart that confirms
the frozen fixtures those tests use still reflect reality.
"""

import os as _os
import sys

_SCRIPT_DIR = _os.path.dirname(_os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from pipeline.acmg_rules import ACMGRuleEngine
from pipeline.functional_evidence.lookup import FunctionalEvidenceLookup
from pipeline.hgvs_utils import to_hgvs_g

_PASS = "PASS"
_FAIL = "FAIL"


def _check(label: str, condition: bool, detail: str = "") -> bool:
    status = _PASS if condition else _FAIL
    print(f"[{status}] {label}" + (f" -- {detail}" if detail else ""))
    return condition


def main() -> int:
    lookup = FunctionalEvidenceLookup()
    results = []

    print("=" * 70)
    print("PART 1: BRCA1 real ClinGen Evidence Repository PS3-Met curation")
    print("=" * 70)
    hgvs_g = to_hgvs_g("17", 43106534, "C", "A", assembly="GRCh38")
    fe_result = lookup.query_variant("BRCA1", hgvs_g=hgvs_g, hgvs_c=None)
    results.append(_check("ERepo query returns found=True", fe_result.get("found") is True))
    results.append(_check("source is clingen_erepo", fe_result.get("source") == "clingen_erepo"))
    ps3_records = [r for r in fe_result.get("records", []) if r.get("call") == "PS3"]
    results.append(_check("a real PS3-Met record was returned", bool(ps3_records)))
    if ps3_records:
        results.append(
            _check(
                "expert panel is the ENIGMA BRCA1/BRCA2 VCEP",
                ps3_records[0].get("expert_panel") == "ENIGMA BRCA1 and BRCA2 VCEP",
                detail=str(ps3_records[0].get("expert_panel")),
            )
        )
    ps3 = ACMGRuleEngine._ps3(fe_result)
    bs3 = ACMGRuleEngine._bs3(fe_result)
    results.append(
        _check(
            "ACMGRuleEngine._ps3 triggers at strong strength", ps3.status == "triggered" and ps3.strength == "strong"
        )
    )
    results.append(
        _check("ACMGRuleEngine._bs3 stays not_evaluated (no conflicting call)", bs3.status == "not_evaluated")
    )

    print()
    print("=" * 70)
    print("PART 2: BRCA1 real MaveDB secondary-source fallback + calibration")
    print("=" * 70)
    # Real row confirmed during feasibility research: NM_007294.3:c.5565A>T
    # -> score -0.0153221623501722 -> MaveDB's own calibration buckets
    # this as "normal" (BS3-relevant).
    fe_result2 = lookup.query_variant("BRCA1", hgvs_g=None, hgvs_c="NM_007294.3:c.5565A>T")
    results.append(_check("MaveDB query returns found=True", fe_result2.get("found") is True))
    results.append(_check("source is mavedb", fe_result2.get("source") == "mavedb"))
    bs3_records = [r for r in fe_result2.get("records", []) if r.get("call") == "BS3"]
    results.append(_check("a real BS3-relevant ('normal') record was returned", bool(bs3_records)))
    if bs3_records:
        results.append(
            _check(
                "raw score matches the known real value",
                abs((bs3_records[0].get("raw_score") or 0) - (-0.0153221623501722)) < 1e-6,
                detail=str(bs3_records[0].get("raw_score")),
            )
        )
    ps3_2 = ACMGRuleEngine._ps3(fe_result2)
    bs3_2 = ACMGRuleEngine._bs3(fe_result2)
    results.append(
        _check(
            "ACMGRuleEngine._bs3 triggers at moderate strength",
            bs3_2.status == "triggered" and bs3_2.strength == "moderate",
        )
    )
    results.append(
        _check("ACMGRuleEngine._ps3 stays not_evaluated (no conflicting call)", ps3_2.status == "not_evaluated")
    )

    print()
    print("=" * 70)
    print("PART 3: CFTR -- known, honestly-reported coverage gap")
    print("=" * 70)
    fe_cftr = lookup.query_variant(
        "CFTR",
        hgvs_g="NC_000007.14:g.117559592G>A",
        hgvs_c="NM_000492.4:c.1521_1523delCTT",
    )
    results.append(_check("CFTR query returns found=False", fe_cftr.get("found") is False))
    ps3_cftr = ACMGRuleEngine._ps3(fe_cftr)
    bs3_cftr = ACMGRuleEngine._bs3(fe_cftr)
    results.append(
        _check("ACMGRuleEngine._ps3 reports not_evaluated (no fabricated call)", ps3_cftr.status == "not_evaluated")
    )
    results.append(
        _check("ACMGRuleEngine._bs3 reports not_evaluated (no fabricated call)", bs3_cftr.status == "not_evaluated")
    )

    print()
    print("=" * 70)
    passed = sum(1 for r in results if r)
    total = len(results)
    print(f"SUMMARY: {passed}/{total} checks passed")
    print("=" * 70)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
