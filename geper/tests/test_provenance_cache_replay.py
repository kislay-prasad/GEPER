"""
Finding 7: cache-replay was a state the provenance vocabulary could not
express.

OBSERVED: a BLAST search returned real NCBI hits (`NM_007294.4 Homo
sapiens BRCA1`, 100% identity) in 0.015s having never contacted NCBI --
the answer came off the on-disk sqlite cache a previous run wrote. The
returned dict is identical in shape and content to a live one, so
nothing downstream could tell the difference, and `compare_reports.py`
went on to call the replayed hit "Newly Matched".

The five `VersionStatus` members are all about VERSION IDENTIFIABILITY
(not_consulted / unknown / timestamp_only / hash_only / version_known);
none of them says anything about where a run's answer came from. So
this is not a missing value in an existing axis -- it is a missing
axis, which is why the fix adds `RetrievalMode` alongside `status`
rather than a sixth status.

`TestTheOldVocabularyCannotExpressThis` is the standing proof of that
premise: it asserts the STATUS axis is identical for a live run and a
replayed one. It passes before and after the fix, by design -- if it
ever fails, the justification for the second axis has changed and this
whole file should be re-argued, not patched.
"""

import os
import tempfile
import threading
import unittest
from unittest import mock

from database.blast_client import BLASTClient, _BlastDiskCache
from pipeline.provenance import (
    RETRIEVAL_MODE_LABELS,
    RetrievalMode,
    RunProvenanceCollector,
    VersionStatus,
)
from report.report_generator import ReportGenerator

SEQUENCE = "ACGT" * 40
BLAST_HITS = {
    "hits": [{"title": "NM_007294.4 Homo sapiens BRCA1", "identity": 100.0}],
    "hit_count": 1,
}


def _client(cache_dir):
    """
    A BLASTClient with a real on-disk cache and nothing else -- built
    without `__init__` so no config load, no biopython check, and no
    blast+ autoinstall runs. Only the attributes `search()` touches are
    set; if `search()` grows a new dependency this raises AttributeError
    rather than silently testing a different path.
    """
    client = BLASTClient.__new__(BLASTClient)
    client.disabled = False
    client.mode = "remote"
    client.local_db_path = None
    client.max_concurrent = 1
    client._result_cache = {}
    client._retrieval_lock = threading.Lock()
    client._live_query_count = 0
    client._cache_replay_count = 0
    client._disk_cache = _BlastDiskCache(os.path.join(cache_dir, "blast_cache.sqlite"))
    return client


class _CacheDirCase(unittest.TestCase):
    def setUp(self):
        # ignore_cleanup_errors: `_BlastDiskCache._connect` opens a new
        # sqlite connection per operation and never closes it (the
        # `with` block commits but does not close), so on Windows the
        # file is still handle-locked at teardown. Pre-existing, out of
        # this pass's scope, noted so the flag is not mistaken for
        # sloppiness.
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.cache_dir = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    def a_replaying_client(self):
        """
        A client that can ONLY answer from cache: a first client makes
        one live search (populating the sqlite file), then a second,
        independent client is handed the same directory. The second is
        what a later run looks like. Returns that second client, with
        the replay already performed.
        """
        first = _client(self.cache_dir)
        with mock.patch.object(BLASTClient, "_search_remote", return_value=BLAST_HITS):
            first.search(SEQUENCE)

        second = _client(self.cache_dir)
        with mock.patch.object(BLASTClient, "_search_remote") as never_called:
            result = second.search(SEQUENCE)
            self.assertEqual(
                never_called.call_count,
                0,
                "the second client contacted BLAST, so nothing here is testing a cache replay. "
                "Treat this as the test going blind (the cache key or the disk-cache lookup moved), "
                "not as the reporting feature being absent.",
            )
        self.assertEqual(result["hit_count"], 1, "the replay returned no hits, so there is no answer to attribute")
        return second


