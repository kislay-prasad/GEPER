"""
tests/test_sevengene_acceptance_suite.py
──────────────────────────────────────────

Human dispatch (conv-sevengene, 2026-09-09/10): a SEVEN-GENE ACCEPTANCE
SAMPLE proving Bij AI's retrieval layer genuinely threads gene identity
through for a spread of real, unrelated genes. THE SEVEN GENES ARE AN
ACCEPTANCE SAMPLE PROVING CAPABILITY, NOT A SUPPORTED GENE LIST -- no
allowlist, no gene-validation gate, no rejection path exists in
production code (confirmed by repo-wide grep before this suite was
written), and the seven names below are TEST FIXTURES ONLY: they must
never be read by, or written into, any production constant.

*** SCOPE, STATED EXPLICITLY SO IT IS NOT ASSUMED COVERED (god's own
correction, 2026-09-10, conv-sevengene): every test below calls a
provider DIRECTLY with an explicit gene argument
(`LiveAPIClinGenProvider().query(fixture["gene"])` and so on). That
proves gene identity reaches retrieval WHEN A PROVIDER IS CALLED WITH
IT -- it does NOT prove the orchestrator actually threads the real
gene symbol from a parsed variant through to that call. The thing that
could actually break in production -- the orchestrator silently
dropping or defaulting gene identity before it ever reaches a
provider -- is entirely upstream of everything this suite touches, and
no such regression could turn any assertion below red. That
orchestrator-level coverage is a separate, not-yet-built card; this
file is a strong test of the PROVIDERS' own gene threading and a
near-vacuous test of THE PIPELINE'S end-to-end gene threading, and
must not be read as the latter. ***

BASELINE (measured, not asserted from a number handed down by anyone
else -- see the dispatch's own "measure your own" instruction): this
worktree's `geper/tests/` collected 2504 tests before this file existed
(`pytest tests/ --collect-only -q`, run at da35c979, same commit this
branch is based on). This file's delta is stated in the dispatch report,
not here.

WHY THIS FILE NEEDED NO RED PHASE: RED-FIRST is the standing discipline
for a feature that does not exist yet (see `test_interpretation_outcome
.py`'s own docstring for that shape). Gene-identity propagation through
the gene-keyed retrieval layer was already independently verified,
before this dispatch, in a prior read-only investigation (ANKRD11 vs
TANGO2, reported to god 2026-09-09T22:36) -- it already works. This
suite's job is to turn that one-off proof into a *permanent* regression
guard covering a wider gene spread, split correctly by source-keying
type, not to fix a defect. So the main positive assertions below are
expected to pass on their very first run, and did. The one place an
actual declare-then-run-and-confirm-failure step happens is the
negative control at the bottom of this file -- see that class's
docstring for the declaration.

THE SOURCE-TYPE SPLIT (the one correction this dispatch added to the
task's scope, and the reason this file has two differently-weighted
test classes instead of one):

  GENE-KEYED sources -- ClinGen gene-disease validity/dosage
  (`LiveAPIClinGenProvider.query(gene_symbol)`), HPO gene-to-phenotype
  (`LiveAPIHPOProvider.query(gene_symbol)`), ERepo functional-evidence
  index (`ErepoFunctionalEvidenceProvider.fetch_gene_index(gene_symbol)`)
  -- take ONLY a gene symbol as their query key. "Seven genes must
  produce seven structurally different outbound requests" is a STRONG,
  load-bearing claim here: if gene identity were not reaching retrieval,
  all seven would collapse to one identical request. This repo has no
  dedicated "gene-specific ACMG specification" provider distinct from
  ClinGen's own curation output (checked: no such module exists), so
  ClinGen's gene-disease validity/dosage curation IS that source for
  this suite's purposes.

  POSITION-KEYED sources -- ClinVar (`ClinVarClient.query_variant`,
  keyed by chrom/pos/ref/alt, confirmed by reading
  `_positional_search_term`: no gene symbol enters the query at all)
  and gnomAD (`CompositeGnomadProvider.query(chrom, pos, ref, alt,
  build)`) -- are keyed by genomic position, not gene symbol. "Seven
  different variant coordinates produce seven different requests" is
  NEAR-VACUOUS here: it would be true whether or not the gene symbol
  ever reached the retrieval layer, because these sources never look at
  a gene symbol in the first place. This file states that explicitly at
  every assertion built on these two sources and never uses them as
  proof of gene-identity wiring.

FIXTURES: seven real, distinct genes, none of them BRCA1/BRCA2 (already
the repo's own heavily-used fixture pair -- reusing them here would
risk conflating "genes this engine happens to be tuned on" with "genes
this engine can handle generically", exactly the distinction this
acceptance sample exists to keep separate), none of them referenced
anywhere else in this repository (checked: `grep -rl` for each symbol
across `geper/` and `kim_pipeline/` returns zero hits before this file
was added). Coordinates and classifications are real, GRCh38, verified
live against NCBI ClinVar's own esearch/esummary the same day this file
was written -- not recalled from training data alone:
  TP53     chr17:7675088   C>T   NM_000546.6:c.524G>A      (p.Arg175His, oncology)
  EGFR     chr7:55191822   T>G   NM_005228.5:c.2573T>G     (p.Leu858Arg, oncology)
  KRAS     chr12:25245350  C>T   c.35G>A                   (p.Gly12Asp, oncology)
  APC      chr5:112819074  C>T   NM_000038.6:c.1042C>T     (p.Arg348Ter, expert-panel Pathogenic)
  PALB2    chr16:23635864  G>A   NM_024675.4:c.682C>T      (p.Gln228Ter, expert-panel Pathogenic)
  ANKRD11  chr16:89290671  TTT>T NM_013275.6:c.554_555del  (p.Lys185fs, KBG syndrome, Pathogenic)
  MECP2    chrX:154031356  T>C   NM_001110792.2:c.508A>G   (p.Thr170Ala, Rett syndrome, expert-panel Pathogenic)
This spread deliberately crosses oncology and Mendelian disease,
autosomal dominant/recessive and X-linked, common and rare -- the point
is generality, not a themed set.
"""

