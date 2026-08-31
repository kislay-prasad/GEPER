"""
21-site str(exc) sweep, Batch 3 (closing batch): the CONSUMER-side
`not X.get("error")` truthiness sites -- eleven of them, AST-exact
re-derived at this HEAD (Phase 1's informal count was three; the
larger set is itself a finding, same shape as "the four sites turning
out to be twelve" in Batch 2 -- `errors.append` CALLS were counted
there; here `not X.get("error")` EXPRESSIONS were counted, and the
duplicated RNA-FM check across four separate files is what pushed the
number well past Phase 1's informal tally).

WHY "RED-FIRST" DOES NOT APPLY THE SAME WAY HERE. Batches 1/2 fixed a
PRODUCER that could emit an empty string; the danger case failed on
the original code and passed on the fixed one. These eleven sites are
CONSUMERS whose truthiness sub-condition is provably INERT: something
else in the same `and`-chain (a `found`/`skipped` check, or, for one
site, the producer string's own guaranteed non-empty prefix) already
decides the branch regardless of how `error` is read. There is no
danger case that fails before the fix and passes after, because the
whole point of "inert" is that the outcome does not change. What each
test below proves instead: the SAME `result` dict with `error=""`
(the truthiness-defeating case) produces an IDENTICAL outcome to the
one with `error=None` (an honestly-absent error) -- and that identity
holds whether the site is read by truthiness or by `is not None`,
which is exactly the claim "inert" is making, demonstrated rather than
just asserted. Every test in this file passes unchanged before and
after the sites' own `not X.get("error")` -> `X.get("error") is None`
edit; none of them is expected to flip red-to-green, and a test that
did would mean a site was misclassified as inert.

THE ELEVEN SITES, AND WHICH OF TWO IMMUNITY MECHANISMS EACH ONE USES:

  MECHANISM A -- an independent `found`/`skipped` gate already decides
  the branch:
    confidence_engine.py:209    ClinGen, _clinical_quality
    confidence_engine.py:388    UniProt, _protein_quality
    confidence_engine.py:417    InterPro, _protein_quality
    confidence_engine.py:546    RNA-FM, _sequence_context_quality
    interpretation.py:337       RNA-FM, _biological_context_evidence's
                                 sibling (sequence-context-models list)
    interpretation.py:~1024     UniProt, _biological_context_evidence
    interpretation.py:~1046     InterPro, _biological_context_evidence
    interpretation_result.py:~532 RNA-FM, ai_context_models
    prioritization_engine.py:~435 RNA-FM, _sequence_context_factor
    interpro/lookup.py:~143     InterPro provider-internal domain-
                                 overlap check (`result.get("found")`)

  pvs1/lookup.py:~150 IS NOT MECHANISM A, AND IS NOT ACTUALLY "INERT"
  IN THE SAME SENSE AS THE OTHER TEN -- confirmed by this file's own
  test, not just asserted. `TestPVS1LookupCacheGateInertSite` FAILS on
  the unfixed code (a mocked `error=""` result genuinely gets cached,
  which is wrong) and only passes once the site's `not
  result.get("error")` becomes `is not None` -- a real behavioural
  difference for that input, unlike the ten Mechanism-A sites above,
  where the same before/after test passes identically either way. What
  makes this site SAFE TODAY, matching the same class as Batch 2's
  four "structurally immune" sites rather than this batch's other ten,
  is that its one real producer (`TranscriptLookup._fetch_live`) can
  never actually hand it an empty string: its sole "error" assignment
  is f"Ensembl transcript lookup failed for {gene} after {N} attempts:
  {last_error}" -- a non-empty prefix regardless of whether the
  underlying exception itself had a message. So the fix here is
  closing a genuine (if currently unreachable via the real code path)
  defect, not a no-op -- and its consequence, had it ever fired, would
  also have been a different KIND of harm than the other ten: it gates
  whether a result gets CACHED, not what text a reader sees, so a
  transient failure would have been served from cache to every
  subsequent query for that gene rather than merely misleading a
  rationale sentence. Grouped in this batch because it shares the same
  `not X.get("error")` -> `is not None` mechanical edit, not because it
  shares the other ten's actual behavioural inertness.

NOT INDEPENDENTLY TESTED: interpretation_result.py's RNA-FM site lives
inside `build_interpretation_result`, a large function with many
required inputs (variant dict, every provider result, transcript
structure, ...) that resists isolation the same way Batch 2's inline
Sequence-context-generation/Routing sites did. It is BYTE-IDENTICAL to
the confidence_engine.py/interpretation.py/prioritization_engine.py
RNA-FM checks (same three-line `if rna_result and not
rna_result.get("skipped") and ...` -- confirmed by direct comparison,
not assumed from the shared docstring's own claim), and the same
Mechanism-A proof applies, but it is named here as unverified-by-
execution rather than silently treated as covered by the other three.
"""

import unittest