class TestACacheReplayIsIndistinguishableFromALiveQuery(_CacheDirCase):
    """The premise. Without this, the rest is a fix for nothing."""

    def test_a_replayed_search_returns_the_same_real_hits_with_no_contact(self):
        first = _client(self.cache_dir)
        with mock.patch.object(BLASTClient, "_search_remote", return_value=BLAST_HITS):
            live_result = first.search(SEQUENCE)

        second = _client(self.cache_dir)
        with mock.patch.object(BLASTClient, "_search_remote") as never_called:
            replayed_result = second.search(SEQUENCE)

        self.assertEqual(never_called.call_count, 0)
        self.assertEqual(live_result, replayed_result)
        self.assertEqual(replayed_result["hits"][0]["title"], "NM_007294.4 Homo sapiens BRCA1")


class TestTheOldVocabularyCannotExpressThis(_CacheDirCase):
    """
    Passes before AND after the fix -- deliberately. It pins the reason
    the new axis exists, and would fail only if `VersionStatus` itself
    were later given cache semantics, which is a decision that should
    break a test rather than pass silently.
    """

    def test_status_is_byte_identical_for_a_live_run_and_a_replayed_run(self):
        def record_a_run():
            collector = RunProvenanceCollector()
            collector.record("BLAST", VersionStatus.VERSION_KNOWN, version="blastn 2.16.0+")
            return collector.get("BLAST")

        live, replayed = record_a_run(), record_a_run()
        self.assertEqual(live.status, replayed.status)
        self.assertEqual(live.status.value, "version_known")

    def test_no_version_status_member_says_anything_about_retrieval(self):
        self.assertEqual(
            [member.value for member in VersionStatus],
            ["not_consulted", "unknown", "timestamp_only", "hash_only", "version_known"],
        )
        for member in VersionStatus:
            self.assertNotIn("cache", member.value)
            self.assertNotIn("replay", member.value)

    def test_query_timestamp_cannot_stand_in_for_it_either(self):
        """
        The obvious "just look at the timestamp" answer does not work:
        `record` stamps `query_timestamp` with now() regardless, so a
        replayed run carries a fresh-looking time just like a live one.
        """
        collector = RunProvenanceCollector()
        collector.record("BLAST", VersionStatus.VERSION_KNOWN, version="blastn 2.16.0+")
        self.assertIsNotNone(collector.get("BLAST").query_timestamp)


class TestTheNewAxisExpressesIt(_CacheDirCase):
    def test_a_replaying_client_reports_only_cache_replays(self):
        counts = self.a_replaying_client().retrieval_counts()
        self.assertEqual(counts, {"live": 0, "cache_replay": 1})

    def test_provenance_records_cache_replay(self):
        collector = RunProvenanceCollector()
        collector.record("BLAST", VersionStatus.VERSION_KNOWN, version="blastn 2.16.0+")
        collector.note_retrieval("BLAST", RetrievalMode.CACHE_REPLAY)

        record = collector.get("BLAST")
        self.assertIs(record.retrieval, RetrievalMode.CACHE_REPLAY)
        # and the version axis is untouched by it
        self.assertIs(record.status, VersionStatus.VERSION_KNOWN)
        self.assertEqual(record.version, "blastn 2.16.0+")

    def test_a_live_run_is_not_reported_as_a_replay(self):
        """Negative control."""
        client = _client(self.cache_dir)
        with mock.patch.object(BLASTClient, "_search_remote", return_value=BLAST_HITS):
            client.search(SEQUENCE)
        self.assertEqual(client.retrieval_counts(), {"live": 1, "cache_replay": 0})

        collector = RunProvenanceCollector()
        collector.note_retrieval("BLAST", RetrievalMode.LIVE)
        self.assertIs(collector.get("BLAST").retrieval, RetrievalMode.LIVE)

    def test_an_in_memory_hit_is_counted_as_neither(self):
        """
        A same-run duplicate sequence must not inflate either bucket:
        the first occurrence was already attributed, and counting the
        repeat would make one live query look like two.
        """
        client = _client(self.cache_dir)
        with mock.patch.object(BLASTClient, "_search_remote", return_value=BLAST_HITS):
            client.search(SEQUENCE)
            client.search(SEQUENCE)
            client.search(SEQUENCE)
        self.assertEqual(client.retrieval_counts(), {"live": 1, "cache_replay": 0})

    def test_an_untracked_source_claims_nothing(self):
        """
        Only BLAST is wired this pass. Every other source must stay
        None -- NOT silently acquire a "retrieved live" claim.
        """
        collector = RunProvenanceCollector()
        collector.record("ClinVar", VersionStatus.UNKNOWN, notes="no release version exposed")
        self.assertIsNone(collector.get("ClinVar").retrieval)