from __future__ import annotations

import json
import time
import unittest

import requests

from pipeline.clingen.provider import LiveAPIClinGenProvider
from pipeline.functional_evidence.erepo_provider import ErepoFunctionalEvidenceProvider
from pipeline.gnomad.provider import CompositeGnomadProvider
from pipeline.hpo.provider import LiveAPIHPOProvider
from pipeline.vcf_parser import Variant

from database.clinvar_client import ClinVarClient

# Test fixtures only -- never read by production code. See module
# docstring for how each coordinate was chosen and verified.
SEVEN_GENE_FIXTURES = [
    {"gene": "TP53", "chrom": "17", "pos": 7675088, "ref": "C", "alt": "T"},
    {"gene": "EGFR", "chrom": "7", "pos": 55191822, "ref": "T", "alt": "G"},
    {"gene": "KRAS", "chrom": "12", "pos": 25245350, "ref": "C", "alt": "T"},
    {"gene": "APC", "chrom": "5", "pos": 112819074, "ref": "C", "alt": "T"},
    {"gene": "PALB2", "chrom": "16", "pos": 23635864, "ref": "G", "alt": "A"},
    {"gene": "ANKRD11", "chrom": "16", "pos": 89290671, "ref": "TTT", "alt": "T"},
    {"gene": "MECP2", "chrom": "X", "pos": 154031356, "ref": "T", "alt": "C"},
]

_BUILD = "GRCh38"


class _CaptureAndBlock:
    """Patches `requests.get`/`requests.post` to CAPTURE the outbound
    request (url, params, json body) and then raise before any real
    network round-trip. Used throughout this file instead of letting
    calls through, deliberately: the URL/params/json a provider builds
    is the entire thing under test here, every provider in this suite
    is documented as never raising on a query failure (graceful
    degradation), and blocking keeps this suite fast, deterministic,
    and safe to run in CI without depending on live third-party APIs
    or their rate limits -- exactly the same method used in the prior
    ANKRD11/TANGO2 investigation's second, controlled probe."""

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
        # Several clients retry on a transient-looking connection error
        # with a real (increasing) backoff sleep before giving up -- the
        # capture already happened on the first attempt, so the retries
        # add nothing but wall-clock time to a test suite that is meant
        # to run in CI on every push. Not a behavior under test here.
        time.sleep = lambda *a, **kw: None
        return self

    def __exit__(self, *exc_info):
        requests.get = self._orig_get
        requests.post = self._orig_post
        time.sleep = self._orig_sleep


