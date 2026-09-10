"""
tests/test_orchestrator_threads_gene_identity.py
──────────────────────────────────────────────────

Human dispatch (conv-orchestrator-gene-identity, 2026-09-10): closes the
gap Andy's own merged suite (`tests/test_sevengene_acceptance_suite.py`)
names explicitly in its own docstring -- his tests call
`LiveAPIClinGenProvider().query(gene_symbol)` / `LiveAPIHPOProvider()
.query(gene_symbol)` / `ErepoFunctionalEvidenceProvider()
.fetch_gene_index(gene_symbol)` DIRECTLY, with an explicit gene
argument he varies himself. That proves the PROVIDERS thread gene
identity when handed it. It cannot prove the ORCHESTRATOR actually
hands them the real one: the seam Andy's suite structurally cannot see
is the layer between "a variant's gene was resolved" and "a provider
was called with it".

THE EXACT SEAM, NAMED (found by reading, not assumed): every one of the
gene-keyed stage wrappers in `pipeline/orchestrator.py` re-derives its
own gene symbol from the ClinGen stage's already-resolved result,
independently, in a line that looks like this --

  `_run_hpo_stage` (line ~2753):
      gene_symbol = (clingen_result or {}).get("gene_symbol")
  `_run_functional_evidence_stage` (line ~3010):
      gene_symbol = (clingen_result or {}).get("gene_symbol")

-- and THIS is the layer nothing tests: if either of these lines were
mutated to hold a constant, an empty string, or None instead of really
reading `clingen_result["gene_symbol"]`, no existing test (including
Andy's) would go red, because nothing drives these two orchestrator
methods with a REAL, resolved `clingen_result` built from a REAL
variant.

THE ORCHESTRATOR ENTRY POINT CHOSEN, AND WHY: `GeperPipeline
._run_clingen_stage` chained into `._run_hpo_stage` (and, for a second,
independent proof of the identical defect shape,
`._run_functional_evidence_stage`) -- called directly on a real
(if minimally-constructed) `GeperPipeline` instance, exactly the two
consecutive calls `_process_variant` itself makes
(`clingen_result = self._run_clingen_stage(...)`, then
`hpo_result = self._run_hpo_stage(clingen_result, errors)`). This is
the narrowest real entry point that actually contains the seam: going
one level up to `_process_variant` itself would require constructing
DNA/RNA model routing, sequence context generation, and a dozen other
unrelated stages this task has no interest in, for no additional
coverage of the actual defect class -- the two stage methods ARE the
orchestrator layer in question. (Same reasoning
`test_orchestrator_empty_error_still_recorded.py` already used to
justify calling stage methods directly on a bare
`GeperPipeline.__new__` instance rather than the full pipeline.)

WHY GENE RESOLUTION ITSELF NEEDS NO NETWORK MOCKING (fully offline,
deterministic, confirmed by reading `pipeline/clingen/utils.py::
resolve_gene_symbol_detail`): resolution prefers the VCF's own `GENE=`
INFO field over any Ensembl overlap lookup, and returns immediately
when present -- no network call happens at all. So two `Variant`
objects differing only in `info={"GENE": ...}` resolve deterministically
to two different gene symbols via REAL, unmodified orchestrator/ClinGen
code, with nothing mocked at that layer.

RED-FIRST, THE SHAPE THIS TASK ACTUALLY HAS (declared before touching
anything, per the dispatch's own anticipation): the orchestrator
ALREADY threads gene identity correctly today -- nothing here is
broken, so there is no natural "fails against current code, passes
after a fix" cycle the way a genuine defect fix would have. GREEN IS
EMPTY: no production line changes in this branch. The red-first
discipline this task actually calls for is the dispatch's own
acceptance criterion instead -- INTRODUCE THE DEFECT IN PRODUCTION ON
PURPOSE (mutate `_run_hpo_stage`'s gene_symbol line to a constant, and
separately `_run_functional_evidence_stage`'s identical line, one at a
time), declare in writing what is expected, run, confirm the RELEVANT
test below goes red WHILE THE OTHER STAYS GREEN (proving each test is
specific to its own stage, not accidentally sensitive to any change
anywhere), then revert and confirm both green again. Both mutations
were performed and reverted; neither is committed anywhere (same
practice the dispatch names -- "Pam mutated production in place
tonight ... and reverted before committing") -- see the dispatch report
for the full transcript of both. Without this step, this file would be
exactly the failure mode the dispatch warns about: "if your test stays
green under a broken orchestrator, you have built Andy's suite again
one layer up." IT ALMOST WAS: see `_first_gene_specific_capture`'s own
docstring below for a self-caught bug that made the FIRST version of
this file's HPO test pass for the wrong reason even under the
deliberate defect, found only by actually running this exact
mutate-and-confirm step rather than trusting the test on inspection
alone.

BASELINE (measured on my own instrument, not taken from anyone else --
standing rule after tonight's CI-figure mismatch, and MY OWN INSTANCE
OF THE SAME MISTAKE, caught before it reached a report: I first carried
forward 2497 -- my own review_required task's number, measured at a
DIFFERENT commit, 1cee993/da35c979 -- as if it were this branch's
baseline at abaa7c96, without re-measuring. It was not: `geper/tests`
actually collects/passes 2504 at master abaa7c96 with this file absent
(confirmed by moving this file aside and re-running immediately before
writing this sentence), not 2497. This file's own delta is stated in
the dispatch report, not here -- but the number it is a delta FROM is
2504.)
"""