class TestMergeSemantics(unittest.TestCase):
    def test_live_and_replay_together_become_mixed(self):
        for first, second in (
            (RetrievalMode.LIVE, RetrievalMode.CACHE_REPLAY),
            (RetrievalMode.CACHE_REPLAY, RetrievalMode.LIVE),
        ):
            collector = RunProvenanceCollector()
            collector.note_retrieval("BLAST", first)
            collector.note_retrieval("BLAST", second)
            self.assertIs(
                collector.get("BLAST").retrieval,
                RetrievalMode.MIXED,
                f"{first.value} then {second.value} must not collapse to either one alone",
            )

    def test_not_retrieved_never_overwrites_a_real_retrieval(self):
        collector = RunProvenanceCollector()
        collector.note_retrieval("BLAST", RetrievalMode.CACHE_REPLAY)
        collector.note_retrieval("BLAST", RetrievalMode.NOT_RETRIEVED)
        self.assertIs(collector.get("BLAST").retrieval, RetrievalMode.CACHE_REPLAY)

    def test_repeating_a_mode_does_not_escalate_it_to_mixed(self):
        collector = RunProvenanceCollector()
        for _ in range(3):
            collector.note_retrieval("BLAST", RetrievalMode.CACHE_REPLAY)
        self.assertIs(collector.get("BLAST").retrieval, RetrievalMode.CACHE_REPLAY)

    def test_a_later_status_capture_does_not_erase_the_retrieval_mode(self):
        """
        The orchestrator re-sweeps dataset provenance after the variant
        loop, so a `record` call CAN land after a retrieval is known.
        `record` rebuilds the record wholesale; if it dropped this
        field the cache-replay finding would vanish in the ordinary
        case.
        """
        collector = RunProvenanceCollector()
        collector.note_retrieval("BLAST", RetrievalMode.CACHE_REPLAY)
        collector.record("BLAST", VersionStatus.VERSION_KNOWN, version="blastn 2.16.0+")
        self.assertIs(collector.get("BLAST").retrieval, RetrievalMode.CACHE_REPLAY)

    def test_an_unknown_source_is_ignored_not_invented(self):
        collector = RunProvenanceCollector()
        collector.note_retrieval("Not A Real Source", RetrievalMode.LIVE)
        self.assertIsNone(collector.get("Not A Real Source"))


class TestCountsMapToTheHonestLabel(unittest.TestCase):
    """`RetrievalMode.from_counts` -- the rule, tested without a pipeline."""

    def test_the_four_cases(self):
        for live, replayed, expected in (
            (0, 0, RetrievalMode.NOT_RETRIEVED),
            (3, 0, RetrievalMode.LIVE),
            (0, 3, RetrievalMode.CACHE_REPLAY),
            (1, 9, RetrievalMode.MIXED),
            (9, 1, RetrievalMode.MIXED),
        ):
            with self.subTest(live=live, cache_replay=replayed):
                self.assertIs(RetrievalMode.from_counts(live, replayed), expected)

    def test_a_mostly_cached_run_is_never_called_live(self):
        """
        The specific misreport this whole change exists to prevent: one
        live query alongside nine replays must not license the report to
        say the data came off the wire.
        """
        self.assertIsNot(RetrievalMode.from_counts(1, 9), RetrievalMode.LIVE)
        self.assertIsNot(RetrievalMode.from_counts(1, 9), RetrievalMode.CACHE_REPLAY)