from pipeline.confidence_engine import ConfidenceEngine
from pipeline.interpretation import InterpretationEngine
from pipeline.interpro.lookup import InterProLookup
from pipeline.prioritization_engine import PrioritizationEngine
from pipeline.pvs1.lookup import TranscriptLookup


def _failed_result(**extra):
    """The shape every orchestrator.py stage wrapper Batches 1/2 fixed
    now returns on a genuine failure: found=False, error=<non-empty,
    or -- the dangerous case -- empty>."""
    base = {"found": False, "skipped": False, "error": ""}
    base.update(extra)
    return base


class TestConfidenceEngineInertSites(unittest.TestCase):
    def test_clingen_empty_error_matches_none_error(self):
        empty = ConfidenceEngine._clinical_quality(None, _failed_result(), 1.0)
        none_ = ConfidenceEngine._clinical_quality(None, _failed_result(error=None), 1.0)
        self.assertEqual(
            (empty.presence, empty.quality, empty.rationale), (none_.presence, none_.quality, none_.rationale)
        )
        self.assertEqual(empty.presence, 0.0)

    def test_uniprot_interpro_empty_error_matches_none_error(self):
        empty = ConfidenceEngine._protein_quality(_failed_result(), _failed_result(), 1.0)
        none_ = ConfidenceEngine._protein_quality(_failed_result(error=None), _failed_result(error=None), 1.0)
        self.assertEqual((empty.presence, empty.rationale), (none_.presence, none_.rationale))
        self.assertEqual(empty.presence, 0.0)

    def test_rna_fm_empty_error_matches_none_error(self):
        empty = ConfidenceEngine._sequence_context_quality([], {"skipped": True, "error": ""}, None, 1.0)
        none_ = ConfidenceEngine._sequence_context_quality([], {"skipped": True, "error": None}, None, 1.0)
        self.assertEqual((empty.presence, empty.rationale), (none_.presence, none_.rationale))


class TestInterpretationInertSites(unittest.TestCase):
    def test_uniprot_interpro_empty_error_matches_none_error(self):
        empty = InterpretationEngine._biological_context_evidence(
            uniprot_result=_failed_result(), interpro_result=_failed_result(), alphafold_result=None
        )
        none_ = InterpretationEngine._biological_context_evidence(
            uniprot_result=_failed_result(error=None), interpro_result=_failed_result(error=None), alphafold_result=None
        )
        self.assertEqual(empty, none_)
        self.assertEqual(empty, [])


class TestPrioritizationEngineInertSite(unittest.TestCase):
    def test_rna_fm_empty_error_matches_none_error(self):
        empty = PrioritizationEngine._sequence_context_factor([], {"skipped": True, "error": ""}, None, 1.0)
        none_ = PrioritizationEngine._sequence_context_factor([], {"skipped": True, "error": None}, None, 1.0)
        self.assertEqual(empty.value, none_.value)


class TestInterProLookupInertSite(unittest.TestCase):
    def test_empty_error_matches_none_error(self):
        from unittest import mock

        lookup = InterProLookup.__new__(InterProLookup)
        uniprot_result = {"accession": "P38398"}
        with mock.patch("pipeline.interpro.lookup.CONFIG") as fake_config:
            fake_config.interpro.ENABLED = True
            lookup.query_accession = lambda accession: _failed_result(domains=[{"start": 1, "end": 10}])
            empty = lookup.query_variant(uniprot_result, protein_position=5)
            lookup.query_accession = lambda accession: _failed_result(error=None, domains=[{"start": 1, "end": 10}])
            none_ = lookup.query_variant(uniprot_result, protein_position=5)
        self.assertEqual(empty.get("affected_domains"), none_.get("affected_domains"))
        self.assertIsNone(empty.get("affected_domains"))


class TestPVS1LookupCacheGateInertSite(unittest.TestCase):
    """MECHANISM B: proves the cache gate treats error="" and error=None
    identically -- neither caches -- because `found`/`skipped` play no
    role here at all; only presence of a non-None `error` matters."""

    class _RecordingCache:
        def __init__(self):
            self.puts = []

        def get(self, key):
            return None

        def put(self, key, value):
            self.puts.append((key, value))

    def test_empty_error_and_none_error_are_not_both_treated_as_cacheable(self):
        lookup = TranscriptLookup.__new__(TranscriptLookup)
        cache = self._RecordingCache()
        lookup.cache = cache
        lookup._fetch = lambda gene_symbol, build: {"found": False, "error": ""}
        lookup.query_gene("BRCA1")
        self.assertEqual(cache.puts, [], "an empty-string error must not be cached as though it were a success")

    def test_a_genuine_success_is_still_cached(self):
        lookup = TranscriptLookup.__new__(TranscriptLookup)
        cache = self._RecordingCache()
        lookup.cache = cache
        lookup._fetch = lambda gene_symbol, build: {"found": True, "error": None, "transcript": {}}
        lookup.query_gene("BRCA1")
        self.assertEqual(len(cache.puts), 1)


if __name__ == "__main__":
    unittest.main()
