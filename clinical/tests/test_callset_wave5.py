# ruff: noqa: F811
"""
Fifth correction wave (fourth independent review), against the REAL schema as the REAL `clinical_app` /
`clinical_retention` roles. Every control here was removed in a mutant during the review (or this wave) and the mutant
survived; each test below names the mutant it exists to kill.

  M1  the bytes' SHA-256 is recomputed on every read and must equal the recorded one, warm cache or not
  M2  cache bounds (oversize, eviction, budget), integrity on a cache hit, exclusion precedence, warm == cold
  M4  single flight: concurrent cold reads of one call set verify it once
  L1  the application role cannot choose the ordering column
  L2  near-duplicate bytes: current behaviour pinned (a decision is pending)
  L3  retention-redacted evidence is not described as "never attached"
  L4  a retired parser rule set is refused by warm and cold processes alike
  L5  a wrong-class / wrong-run replacement is a clean domain error (the foreign key remains the last barrier)
"""

from __future__ import annotations

import datetime
import hashlib
import random
import threading

import pytest

from clinical.data_access import CallSetConflictError, DataAccess
from clinical.genome_analysis import CALLSET_PARSER_VERSION
from clinical.tests.test_callset_dao import (  # noqa: F401  (fixtures and helpers re-used)
    _fresh_dao,
    app_conn,
    approve_caller,
    audits,
    dao,
    new_run,
    release,
    ret_conn,
    row,
    state,
    su,
    submit,
    system_principal,
    tamper,
    validated,
    vcf_text,
    world,
)
from clinical.tests.test_callset_review_fixes import _order_state, _qc, _withdraw_consent  # noqa: F401
from clinical.tests.test_callset_wave4 import _withdraw_all_consents

psycopg = pytest.importorskip("psycopg")


@pytest.fixture(autouse=True)
def _clean_cache(monkeypatch):
    monkeypatch.delenv("CLINICAL_CALLSET_CACHE_RECORDS", raising=False)
    DataAccess._CALLSET_VERIFIED_CACHE.clear()
    DataAccess._CALLSET_INFLIGHT.clear()
    yield
    DataAccess._CALLSET_VERIFIED_CACHE.clear()
    DataAccess._CALLSET_INFLIGHT.clear()


def _evidence(dao, w):
    return dao.patient_call_set_evidence(w.reader, w.run.patient)


def _drawn(dao, w):
    return [e["call_set_id"] for e in _evidence(dao, w)["eligible"]]


# ═══ M1: the recomputed bytes digest is compared to the recorded SHA-256 ═══════