def _signature(captured_request):
    """The part of a captured request that should vary with the query
    key: URL plus whatever params/json were sent, serialized to a
    hashable string (querystring dicts and GraphQL variable dicts are
    not hashable on their own)."""
    return json.dumps(
        {"url": captured_request["url"], "params": captured_request["params"], "json": captured_request["json"]},
        sort_keys=True,
        default=str,
    )


def _first_captured(cap: _CaptureAndBlock):
    assert cap.captured, "expected at least one outbound request to be captured, got none"
    return cap.captured[0]


def _capture_one(cap: _CaptureAndBlock, fn, *args, **kwargs):
    """Runs a provider call inside the given capture context. Some
    providers in this repo swallow a query failure internally (gnomAD,
    ClinGen, HPO -- documented graceful-degradation policy); others
    (ClinVarClient, ErepoFunctionalEvidenceProvider) let a genuine
    `ExternalAPIError` propagate after their own retries are exhausted.
    Either way the outbound request was already captured on the first
    attempt before any of that retry/degradation logic ran, which is
    the only thing this suite is checking -- so a raised exception here
    is expected and deliberately swallowed, not a test failure."""
    try:
        fn(*args, **kwargs)
    except Exception:  # noqa: BLE001 -- deliberately broad, see docstring above
        pass
    return _first_captured(cap)


class TestGeneKeyedSourcesThreadGeneIdentity(unittest.TestCase):
    """STRONG, load-bearing assertions: these three sources take only a
    gene symbol as their query key, so seven different captured
    requests across seven different genes is direct proof gene identity
    reached the retrieval layer as a live parameter, not a default."""

    def test_clingen_produces_seven_distinct_requests_for_seven_genes(self):
        signatures = []
        for fixture in SEVEN_GENE_FIXTURES:
            with _CaptureAndBlock() as cap:
                captured = _capture_one(cap, LiveAPIClinGenProvider().query, fixture["gene"])
            signatures.append(_signature(captured))
        self.assertEqual(
            len(set(signatures)),
            len(SEVEN_GENE_FIXTURES),
            f"ClinGen produced fewer than 7 distinct outbound requests for 7 genes: {signatures}",
        )

    def test_hpo_produces_seven_distinct_requests_for_seven_genes(self):
        signatures = []
        for fixture in SEVEN_GENE_FIXTURES:
            with _CaptureAndBlock() as cap:
                captured = _capture_one(cap, LiveAPIHPOProvider().query, fixture["gene"])
            signatures.append(_signature(captured))
        self.assertEqual(
            len(set(signatures)),
            len(SEVEN_GENE_FIXTURES),
            f"HPO produced fewer than 7 distinct outbound requests for 7 genes: {signatures}",
        )

    def test_erepo_produces_seven_distinct_requests_for_seven_genes(self):
        signatures = []
        for fixture in SEVEN_GENE_FIXTURES:
            with _CaptureAndBlock() as cap:
                captured = _capture_one(cap, ErepoFunctionalEvidenceProvider().fetch_gene_index, fixture["gene"])
            signatures.append(_signature(captured))
        self.assertEqual(
            len(set(signatures)),
            len(SEVEN_GENE_FIXTURES),
            f"ERepo produced fewer than 7 distinct outbound requests for 7 genes: {signatures}",
        )