from __future__ import annotations

import time
import unittest
from types import SimpleNamespace

import requests

from pipeline.clingen.lookup import ClinGenLookup
from pipeline.functional_evidence.lookup import FunctionalEvidenceLookup
from pipeline.hpo.lookup import HPOLookup
from pipeline.orchestrator import GeperPipeline
from pipeline.vcf_parser import Variant


class _CaptureAndBlock:
    """
    Own local copy of the identical helper
    `test_sevengene_acceptance_suite.py` uses -- DELIBERATELY DUPLICATED,
    not imported from that file, and the trade is written down here
    rather than left looking like an oversight: importing a private
    helper from Andy's module would couple THIS file's control to HIS
    implementation -- a future change to his `_CaptureAndBlock` (a
    tightened capture condition, a different exception type, anything)
    could then turn this orchestrator-level test green or red for a
    reason that has nothing to do with whether the orchestrator itself
    threads gene identity, which is the one thing this file exists to
    prove. Duplication costs a few lines of drift risk between two
    near-identical helpers; coupling would have cost this file's own
    meaning. Patches `requests.get`/`requests.post` to CAPTURE the
    outbound request and then raise before any real network round-trip,
    so this whole file is offline and deterministic.
    """

    def __init__(self):
        self.captured = []
        self._orig_get = requests.get
        self._orig_post = requests.post
        self._orig_sleep = time.sleep

    def _capture(self, method, url, *args, **kwargs):
        self.captured.append({"method": method, "url": url, "params": kwargs.get("params"), "json": kwargs.get("json")})
        raise requests.ConnectionError(f"blocked-for-test: {method} already captured above")

    def __enter__(self):
        requests.get = lambda url, *a, **kw: self._capture("GET", url, *a, **kw)
        requests.post = lambda url, *a, **kw: self._capture("POST", url, *a, **kw)
        time.sleep = lambda *a, **kw: None
        return self

    def __exit__(self, *exc_info):
        requests.get = self._orig_get
        requests.post = self._orig_post
        time.sleep = self._orig_sleep


def _signature(captured_request):
    import json

    return json.dumps(
        {"url": captured_request["url"], "params": captured_request["params"], "json": captured_request["json"]},
        sort_keys=True,
        default=str,
    )


def _first_gene_specific_capture(cap: _CaptureAndBlock, gene_symbol: str):
    """
    SELF-CAUGHT BUG, FOUND BY RUNNING THE NEGATIVE CONTROL BEFORE
    TRUSTING THIS FILE (exactly the discipline this task's own
    acceptance criterion demands): taking `cap.captured[0]` unconditionally
    (Andy's own pattern in `test_sevengene_acceptance_suite.py`, correct
    there because his providers are called standalone with nothing else
    to warm up first) silently picked up a ONE-TIME,
    PROCESS-LIFETIME local-dataset BOOTSTRAP fetch
    (`pipeline.hpo.bootstrap`'s "genes_to_phenotype.txt" download
    attempt, cache-miss-or-stale, fires once per process and never
    again) as the "first" captured request on whichever variant's HPO
    stage call happened to run FIRST in the whole test process -- a
    request that names NO gene symbol at all. That made the first and
    second variants' "first captured" requests look different for a
    reason that had NOTHING to do with gene identity (bootstrap-fetch
    vs. real gene-search request), which is precisely how this test
    stayed GREEN under a deliberately broken orchestrator during this
    file's own required negative-control validation (see the module
    docstring and the dispatch report for the full transcript). Fixed
    by matching on the ACTUAL gene symbol string appearing in the
    request signature, not positional order -- robust to any number of
    one-time bootstrap/warmup requests a provider fires before its real
    per-gene call, without needing to hardcode each provider's dataset
    URL to exclude it.
    """
    for request in cap.captured:
        if gene_symbol in _signature(request):
            return request
    raise AssertionError(
        f"no captured request's signature contained the gene symbol {gene_symbol!r} -- "
        f"captured requests were: {cap.captured}"
    )