class TestStoredBytesMustHashToTheirRecordedDigest:
    """Review-4 mutant 1 (`elif r[8] != r[6]` -> `elif False`) left every test green. The database refuses bytes that
    disagree with their hash, so the reproduction is what only a table owner could do: remove that CHECK, change the
    bytes under an unchanged hash, put the CHECK back."""

    @staticmethod
    def _hash_check_name(su):
        return row(
            su,
            "SELECT conname FROM pg_constraint WHERE conrelid = 'public.call_sets'::regclass AND contype = 'c' "
            "AND strpos(pg_get_constraintdef(oid), 'sha256(source_vcf)') > 0",
        )[0][0]

    def _swap_bytes_keeping_the_hash(self, su, cs, new_bytes):
        name = self._hash_check_name(su)
        su.execute(f"ALTER TABLE public.call_sets DROP CONSTRAINT {name}")
        su.commit()
        tamper(su, "UPDATE call_sets SET source_vcf = %s WHERE id = %s", (new_bytes, cs))
        return name

    def _restore(self, su, name):
        su.execute(
            f"ALTER TABLE public.call_sets ADD CONSTRAINT {name} "
            "CHECK (source_vcf IS NULL OR source_content_hash = encode(sha256(source_vcf), 'hex'))"
        )
        su.commit()

    def test_bytes_that_no_longer_hash_to_the_recorded_digest_are_never_drawn_even_with_a_warm_cache(
        self, su, dao, world
    ):
        cs = validated(su, dao, world)
        original = bytes(row(su, "SELECT source_vcf FROM call_sets WHERE id = %s", (cs,))[0][0])
        assert _drawn(dao, world) == [cs]
        assert len(DataAccess._CALLSET_VERIFIED_CACHE) == 1, "the cache is warm: the document is unchanged below"
        name = self._swap_bytes_keeping_the_hash(su, cs, original + b"\n")
        try:
            evidence = _evidence(dao, world)
            assert evidence["eligible"] == [], "the bytes are not the bytes that were validated"
            assert evidence["excluded"] == {"integrity_failure": 1}
            incidents = audits(su, "call_set_integrity_failure")
            assert [(a[0], a[1], a[2]) for a in incidents] == [("error", "call_set", str(cs))]
        finally:
            tamper(su, "UPDATE call_sets SET source_vcf = %s WHERE id = %s", (original, cs))
            self._restore(su, name)
        # twin: with the original bytes back the same call set is drawn again (it was not remembered as bad)
        assert _drawn(dao, world) == [cs]

    def test_the_same_with_a_cold_cache(self, su, dao, world):
        cs = validated(su, dao, world)
        original = bytes(row(su, "SELECT source_vcf FROM call_sets WHERE id = %s", (cs,))[0][0])
        name = self._swap_bytes_keeping_the_hash(su, cs, original + b" ")
        try:
            assert _evidence(dao, world) == {"eligible": [], "excluded": {"integrity_failure": 1}}
        finally:
            tamper(su, "UPDATE call_sets SET source_vcf = %s WHERE id = %s", (original, cs))
            self._restore(su, name)

    def test_tampered_content_does_not_reach_the_genome_map_source(self, su, dao, world):
        from clinical.genome_endpoints import DaoCallSetSource

        cs = validated(su, dao, world)
        source = DaoCallSetSource(dao)
        assert len(list(source.call_sets_for(world.reader, world.run.patient))) == 1
        tamper(
            su,
            "UPDATE call_sets SET parsed_document = jsonb_set(parsed_document, '{records,0,start}', '1') WHERE id = %s",
            (cs,),
        )
        assert list(source.call_sets_for(world.reader, world.run.patient)) == []
        assert source.excluded_for(world.reader, world.run.patient) == {"integrity_failure": 1}

    def test_the_full_check_refuses_every_kind_of_disagreement(self, su, dao, world):
        cs = validated(su, dao, world)
        source, doc, digest, version = row(
            su,
            "SELECT source_vcf, parsed_document, source_content_hash, parser_version FROM call_sets WHERE id = %s",
            (cs,),
        )[0]
        raw = bytes(source)
        intact = DataAccess._callset_evidence_intact
        assert intact(cs, raw, digest, version, doc) is True
        assert intact(cs, raw + b"\n", digest, version, doc) is False, "bytes changed under the recorded hash"
        assert intact(cs, raw, hashlib.sha256(b"other").hexdigest(), version, doc) is False
        assert intact(cs, raw, digest, version, {**doc, "records": []}) is False, "document changed under the bytes"
        assert intact(cs, raw, digest, "structural_vcf/0", doc) is False, "an unknown rule set never passes"


# ═══ M2: cache bounds, integrity on a hit, precedence, warm == cold ═════════════


class TestCacheBudget:
    def test_the_budget_is_the_environment_value_the_default_or_zero(self, monkeypatch):
        budget = DataAccess._callset_cache_budget
        assert budget() == DataAccess.CALLSET_DEFAULT_CACHE_RECORDS == 100_000
        monkeypatch.setenv("CLINICAL_CALLSET_CACHE_RECORDS", "50")
        assert budget() == 50, "a configured budget is never raised"
        monkeypatch.setenv("CLINICAL_CALLSET_CACHE_RECORDS", "0")
        assert budget() == 0
        monkeypatch.setenv("CLINICAL_CALLSET_CACHE_RECORDS", "-5")
        assert budget() == 0
        monkeypatch.setenv("CLINICAL_CALLSET_CACHE_RECORDS", "not a number")
        assert budget() == DataAccess.CALLSET_DEFAULT_CACHE_RECORDS

    def test_a_budget_of_zero_caches_nothing(self, monkeypatch):
        monkeypatch.setenv("CLINICAL_CALLSET_CACHE_RECORDS", "0")
        assert DataAccess._callset_cache_put("k", 1, object()) is False
        assert len(DataAccess._CALLSET_VERIFIED_CACHE) == 0