class TestPositionKeyedSourcesVaryByPositionNotByGeneIdentity(unittest.TestCase):
    """WEAK, explicitly NON-load-bearing assertions. ClinVar and gnomAD
    are keyed by chrom/pos/ref/alt, never by gene symbol -- so seven
    distinct requests here is expected REGARDLESS of whether gene
    identity reaches retrieval at all, and must never be cited as
    evidence that it does. Kept in this suite only so a future reader
    sees, in the same file, that these two sources were considered and
    deliberately excluded from the load-bearing claim -- not silently
    left out."""

    def test_clinvar_produces_seven_distinct_requests_for_seven_positions(self):
        signatures = []
        for fixture in SEVEN_GENE_FIXTURES:
            variant = Variant(
                chrom=fixture["chrom"],
                pos=fixture["pos"],
                variant_id=".",
                ref=fixture["ref"],
                alt=fixture["alt"],
                qual=None,
                filter_status=None,
            )
            with _CaptureAndBlock() as cap:
                captured = _capture_one(cap, ClinVarClient().query_variant, variant, assembly=_BUILD)
            signatures.append(_signature(captured))
        self.assertEqual(
            len(set(signatures)),
            len(SEVEN_GENE_FIXTURES),
            "ClinVar requests did not vary by position -- NOTE: this would not prove gene-identity "
            "wiring even if it passed, since ClinVar is never queried by gene symbol.",
        )

    def test_gnomad_produces_seven_distinct_requests_for_seven_positions(self):
        signatures = []
        for fixture in SEVEN_GENE_FIXTURES:
            with _CaptureAndBlock() as cap:
                captured = _capture_one(
                    cap,
                    CompositeGnomadProvider().query,
                    fixture["chrom"],
                    fixture["pos"],
                    fixture["ref"],
                    fixture["alt"],
                    _BUILD,
                )
            signatures.append(_signature(captured))
        self.assertEqual(
            len(set(signatures)),
            len(SEVEN_GENE_FIXTURES),
            "gnomAD requests did not vary by position -- NOTE: this would not prove gene-identity "
            "wiring even if it passed, since gnomAD is never queried by gene symbol.",
        )


class TestNegativeControlBreakGeneIdentityOnPurpose(unittest.TestCase):
    """The dispatch's required negative control, and the reason the
    source-type split above is trustworthy rather than asserted on
    faith.

    DECLARED BEFORE RUNNING: if gene identity is dropped before it
    reaches a gene-keyed provider -- simulated here by calling the same
    real, unmodified provider methods used above but with the gene
    argument held constant across all seven "slots" instead of varying
    it, which is exactly what the retrieval layer would look like from
    the outside if the orchestrator forgot to thread the real gene
    symbol through -- the GENE-KEYED sources' seven captured requests
    must collapse to ONE distinct signature (not seven), which is
    precisely what the assertions in
    `TestGeneKeyedSourcesThreadGeneIdentity` would have caught as a
    failure. The POSITION-KEYED sources, called in the same run with
    their real, still-varying chrom/pos/ref/alt (gene identity was
    never one of their inputs to begin with), must still produce seven
    distinct signatures -- proving they are structurally blind to this
    exact defect class, which is the whole reason they were excluded
    from the load-bearing claim above rather than lumped in with it.

    Confirmed by running this exact class before it was finalized:
    both expectations held as declared.
    """

    _STUB_GENE = "STUBGENE_CONSTANT_ACROSS_ALL_SEVEN_SLOTS"

    def test_gene_keyed_requests_collapse_to_one_signature_when_gene_identity_is_held_constant(self):
        signatures = []
        for _fixture in SEVEN_GENE_FIXTURES:
            with _CaptureAndBlock() as cap:
                captured = _capture_one(cap, LiveAPIClinGenProvider().query, self._STUB_GENE)
            signatures.append(_signature(captured))
        self.assertEqual(
            len(set(signatures)),
            1,
            "expected a single collapsed signature when gene identity is held constant across all "
            f"seven slots -- got {len(set(signatures))} distinct signatures instead: {signatures}",
        )

    def test_position_keyed_requests_still_vary_even_when_gene_identity_is_broken(self):
        """Same broken-gene-identity condition as above (the stub gene
        is irrelevant here since ClinVar never receives it), real
        varying positions -- must still produce seven distinct
        requests, proving position-keyed sources cannot detect this
        defect class at all."""
        signatures = []
        for fixture in SEVEN_GENE_FIXTURES:
            variant = Variant(
                chrom=fixture["chrom"],
                pos=fixture["pos"],
                variant_id=".",
                ref=fixture["ref"],
                alt=fixture["alt"],
                qual=None,
                filter_status=None,
            )
            with _CaptureAndBlock() as cap:
                captured = _capture_one(cap, ClinVarClient().query_variant, variant, assembly=_BUILD)
            signatures.append(_signature(captured))
        self.assertEqual(
            len(set(signatures)),
            len(SEVEN_GENE_FIXTURES),
            "position-keyed requests collapsed under a broken-gene-identity condition they should "
            "be structurally blind to -- this would mean the position-keyed assertion above is not "
            "as vacuous as documented, which changes this file's own reasoning and must be reported.",
        )


if __name__ == "__main__":
    unittest.main()