def _capture_one_for_gene(cap: _CaptureAndBlock, gene_symbol: str, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except Exception:  # noqa: BLE001 -- deliberately broad, see _CaptureAndBlock's docstring
        pass
    return _first_gene_specific_capture(cap, gene_symbol)


# Test fixtures only, never read by production code -- two real,
# unrelated genes (not BRCA1/BRCA2, for the same reason Andy's suite
# avoids them: not conflating "genes this engine happens to be tuned
# on" with "genes it can handle generically"). Coordinates are
# realistic GRCh38 positions inside each gene but are not load-bearing
# here -- gene resolution below goes through the VCF `GENE=` hint, never
# a coordinate-based Ensembl lookup, so what matters is that the two
# `info={"GENE": ...}` values differ, not the exact position.
_VARIANT_A = Variant(
    chrom="17", pos=7675088, variant_id=".", ref="C", alt="T", qual=None, filter_status=None, info={"GENE": "TP53"}
)
_VARIANT_B = Variant(
    chrom="7", pos=55191822, variant_id=".", ref="T", alt="G", qual=None, filter_status=None, info={"GENE": "EGFR"}
)


def _bare_pipeline():
    """A `GeperPipeline` with only the attributes these two stage
    methods touch, plus REAL (not mocked) lookup facades -- the whole
    point of this file is that gene identity must survive a REAL
    resolution call, not a hand-typed dict standing in for one. Same
    `__new__` pattern `test_orchestrator_empty_error_still_recorded.py`
    already uses, for the same reason (constructing a full pipeline
    pulls in models/config this test has no interest in)."""
    p = GeperPipeline.__new__(GeperPipeline)
    p.enable_profiling = False
    p.sequence_context_gen = SimpleNamespace(assembly="GRCh38")
    p.clingen_client = ClinGenLookup()
    p.hpo_client = HPOLookup()
    p.functional_evidence_client = FunctionalEvidenceLookup()
    return p


def _resolve_clingen(pipeline: GeperPipeline, variant: Variant):
    """Runs the REAL `_run_clingen_stage` (network-blocked -- the
    ClinGen evidence fetch itself is irrelevant here; only
    `gene_symbol`, which gene RESOLUTION sets before any network call
    happens, matters) and returns the resulting dict."""
    errors: list = []
    with _CaptureAndBlock():
        clingen_result = pipeline._run_clingen_stage(variant, errors)
    return clingen_result


class TestOrchestratorThreadsGeneIdentityThroughHpoStage(unittest.TestCase):
    """THE load-bearing assertion: two real variants, resolved via the
    REAL `_run_clingen_stage`, chained into the REAL `_run_hpo_stage` --
    exactly the two consecutive calls `_process_variant` itself makes --
    must each produce an outbound HPO request naming that variant's OWN
    real gene, and the two requests must differ from each other."""

    def test_hpo_request_names_the_variants_own_resolved_gene(self):
        pipeline = _bare_pipeline()

        clingen_a = _resolve_clingen(pipeline, _VARIANT_A)
        clingen_b = _resolve_clingen(pipeline, _VARIANT_B)
        # Sanity check on the resolution step itself, before trusting
        # anything downstream of it.
        self.assertEqual(clingen_a.get("gene_symbol"), "TP53")
        self.assertEqual(clingen_b.get("gene_symbol"), "EGFR")

        errors_a: list = []
        errors_b: list = []
        with _CaptureAndBlock() as cap_a:
            captured_a = _capture_one_for_gene(cap_a, "TP53", pipeline._run_hpo_stage, clingen_a, errors_a)
        with _CaptureAndBlock() as cap_b:
            captured_b = _capture_one_for_gene(cap_b, "EGFR", pipeline._run_hpo_stage, clingen_b, errors_b)

        # `_first_gene_specific_capture` above already raises if neither
        # variant's own gene symbol appears anywhere in its own captured
        # request -- that IS the load-bearing assertion. This is the
        # belt-and-suspenders restatement of the same fact in the shape
        # the dispatch itself asked for ("two distinct requests").
        self.assertNotEqual(
            _signature(captured_a),
            _signature(captured_b),
            "the orchestrator's HPO stage produced the SAME outbound request for two variants "
            "in two different genes -- gene identity did not reach the provider call",
        )


class TestOrchestratorThreadsGeneIdentityThroughFunctionalEvidenceStage(unittest.TestCase):
    """The identical defect shape, a second time, through
    `_run_functional_evidence_stage` -- the other stage wrapper that
    independently re-derives `gene_symbol` from `clingen_result` in the
    same one-line pattern. `transcript_result={}` throughout: this
    stage's genomic (g.) HGVS string (computed from chrom/pos/ref/alt
    alone, no transcript needed) is what reaches ClinGen ERepo first;
    the coding (c.) HGVS string that WOULD need a real transcript is
    irrelevant to whether gene identity itself threads through."""

    def test_functional_evidence_request_names_the_variants_own_resolved_gene(self):
        pipeline = _bare_pipeline()

        clingen_a = _resolve_clingen(pipeline, _VARIANT_A)
        clingen_b = _resolve_clingen(pipeline, _VARIANT_B)

        errors_a: list = []
        errors_b: list = []
        with _CaptureAndBlock() as cap_a:
            captured_a = _capture_one_for_gene(
                cap_a, "TP53", pipeline._run_functional_evidence_stage, _VARIANT_A, clingen_a, {}, errors_a
            )
        with _CaptureAndBlock() as cap_b:
            captured_b = _capture_one_for_gene(
                cap_b, "EGFR", pipeline._run_functional_evidence_stage, _VARIANT_B, clingen_b, {}, errors_b
            )

        self.assertNotEqual(
            _signature(captured_a),
            _signature(captured_b),
            "the orchestrator's functional-evidence stage produced the SAME outbound request for "
            "two variants in two different genes -- gene identity did not reach the provider call",
        )


if __name__ == "__main__":
    unittest.main()