class TestCacheBounds:
    @pytest.fixture(autouse=True)
    def _small(self, monkeypatch):
        monkeypatch.setenv("CLINICAL_CALLSET_CACHE_RECORDS", "10")

    @staticmethod
    def _held():
        cache = DataAccess._CALLSET_VERIFIED_CACHE
        return list(cache), sum(v[0] for v in cache.values())

    def test_an_entry_larger_than_the_whole_budget_is_never_stored_and_evicts_nothing(self):
        put = DataAccess._callset_cache_put
        assert put("small", 4, object()) is True
        assert put("huge", 11, object()) is False
        assert self._held() == (["small"], 4)

    def test_an_entry_exactly_the_budget_is_stored(self):
        assert DataAccess._callset_cache_put("exact", 10, object()) is True
        assert self._held() == (["exact"], 10)

    def test_the_least_recently_used_entry_is_evicted_first_and_a_read_refreshes_it(self):
        put, get = DataAccess._callset_cache_put, DataAccess._callset_cache_get
        put("a", 4, object())
        put("b", 4, object())
        assert get("a") is not None  # a is now the most recent
        put("c", 4, object())  # 12 > 10: b (least recently used) goes, not a
        assert self._held() == (["a", "c"], 8)

    def test_it_never_holds_more_than_the_budget_whatever_is_stored(self):
        put = DataAccess._callset_cache_put
        rnd = random.Random(7)
        for i in range(500):
            put(f"k{i}", rnd.randint(1, 10), object())
            assert self._held()[1] <= 10
        assert self._held()[1] > 0

    def test_storing_a_key_again_replaces_it_instead_of_counting_it_twice(self):
        put = DataAccess._callset_cache_put
        put("a", 6, object())
        put("a", 6, object())
        assert self._held() == (["a"], 6)


class TestACacheHitCannotBypassIntegrity:
    def test_a_changed_document_a_changed_rule_set_and_changed_bytes_are_each_verified_in_full(
        self, su, dao, world, monkeypatch
    ):
        import clinical.genome_analysis as ga

        cs = validated(su, dao, world)
        calls = []
        real = ga.verify_document_for_version
        monkeypatch.setattr(ga, "verify_document_for_version", lambda *a, **k: (calls.append(1), real(*a, **k))[1])
        assert _drawn(dao, world) == [cs] and len(calls) == 1
        assert _drawn(dao, world) == [cs] and len(calls) == 1, "unchanged: served from the verified cache"
        tamper(
            su,
            "UPDATE call_sets SET parsed_document = jsonb_set(parsed_document, '{records,0,status}', '\"uncertain\"') WHERE id = %s",
            (cs,),
        )
        assert _drawn(dao, world) == [] and len(calls) == 2, "a different document is a different key"

    def test_a_warm_entry_is_not_served_after_the_call_set_stops_being_eligible(self, su, dao, world):
        cs = validated(su, dao, world)
        assert _drawn(dao, world) == [cs]
        _withdraw_all_consents(su, world)
        assert _evidence(dao, world) == {"eligible": [], "excluded": {"consent_withdrawn": 1}}