class TestTheOrchestratorActuallyCallsIt(_CacheDirCase):
    """
    Finding 5's lesson: the mechanism existing and the mechanism being
    WIRED are two different claims, and only the second one shows up in
    a report.

    What the first two tests below check: the orchestrator's capture
    method, driven against a stub `self`. What they do NOT check -- and
    an earlier version of this docstring wrongly claimed they did -- is
    that anything ever CALLS it; they would pass just as happily if the
    call site were deleted. `test_run_still_calls_the_capture_method`
    covers that gap, with its own stated limits.
    """

    def test_the_capture_method_records_a_replay_on_the_collector(self):
        from pipeline.orchestrator import GeperPipeline

        class _Stub:
            pass

        stub = _Stub()
        stub.provenance = RunProvenanceCollector()
        stub.blast_client = self.a_replaying_client()

        GeperPipeline._capture_blast_retrieval_provenance(stub)
        self.assertIs(stub.provenance.get("BLAST").retrieval, RetrievalMode.CACHE_REPLAY)

    def test_a_broken_blast_client_cannot_break_the_run(self):
        """
        Every other provenance capture in the orchestrator swallows its
        own failure rather than killing a pipeline run. This one must
        behave the same way -- a provenance nicety is never worth a
        failed clinical run.
        """
        from pipeline.orchestrator import GeperPipeline

        class _Stub:
            pass

        class _Exploding:
            def retrieval_counts(self):
                raise RuntimeError("boom")

        stub = _Stub()
        stub.provenance = RunProvenanceCollector()
        stub.blast_client = _Exploding()

        GeperPipeline._capture_blast_retrieval_provenance(stub)  # must not raise
        self.assertIsNone(stub.provenance.get("BLAST").retrieval)

    def test_run_still_calls_the_capture_method(self):
        """
        A source-level check, and only that: it proves the call appears
        in `GeperPipeline.run`, NOT that it executes on any particular
        path through it. Weak on purpose -- reaching the real call site
        needs a full pipeline run against live external services, which
        does not belong in this file. Its job is narrow: catch the
        capture method being orphaned by a refactor, which is precisely
        how Finding 5's data came to be computed and then discarded.
        """
        import inspect

        from pipeline.orchestrator import GeperPipeline

        self.assertIn(
            "self._capture_blast_retrieval_provenance()",
            inspect.getsource(GeperPipeline.run),
            "GeperPipeline.run no longer calls _capture_blast_retrieval_provenance, so no real run "
            "records where its BLAST evidence came from -- the reports go back to being unable to "
            "tell a live query from a cache replay.",
        )


class TestItReachesTheReport(unittest.TestCase):
    def _document(self, retrieval):
        collector = RunProvenanceCollector()
        collector.record("BLAST", VersionStatus.VERSION_KNOWN, version="blastn 2.16.0+")
        if retrieval is not None:
            collector.note_retrieval("BLAST", retrieval)
        return {"provenance": collector.to_list(), "run_complete": True}

    def test_markdown_states_the_source_was_not_contacted(self):
        markdown = ReportGenerator().generate(self._document(RetrievalMode.CACHE_REPLAY))
        self.assertIn("REPLAYED FROM LOCAL CACHE", markdown)
        self.assertIn("the source itself was not contacted this run", markdown)

    def test_markdown_of_a_live_run_makes_no_such_claim(self):
        """Negative control -- the label must not be unconditional."""
        markdown = ReportGenerator().generate(self._document(RetrievalMode.LIVE))
        self.assertNotIn("REPLAYED FROM LOCAL CACHE", markdown)
        self.assertIn("retrieved live from the source this run", markdown)

    def test_an_untracked_source_renders_no_retrieval_claim_at_all(self):
        markdown = ReportGenerator().generate(self._document(None))
        self.assertNotIn("Retrieval:", markdown)

    def test_both_renderers_use_the_same_label_map(self):
        """
        The status labels above exist as two hand-maintained copies kept
        in step only by a comment. This axis is defined once in
        `pipeline/provenance.py` and imported by both, so a drift test
        is cheap: assert both modules resolve to the same object.
        """
        from report import report_generator, summary

        self.assertIs(report_generator.RETRIEVAL_MODE_LABELS, RETRIEVAL_MODE_LABELS)
        self.assertIs(summary.RETRIEVAL_MODE_LABELS, RETRIEVAL_MODE_LABELS)


if __name__ == "__main__":
    unittest.main()