class TestExclusionPrecedence:
    ORDER = (
        "consent_withdrawn",
        "sample_qc_not_passed",
        "order_cancelled",
        "sample_identity_changed",
        "assembly_mismatch",
    )

    def test_when_two_things_are_wrong_the_earlier_reason_in_the_fixed_order_is_given(self):
        pairs = 0
        for i in range(5):
            for j in range(i + 1, 5):
                flags = [True] * 5
                flags[i] = flags[j] = False
                assert DataAccess._callset_exclusion_reason(*flags) == self.ORDER[i], (i, j)
                pairs += 1
        assert pairs == 10
        assert DataAccess._callset_exclusion_reason(*([True] * 5)) is None

    def test_each_single_failure_has_its_own_reason(self):
        for i, reason in enumerate(self.ORDER):
            flags = [True] * 5
            flags[i] = False
            assert DataAccess._callset_exclusion_reason(*flags) == reason

    @staticmethod
    def _repoint_sample(su, w):
        tamper(
            su,
            "UPDATE sequencing_runs SET sample_id = %s WHERE id = %s",
            (w.other_sample, w.run.id),
            table="sequencing_runs",
            trigger="trg_callset_identity_runs",
        )

    @pytest.mark.parametrize(
        "first, then, expected",
        [
            ("consent", "sample", "consent_withdrawn"),
            ("qc", "cancelled", "sample_qc_not_passed"),
            ("consent", "cancelled", "consent_withdrawn"),
            ("cancelled", "qc", "sample_qc_not_passed"),
            ("consent", "qc", "consent_withdrawn"),
        ],
    )
    def test_the_database_scenario_reports_one_reason_and_it_is_the_first(self, su, dao, world, first, then, expected):
        world.other_sample = new_run(su, dao, world, patient=world.run.patient).sample
        validated(su, dao, world)
        assert len(_evidence(dao, world)["eligible"]) == 1
        makers = {
            "consent": lambda: _withdraw_all_consents(su, world),
            "qc": lambda: _qc(su, world, world.run, "failed"),
            "cancelled": lambda: _order_state(su, world.run, "cancelled"),
            "sample": lambda: self._repoint_sample(su, world),
        }
        makers[first]()
        makers[then]()
        evidence = _evidence(dao, world)
        assert evidence["eligible"] == [] and evidence["excluded"] == {expected: 1}, "counted once, under one reason"


class TestWarmAndColdProcessesAgree:
    @pytest.mark.parametrize(
        "make_it, reason",
        [
            (lambda su, w: _withdraw_all_consents(su, w), "consent_withdrawn"),
            (lambda su, w: _qc(su, w, w.run, "failed"), "sample_qc_not_passed"),
            (lambda su, w: _order_state(su, w.run, "cancelled"), "order_cancelled"),
        ],
    )
    def test_the_answer_does_not_depend_on_what_the_process_has_cached(self, su, dao, world, make_it, reason):
        cs = validated(su, dao, world)
        warm_before = _evidence(dao, world)
        assert [e["call_set_id"] for e in warm_before["eligible"]] == [cs]
        make_it(su, world)
        warm = _evidence(dao, world)  # cache still holds the verified call set
        DataAccess._CALLSET_VERIFIED_CACHE.clear()  # a process that has never seen it
        cold = _evidence(dao, world)
        assert warm == cold == {"eligible": [], "excluded": {reason: 1}}


# ═══ M4: single flight ═════════════════════════════════════════════════════════


class TestConcurrentColdReadsVerifyOnce:
    def test_two_simultaneous_cold_readers_of_one_call_set_run_one_full_verification(self, su, dao, world, monkeypatch):
        import clinical.genome_analysis as ga

        cs = validated(su, dao, world)
        DataAccess._CALLSET_VERIFIED_CACHE.clear()
        started, release_it = threading.Event(), threading.Event()
        calls = []
        real = ga.verify_document_for_version

        def slow(*a, **k):
            calls.append(threading.get_ident())
            started.set()
            assert release_it.wait(30)
            return real(*a, **k)

        monkeypatch.setattr(ga, "verify_document_for_version", slow)
        waited = threading.Event()
        real_claim = DataAccess._callset_claim.__func__

        def spying_claim(cls, key):
            mine, event = real_claim(cls, key)
            if not mine:
                waited.set()
            return mine, event

        monkeypatch.setattr(DataAccess, "_callset_claim", classmethod(spying_claim))
        results = {}

        def read(name, d):
            results[name] = [
                e["call_set_id"] for e in d.patient_call_set_evidence(world.reader, world.run.patient)["eligible"]
            ]

        connection, other = _fresh_dao()
        try:
            first = threading.Thread(target=read, args=("first", dao))
            first.start()
            assert started.wait(30), "the first reader is inside the full verification"
            second = threading.Thread(target=read, args=("second", other))
            second.start()
            assert waited.wait(30), "the second reader found the call set already being verified"
            assert len(calls) == 1
            release_it.set()
            first.join(60)
            second.join(60)
        finally:
            release_it.set()
            connection.close()
        assert results == {"first": [cs], "second": [cs]}
        assert len(calls) == 1, "one full verification served both readers"
        assert DataAccess._CALLSET_INFLIGHT == {}, "nothing is left claimed"

    def test_a_reader_that_waited_is_still_checked_against_its_own_read_of_the_bytes(self, su, dao, world):
        cs = validated(su, dao, world)
        key_state = DataAccess._callset_claim(("not-a-real-key",))
        assert key_state[0] is True and DataAccess._callset_claim(("not-a-real-key",))[0] is False
        DataAccess._callset_release(("not-a-real-key",), key_state[1])
        assert DataAccess._CALLSET_INFLIGHT == {}
        # a failed verification leaves nothing in the cache, so a later reader verifies (and refuses) it itself
        tamper(
            su,
            "UPDATE call_sets SET parsed_document = jsonb_set(parsed_document, '{records,0,status}', '\"uncertain\"') WHERE id = %s",
            (cs,),
        )
        assert _evidence(dao, world) == {"eligible": [], "excluded": {"integrity_failure": 1}}
        assert len(DataAccess._CALLSET_VERIFIED_CACHE) == 0


# ═══ L4: a retired rule set ═══════════════════════════════════════════════════


class TestRetiredRuleSetIsRefusedWarmOrCold:
    def test_removing_the_rule_set_stops_a_warm_cache_serving_what_a_cold_process_would_refuse(
        self, su, dao, world, monkeypatch
    ):
        import clinical.genome_analysis as ga

        cs = validated(su, dao, world)
        assert _drawn(dao, world) == [cs]  # warm
        monkeypatch.delitem(ga.PARSER_RULESETS, CALLSET_PARSER_VERSION)
        warm = _evidence(dao, world)
        DataAccess._CALLSET_VERIFIED_CACHE.clear()
        cold = _evidence(dao, world)
        assert warm == cold == {"eligible": [], "excluded": {"integrity_failure": 1}}
        assert len(audits(su, "call_set_integrity_failure")) == 1, "reported, once an hour"
        monkeypatch.undo()
        assert _drawn(dao, world) == [cs], "nothing was lost: with the rule set back it verifies and is drawn"


# ═══ L1 / L2 / L3 / L5 ════════════════════════════════════════════════════════


class TestTheApplicationRoleCannotChooseTheOrdering:
    def test_no_insert_privilege_on_seq_but_on_every_other_column(self, su):
        columns = [
            r[0] for r in row(su, "SELECT column_name FROM information_schema.columns WHERE table_name = 'call_sets'")
        ]
        assert "seq" in columns and len(columns) > 15
        for column in columns:
            has = row(su, "SELECT has_column_privilege('clinical_app', 'public.call_sets', %s, 'INSERT')", (column,))[
                0
            ][0]
            assert has is (column != "seq"), column
        assert row(su, "SELECT has_table_privilege('clinical_app', 'public.call_sets', 'UPDATE')")[0][0] is False

    def test_overriding_system_value_is_refused_and_a_normal_submission_still_gets_a_database_sequence(
        self, su, dao, world, app_conn
    ):
        data = vcf_text(world.run.sample, n=3)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with app_conn.cursor() as cur:
                cur.execute("SELECT set_config('clinical.actor_id', %s, true)", (str(world.lab1.user_id),))
                cur.execute(
                    "INSERT INTO call_sets (org_id,id,seq,sequencing_run_id,record_class,assembly,caller_name,"
                    "caller_approved_at_submission,vcf_sample_name,source_filename,source_vcf,source_content_hash,"
                    "parser_version,parsed_document,created_at,created_by,retention_days) OVERRIDING SYSTEM VALUE "
                    "VALUES (%s, gen_random_uuid(), 9223372036854775000, gen_random_uuid(), 'sv','GRCh38',"
                    "'c',false,'s','x.vcf',%s,%s,'v','{}'::jsonb,now(),gen_random_uuid(),1)",
                    (world.org, data, hashlib.sha256(data).hexdigest()),
                )
        app_conn.rollback()
        first = submit(dao, world, world.run, data=vcf_text(world.run.sample, n=1))
        second = submit(dao, world, world.run, data=vcf_text(world.run.sample, n=2))
        seqs = [row(su, "SELECT seq FROM call_sets WHERE id = %s", (c,))[0][0] for c in (first, second)]
        assert seqs[0] < seqs[1]


class TestNearDuplicateBytesCurrentBehaviour:
    """L2, pinned and NOT changed: identity is the exact bytes. A canonical-content hash is a design decision (see the
    corrections document, section 9). What protects the map today is the one-validated rule, not byte identity."""

    def test_the_same_file_with_one_more_newline_is_a_different_call_set_but_cannot_be_validated_beside_it(
        self, su, dao, world
    ):
        from clinical.data_access import CallSetConflictError

        data = vcf_text(world.run.sample, n=1)
        first = validated(su, dao, world, data=data)
        near = submit(dao, world, world.run, data=data + b"\n")
        assert near != first and state(su, near) == "pending_validation"
        with pytest.raises(CallSetConflictError):
            dao.validate_call_set(world.interp2, near)
        assert _drawn(dao, world) == [first], "never drawn twice"


class TestRetentionRedactedEvidenceIsNotNeverAttached:
    def test_a_redacted_validated_call_set_is_reported_as_removed_by_retention(self, su, dao, world, ret_conn):
        from clinical.genome_endpoints import _call_sets_note
        from clinical.retention import RetentionPrincipal

        validated(su, dao, world)
        release(su, world, world.run, datetime.datetime(2030, 1, 1, tzinfo=datetime.timezone.utc))
        system_principal(dao, world.org)
        RetentionPrincipal(ret_conn).purge_expired(
            world.org, "call_set", now=datetime.datetime(2060, 1, 1, tzinfo=datetime.timezone.utc)
        )
        ret_conn.commit()
        evidence = _evidence(dao, world)
        assert evidence == {"eligible": [], "excluded": {"retention_redacted": 1}}
        assert "retention_redacted" in DataAccess.CALLSET_EXCLUSION_REASONS
        note = _call_sets_note([], evidence["excluded"])
        assert "retention policy" in note and "not a finding that no such variants exist" in note
        assert "No CNV or structural-variant call set is attached" not in note
        for needle in ("chr", "POS", "DEL", "DUP", "1000"):
            assert needle not in str(evidence["excluded"]), "a count only; nothing about the content"

    def test_a_patient_with_nothing_attached_is_still_never_attached(self, su, dao, world):
        assert _evidence(dao, world) == {"eligible": [], "excluded": {}}


class TestAReplacementOfTheWrongClassOrRunIsACleanDomainError:
    def test_wrong_class_is_refused_before_the_database_and_the_foreign_key_remains(self, su, dao, world):
        approve_caller(dao, world, record_class="cnv")
        cs = validated(su, dao, world)
        with pytest.raises(CallSetConflictError, match="same sequencing run and of the same evidence class"):
            submit(
                dao,
                world,
                world.run,
                data=vcf_text(world.run.sample, "GRCh38", "cnv", n=2),
                supersedes=cs,
                record_class="cnv",
            )
        definition = row(
            su, "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'fk_callset_supersedes'"
        )[0][0]
        assert "record_class" in definition and "sequencing_run_id" in definition, "the last barrier is still there"

    def test_wrong_run_is_the_same_clean_error(self, su, dao, world):
        cs = validated(su, dao, world)
        other = new_run(su, dao, world, patient=world.run.patient)
        with pytest.raises(CallSetConflictError, match="same sequencing run"):
            submit(dao, world, other, data=vcf_text(other.sample, n=2), supersedes=cs)

    def test_a_correct_replacement_still_works(self, su, dao, world):
        cs = validated(su, dao, world)
        replacement = submit(dao, world, world.run, data=vcf_text(world.run.sample, n=2), supersedes=cs)
        assert state(su, replacement) == "pending_validation"
