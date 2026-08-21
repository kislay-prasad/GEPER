"""
tests/test_clingen_dosage_lookup.py
─────────────────────────────────────
Regression tests for pipeline/clingen/lookup.py (ClinGenDosageLookup +
DosageSensitivityRecord, created by commit 871f23d). 324 lines of
production code feeding is_lof_intolerant() in pipeline/constraint/
lookup.py -- PVS1-adjacent ACMG evidence -- shipped with ZERO tests.
The board card for that work closed with "READY FOR PAM TO ADD TESTS."
It never happened until now.

SCOPE, per Meredith's own intent answers (she wrote the module): the
local-file parser was tested only against a synthetic fixture at the
time; the API path was UNVERIFIED and disabled by default. This file
reflects that split honestly rather than pretending equal coverage:

  - TestBackendSelection, TestLocalFileParsing, TestDosageSensitivity-
    RecordSufficientEvidenceMembership, TestLookupGeneCaching,
    TestIsDosageSufficientForLof exercise the LOCAL path and pure logic
    thoroughly -- these run for real, no network, no mocking beyond
    writing fixture files to tmp_path.

  - TestApiBackendPath covers what CAN be verified from the repo: that
    the module correctly SELECTS the API backend and correctly handles
    a non-2xx / malformed response (both exercised via mocking
    `pipeline.utils.http._api_get`, the same boundary
    test_clinvar.py-style tests in this suite mock at). What it does
    NOT and CANNOT cover, stated explicitly per the standing rule
    ("ships with a test, or an explicit note saying why it can't"):
    whether ClinGen's REAL API response shape actually matches what
    `_api_lookup_gene` expects (`dosageSensitivity`/`dosage` keys,
    `haploinsufficiencyScore` etc.) -- there is no network route to
    clinicalgenome.org from this environment, and the module's own
    docstring already flags this path as unverified. A test that
    mocked a response shaped exactly like the code expects would prove
    only that the code trusts its own assumptions, not that ClinGen's
    real API matches them -- so that specific claim is left as an
    explicit, named gap rather than a false-confidence test.

  - TestEveryKnownDosageScoreLabelMatchesSufficiencyMembership replaces
    the original TestHaploinsufficiencyScoreThirtyAndFortyAreNot-
    SufficientEvidence, which documented (did not endorse) a real
    finding surfaced while writing this coverage: scores 30 and 40 were
    incorrectly read as sufficient evidence under a bare `>= 3`
    comparison. That finding was carded HIGH
    ([[clingen-dosage-score-40-reads-as-sufficient-evidence-for-lof]])
    and fixed in commit c3e23ab (DOSAGE_SUFFICIENT_EVIDENCE_SCORE, a
    scalar threshold, replaced by DOSAGE_SUFFICIENT_EVIDENCE_SCORES, an
    explicit membership set -- {3} today). Per that original class's
    own docstring instruction ("retire, don't flip" -- since adopted as
    the floor's standard for any defect-documenting test), the class
    was DELETED rather than edited in place; see git history for the
    original. Its replacement asserts the CORRECT, now-shipped
    behavior, parametrized over every key in DOSAGE_SCORE_LABELS (not
    just the two codes that were wrong) per Kelly's own review
    suggestion -- a test enumerating only 30 and 40 would repeat the
    original bug's blacklist shape.

  - TestKnownHeldDefectClinGenFallbackExceptionCollapsesToTolerant
    documents (does NOT endorse) the human-held defect named in the
    dispatch: [[clingen-lof-fallback-collapses-lookup-failure-into-
    confirmed-tolerant]]. Does not touch pipeline/clingen/lookup.py or
    pipeline/constraint/lookup.py -- imports GnomadConstraintLookup
    (constraint/lookup.py) read-only, exactly as any test imports
    production code, and injects a duck-typed fake fallback object (not
    a real ClinGenDosageLookup) that raises on
    `is_dosage_sufficient_for_lof()`, since ClinGenDosageLookup's own
    real implementation cannot be made to raise there without a
    contrived, unrealistic setup (`_api_get` itself never raises -- see
    pipeline/utils/http.py's own docstring, "Never raises"). See that
    test class's own docstring for why it exists and how to retire it.
"""

from pathlib import Path
from unittest import mock

import pytest

from pipeline.clingen.lookup import (
    DOSAGE_SCORE_LABELS,
    DOSAGE_SUFFICIENT_EVIDENCE_SCORES,
    ClinGenDosageLookup,
    DosageSensitivityRecord,
)

CLINGEN_LOGGER = "geper.pipeline.clingen.lookup"


# ── Fixture builders ─────────────────────────────────────────────────────────


def _kb_export_tsv(rows: list[str]) -> str:
    """The interactive KB-export shape: a title/date/URL preamble, then
    an ALLCAPS header row, no leading '#'."""
    preamble = "ClinGen Dosage Sensitivity Curations\nDownloaded: 2026-01-01\nhttps://search.clinicalgenome.org/kb/dosage\n"
    header = "GENE SYMBOL\tHAPLOINSUFFICIENCY SCORE\tHAPLOINSUFFICIENCY DESCRIPTION\tTRIPLOSENSITIVITY SCORE\tTRIPLOSENSITIVITY DESCRIPTION\n"
    return preamble + header + "".join(rows)


def _ftp_mirror_tsv(rows: list[str]) -> str:
    """The ftp-mirror shape: #-prefixed preamble lines, where the LAST
    '#' line is the real title-case header."""
    preamble = "#Title: ClinGen Dosage Sensitivity\n#Date: 2026-01-01\n"
    header = "#Gene Symbol\tHaploinsufficiency Score\tHaploinsufficiency Description\tTriplosensitivity Score\tTriplosensitivity Description\n"
    return preamble + header + "".join(rows)


def _write(tmp_path: Path, content: str, name: str = "clingen_dosage.tsv") -> Path:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


def _cfg(local_path=None, enabled=True, api_enabled=False):
    cg = {"enabled": enabled}
    if local_path is not None:
        cg["dosage_sensitivity_path"] = str(local_path)
    cg["api_enabled"] = api_enabled
    return {"clingen": cg}


# ── Backend selection (__init__) ─────────────────────────────────────────────


class TestBackendSelection:
    def test_disabled_never_loads_and_lookup_returns_none(self, tmp_path, caplog):
        path = _write(tmp_path, _kb_export_tsv(["BRCA1\t3\tSufficient\t0\tNo evidence\n"]))
        with caplog.at_level("INFO", logger=CLINGEN_LOGGER):
            clingen = ClinGenDosageLookup(cfg=_cfg(local_path=path, enabled=False))
        assert clingen._backend == "disabled"
        assert clingen.lookup_gene("BRCA1") is None
        assert any("disabled" in r.getMessage() for r in caplog.records)

    def test_valid_local_file_selects_local_backend(self, tmp_path):
        path = _write(tmp_path, _kb_export_tsv(["BRCA1\t3\tSufficient\t0\tNo evidence\n"]))
        clingen = ClinGenDosageLookup(cfg=_cfg(local_path=path))
        assert clingen._backend == "local"
        assert clingen.lookup_gene("BRCA1") is not None

    def test_missing_local_file_with_api_disabled_falls_to_disabled(self, tmp_path, caplog):
        missing = tmp_path / "does_not_exist.tsv"
        with caplog.at_level("WARNING", logger=CLINGEN_LOGGER):
            clingen = ClinGenDosageLookup(cfg=_cfg(local_path=missing, api_enabled=False))
        assert clingen._backend == "disabled"
        assert any("not found" in r.getMessage() for r in caplog.records)

    def test_missing_local_file_with_api_enabled_falls_to_api(self, tmp_path):
        missing = tmp_path / "does_not_exist.tsv"
        clingen = ClinGenDosageLookup(cfg=_cfg(local_path=missing, api_enabled=True))
        assert clingen._backend == "api"

    def test_no_local_path_configured_and_api_enabled_selects_api(self):
        clingen = ClinGenDosageLookup(cfg=_cfg(local_path=None, api_enabled=True))
        assert clingen._backend == "api"

    def test_no_local_path_configured_and_api_disabled_selects_disabled(self):
        clingen = ClinGenDosageLookup(cfg=_cfg(local_path=None, api_enabled=False))
        assert clingen._backend == "disabled"

    def test_local_file_that_fails_to_parse_falls_back_to_api_when_enabled(self, tmp_path, caplog):
        """A file that exists (passes `os.path.isfile`) but whose
        `_load_local` raises for any reason -- not a parsing edge case
        the parser itself tolerates, a genuine failure -- must be
        caught by __init__ and fall back, exactly like a corrupt-file
        failure in the sibling ClinVar/gnomAD loaders this suite
        already pins. Simulated by patching `_load_local` itself
        (rather than trying to construct a real file that reliably
        fails to open cross-platform) -- this tests __init__'s own
        try/except wrapping, not `_load_local`'s parsing logic, which
        is already covered directly above."""
        path = _write(tmp_path, "irrelevant -- _load_local is mocked below\n")
        with mock.patch.object(
            ClinGenDosageLookup, "_load_local", side_effect=OSError("simulated disk read failure")
        ):
            with caplog.at_level("WARNING", logger=CLINGEN_LOGGER):
                clingen = ClinGenDosageLookup(cfg=_cfg(local_path=path, api_enabled=True))
        assert clingen._backend == "api"
        assert any(
            "Failed to load dosage-sensitivity file" in r.getMessage() for r in caplog.records
        )

    def test_local_file_that_fails_to_parse_falls_back_to_disabled_when_api_off(self, tmp_path):
        path = _write(tmp_path, "irrelevant -- _load_local is mocked below\n")
        with mock.patch.object(
            ClinGenDosageLookup, "_load_local", side_effect=OSError("simulated disk read failure")
        ):
            clingen = ClinGenDosageLookup(cfg=_cfg(local_path=path, api_enabled=False))
        assert clingen._backend == "disabled"
        assert clingen.lookup_gene("BRCA1") is None

    def test_empty_config_defaults_to_enabled_no_local_no_api_is_disabled(self):
        """cfg=None / cfg={} must behave sanely: enabled by default
        (matches ClinVar/gnomAD siblings' own "enabled: True" default),
        but with nothing configured to actually read from."""
        clingen = ClinGenDosageLookup(cfg=None)
        assert clingen._enabled is True
        assert clingen._backend == "disabled"


# ── Local file parsing ───────────────────────────────────────────────────────


class TestLocalFileParsing:
    def test_kb_export_shape_parses(self, tmp_path):
        path = _write(
            tmp_path, _kb_export_tsv(["BRCA1\t3\tSufficient evidence\t1\tLittle evidence\n"])
        )
        clingen = ClinGenDosageLookup(cfg=_cfg(local_path=path))
        rec = clingen.lookup_gene("BRCA1")
        assert rec is not None
        assert rec.haploinsufficiency_score == 3
        assert rec.haploinsufficiency_description == "Sufficient evidence"
        assert rec.triplosensitivity_score == 1
        assert rec.backend_used == "local"

    def test_ftp_mirror_shape_parses(self, tmp_path):
        path = _write(
            tmp_path, _ftp_mirror_tsv(["TP53\t3\tSufficient evidence\t2\tSome evidence\n"])
        )
        clingen = ClinGenDosageLookup(cfg=_cfg(local_path=path))
        rec = clingen.lookup_gene("TP53")
        assert rec is not None
        assert rec.haploinsufficiency_score == 3
        assert rec.triplosensitivity_score == 2

    def test_plus_separator_row_is_skipped(self, tmp_path):
        rows = ["+++++++++++++++++++++++++++++\n", "MLH1\t3\tSufficient\t0\tNo evidence\n"]
        path = _write(tmp_path, _kb_export_tsv(rows))
        clingen = ClinGenDosageLookup(cfg=_cfg(local_path=path))
        assert clingen.lookup_gene("MLH1") is not None
        assert len(clingen._by_gene) == 1  # the +++ row must not have become a bogus gene entry

    def test_duplicate_gene_rows_last_one_wins(self, tmp_path):
        rows = [
            "BRCA2\t1\tLittle evidence\t0\tNo evidence\n",
            "BRCA2\t3\tSufficient evidence\t0\tNo evidence\n",
        ]
        path = _write(tmp_path, _kb_export_tsv(rows))
        clingen = ClinGenDosageLookup(cfg=_cfg(local_path=path))
        rec = clingen.lookup_gene("BRCA2")
        assert rec.haploinsufficiency_score == 3  # the later row, not the first

    def test_gene_symbol_is_stored_and_looked_up_case_insensitively(self, tmp_path):
        path = _write(tmp_path, _kb_export_tsv(["scn5a\t3\tSufficient\t0\tNo evidence\n"]))
        clingen = ClinGenDosageLookup(cfg=_cfg(local_path=path))
        assert clingen.lookup_gene("SCN5A") is not None
        assert clingen.lookup_gene("scn5a") is not None
        assert clingen.lookup_gene("ScN5a") is not None

    def test_row_with_blank_gene_symbol_is_skipped(self, tmp_path):
        rows = ["\t3\tSufficient\t0\tNo evidence\n", "PTEN\t3\tSufficient\t0\tNo evidence\n"]
        path = _write(tmp_path, _kb_export_tsv(rows))
        clingen = ClinGenDosageLookup(cfg=_cfg(local_path=path))
        assert len(clingen._by_gene) == 1
        assert clingen.lookup_gene("PTEN") is not None

    def test_malformed_score_becomes_none_not_a_crash(self, tmp_path):
        path = _write(tmp_path, _kb_export_tsv(["APC\tNot Assessed\tno data\t\t\n"]))
        clingen = ClinGenDosageLookup(cfg=_cfg(local_path=path))
        rec = clingen.lookup_gene("APC")
        assert rec is not None
        assert rec.haploinsufficiency_score is None
        assert rec.triplosensitivity_score is None

    def test_no_recognizable_header_treated_as_empty_not_a_crash(self, tmp_path, caplog):
        path = _write(tmp_path, "Some Column\tAnother Column\nfoo\tbar\n")
        with caplog.at_level("WARNING", logger=CLINGEN_LOGGER):
            clingen = ClinGenDosageLookup(cfg=_cfg(local_path=path))
        assert (
            clingen._backend == "local"
        )  # the file loaded without raising -- it was just empty of genes
        assert len(clingen._by_gene) == 0
        assert clingen.lookup_gene("ANYTHING") is None
        assert any("No recognizable" in r.getMessage() for r in caplog.records)

    def test_csv_extension_uses_comma_delimiter(self, tmp_path):
        content = "GENE SYMBOL,HAPLOINSUFFICIENCY SCORE,HAPLOINSUFFICIENCY DESCRIPTION,TRIPLOSENSITIVITY SCORE,TRIPLOSENSITIVITY DESCRIPTION\nMYH7,3,Sufficient,0,No evidence\n"
        path = _write(tmp_path, content, name="clingen_dosage.csv")
        clingen = ClinGenDosageLookup(cfg=_cfg(local_path=path))
        rec = clingen.lookup_gene("MYH7")
        assert rec is not None
        assert rec.haploinsufficiency_score == 3

    def test_gene_not_present_in_dataset_returns_none(self, tmp_path):
        path = _write(tmp_path, _kb_export_tsv(["BRCA1\t3\tSufficient\t0\tNo evidence\n"]))
        clingen = ClinGenDosageLookup(cfg=_cfg(local_path=path))
        assert clingen.lookup_gene("SOME_GENE_NOT_IN_FILE") is None


# ── DosageSensitivityRecord.is_lof_sufficient() ──────────────────────────────


class TestDosageSensitivityRecordSufficientEvidenceMembership:
    """Membership, not a threshold -- ClinGen's scale is not ordinal
    above 3, so is_lof_sufficient() checks set membership
    (DOSAGE_SUFFICIENT_EVIDENCE_SCORES) rather than `>=` (see
    TestEveryKnownDosageScoreLabelMatchesSufficiencyMembership below
    for why that distinction matters and what used to go wrong)."""

    def _rec(self, score):
        return DosageSensitivityRecord(gene="X", haploinsufficiency_score=score)

    def test_every_score_in_the_sufficient_set_is_sufficient(self):
        # Iterates the real set rather than hardcoding which score(s)
        # it contains, so this doesn't silently stop testing anything
        # if the set's membership ever changes.
        assert len(DOSAGE_SUFFICIENT_EVIDENCE_SCORES) > 0, (
            "the sufficient-evidence set must not be empty"
        )
        for score in DOSAGE_SUFFICIENT_EVIDENCE_SCORES:
            assert self._rec(score).is_lof_sufficient() is True

    def test_score_not_in_the_sufficient_set_is_not_sufficient(self):
        not_sufficient = 2
        assert not_sufficient not in DOSAGE_SUFFICIENT_EVIDENCE_SCORES
        assert self._rec(not_sufficient).is_lof_sufficient() is False

    def test_score_zero_is_not_sufficient(self):
        assert self._rec(0).is_lof_sufficient() is False

    def test_score_none_is_not_sufficient(self):
        assert self._rec(None).is_lof_sufficient() is False


class TestEveryKnownDosageScoreLabelMatchesSufficiencyMembership:
    """
    Replaces TestHaploinsufficiencyScoreThirtyAndFortyAreNotSufficientEvidence
    (retired -- deleted, not edited in place, per that class's own
    docstring instruction, since adopted as the floor's standing rule
    for any defect-documenting test: "retire, don't flip"). That class
    documented a real finding surfaced while writing this coverage --
    scores 30 ("Gene associated with autosomal recessive phenotype")
    and 40 ("Dosage sensitivity unlikely") were incorrectly read as
    sufficient evidence for haploinsufficiency under a bare `score >= 3`
    comparison, the opposite of what both codes actually mean. Carded
    HIGH ([[clingen-dosage-score-40-reads-as-sufficient-evidence-for-lof]])
    and fixed in c3e23ab: DOSAGE_SUFFICIENT_EVIDENCE_SCORE (a scalar
    threshold) was replaced outright by DOSAGE_SUFFICIENT_EVIDENCE_SCORES
    (an explicit membership set, {3} today) -- no inequality survives in
    the module, which is deliberate: keeping a scalar named ..._SCORE
    would have left the exact shape of the bug available for the next
    `>=` to reintroduce it.

    PARAMETRIZED OVER EVERY KEY IN DOSAGE_SCORE_LABELS, not just 30 and
    40, per Kelly's own review comment: a test enumerating only the two
    codes known wrong today has the same blind spot the original bug's
    blacklist shape did. This asserts the property -- membership in
    DOSAGE_SUFFICIENT_EVIDENCE_SCORES is_lof_sufficient()'s only
    criterion -- against every code that exists today, not just the
    two that happened to be wrong once.

    NOT testing geper/pipeline/pvs1/utils.py::lof_mechanism_from_clingen,
    which also reads score 30 but maps it to *established* -- correctly,
    per Kelly's own flag, because it asks a different question ("is LoF
    a disease mechanism for this gene at all" vs. kim's "is losing ONE
    allele sufficient"). The two modules are not inconsistent; they
    answer different questions. No test here should ever assert 30 ->
    True on the strength of geper's mapping.
    """

    @pytest.mark.parametrize("score", sorted(DOSAGE_SCORE_LABELS), ids=lambda s: f"score_{s}")
    def test_membership_matches_the_sufficient_set_for_every_known_code(self, score):
        rec = DosageSensitivityRecord(gene="X", haploinsufficiency_score=score)
        expected = score in DOSAGE_SUFFICIENT_EVIDENCE_SCORES
        assert rec.is_lof_sufficient() is expected, (
            f"score {score} ({DOSAGE_SCORE_LABELS[score]!r}): expected is_lof_sufficient() "
            f"== {expected} per DOSAGE_SUFFICIENT_EVIDENCE_SCORES membership"
        )

    def test_none_score_is_not_sufficient(self):
        rec = DosageSensitivityRecord(gene="X", haploinsufficiency_score=None)
        assert rec.is_lof_sufficient() is False


# ── lookup_gene() caching + dispatch ─────────────────────────────────────────


class TestLookupGeneCaching:
    def test_empty_gene_symbol_returns_none_without_touching_cache(self, tmp_path):
        path = _write(tmp_path, _kb_export_tsv(["BRCA1\t3\tSufficient\t0\tNo evidence\n"]))
        clingen = ClinGenDosageLookup(cfg=_cfg(local_path=path))
        assert clingen.lookup_gene("") is None
        assert clingen.cache_stats() == {"hits": 0, "misses": 0, "size": 0}

    def test_repeated_lookup_hits_cache_and_counts_correctly(self, tmp_path):
        path = _write(tmp_path, _kb_export_tsv(["BRCA1\t3\tSufficient\t0\tNo evidence\n"]))
        clingen = ClinGenDosageLookup(cfg=_cfg(local_path=path))
        clingen.lookup_gene("BRCA1")
        clingen.lookup_gene("BRCA1")
        clingen.lookup_gene("BRCA1")
        stats = clingen.cache_stats()
        assert stats["misses"] == 1
        assert stats["hits"] == 2
        assert stats["size"] == 1

    def test_a_negative_result_is_also_cached(self, tmp_path):
        """A gene absent from the dataset must still be cached (as
        None) rather than re-scanning `_by_gene` on every call."""
        path = _write(tmp_path, _kb_export_tsv(["BRCA1\t3\tSufficient\t0\tNo evidence\n"]))
        clingen = ClinGenDosageLookup(cfg=_cfg(local_path=path))
        clingen.lookup_gene("NOT_PRESENT")
        clingen.lookup_gene("NOT_PRESENT")
        stats = clingen.cache_stats()
        assert stats["misses"] == 1
        assert stats["hits"] == 1

    def test_clear_cache_resets_stats_and_forces_a_fresh_lookup(self, tmp_path):
        path = _write(tmp_path, _kb_export_tsv(["BRCA1\t3\tSufficient\t0\tNo evidence\n"]))
        clingen = ClinGenDosageLookup(cfg=_cfg(local_path=path))
        clingen.lookup_gene("BRCA1")
        clingen.clear_cache()
        assert clingen.cache_stats() == {"hits": 0, "misses": 0, "size": 0}
        clingen.lookup_gene("BRCA1")
        assert clingen.cache_stats()["misses"] == 1

    def test_disabled_backend_lookup_returns_none_and_is_still_cached(self):
        clingen = ClinGenDosageLookup(cfg=_cfg(local_path=None, api_enabled=False))
        clingen.lookup_gene("ANY_GENE")
        clingen.lookup_gene("ANY_GENE")
        stats = clingen.cache_stats()
        assert stats["misses"] == 1
        assert stats["hits"] == 1


# ── is_dosage_sufficient_for_lof() ───────────────────────────────────────────


class TestIsDosageSufficientForLof:
    def test_true_when_score_meets_threshold(self, tmp_path):
        path = _write(tmp_path, _kb_export_tsv(["BRCA1\t3\tSufficient\t0\tNo evidence\n"]))
        clingen = ClinGenDosageLookup(cfg=_cfg(local_path=path))
        assert clingen.is_dosage_sufficient_for_lof("BRCA1") is True

    def test_false_when_score_below_threshold(self, tmp_path):
        path = _write(tmp_path, _kb_export_tsv(["BRCA1\t2\tSome evidence\t0\tNo evidence\n"]))
        clingen = ClinGenDosageLookup(cfg=_cfg(local_path=path))
        assert clingen.is_dosage_sufficient_for_lof("BRCA1") is False

    def test_false_when_gene_not_in_dataset(self, tmp_path):
        path = _write(tmp_path, _kb_export_tsv(["BRCA1\t3\tSufficient\t0\tNo evidence\n"]))
        clingen = ClinGenDosageLookup(cfg=_cfg(local_path=path))
        assert clingen.is_dosage_sufficient_for_lof("GENE_NOT_CURATED") is False

    def test_false_when_backend_disabled(self):
        clingen = ClinGenDosageLookup(cfg=_cfg(local_path=None, api_enabled=False))
        assert clingen.is_dosage_sufficient_for_lof("ANY_GENE") is False


# ── API backend: what CAN be verified from the repo ──────────────────────────


class TestApiBackendPath:
    """
    See this file's own module docstring for the explicit gap this
    class does NOT close: whether ClinGen's real API response shape
    matches what `_api_lookup_gene` expects. Not verifiable from this
    environment (no network route to clinicalgenome.org) -- the module
    docstring already says so, and this class does not pretend
    otherwise. What follows is what genuinely IS testable: backend
    selection, and the code's own handling of the boundary it controls
    (`_api_get`'s return contract), mocked at exactly that boundary the
    same way test_clinvar.py's/test_config_validator.py's tests in
    this suite already do.
    """

    def test_api_backend_selected_and_used_when_no_local_file(self, monkeypatch):
        clingen = ClinGenDosageLookup(cfg=_cfg(local_path=None, api_enabled=True))
        assert clingen._backend == "api"

        def fake_api_get(url, params, timeout, max_retries, logger):
            assert "BRCA1" in url
            resp = type(
                "Resp",
                (),
                {
                    "status_code": 200,
                    "json": lambda self: {
                        "dosageSensitivity": {
                            "haploinsufficiencyScore": 3,
                            "haploinsufficiencyDescription": "Sufficient evidence",
                            "triplosensitivityScore": 0,
                            "triplosensitivityDescription": "No evidence",
                        }
                    },
                },
            )()
            return resp

        monkeypatch.setattr("pipeline.clingen.lookup._api_get", fake_api_get)
        rec = clingen.lookup_gene("BRCA1")
        assert rec is not None
        assert rec.haploinsufficiency_score == 3
        assert rec.backend_used == "api"

    def test_api_returns_none_response_yields_none_record(self, monkeypatch):
        """`_api_get` never raises -- it returns None on final failure
        (its own docstring). This is the ONLY failure shape the API
        path needs to handle from that boundary, and it must not
        crash."""
        clingen = ClinGenDosageLookup(cfg=_cfg(local_path=None, api_enabled=True))
        monkeypatch.setattr("pipeline.clingen.lookup._api_get", lambda *a, **kw: None)
        assert clingen.lookup_gene("BRCA1") is None

    def test_api_non_2xx_status_yields_none_record(self, monkeypatch):
        clingen = ClinGenDosageLookup(cfg=_cfg(local_path=None, api_enabled=True))
        resp = type("Resp", (), {"status_code": 404})()
        monkeypatch.setattr("pipeline.clingen.lookup._api_get", lambda *a, **kw: resp)
        assert clingen.lookup_gene("BRCA1") is None

    def test_api_non_json_response_is_handled_not_a_crash(self, monkeypatch, caplog):
        clingen = ClinGenDosageLookup(cfg=_cfg(local_path=None, api_enabled=True))

        def raises_value_error(self):
            raise ValueError("not json")

        resp = type("Resp", (), {"status_code": 200, "json": raises_value_error})()
        monkeypatch.setattr("pipeline.clingen.lookup._api_get", lambda *a, **kw: resp)
        with caplog.at_level("WARNING", logger=CLINGEN_LOGGER):
            result = clingen.lookup_gene("BRCA1")
        assert result is None
        assert any("Non-JSON" in r.getMessage() for r in caplog.records)

    def test_api_response_with_no_dosage_data_yields_none_record(self, monkeypatch):
        """A 200 response that simply has nothing under either
        expected key -- a gene ClinGen's API knows nothing about."""
        clingen = ClinGenDosageLookup(cfg=_cfg(local_path=None, api_enabled=True))
        resp = type("Resp", (), {"status_code": 200, "json": lambda self: {}})()
        monkeypatch.setattr("pipeline.clingen.lookup._api_get", lambda *a, **kw: resp)
        assert clingen.lookup_gene("BRCA1") is None


# ── Known, human-held defect: documented, not endorsed ───────────────────────


class TestKnownHeldDefectClinGenFallbackExceptionCollapsesToTolerant:
    """
    Documents the human-held defect named in the dispatch:
    [[clingen-lof-fallback-collapses-lookup-failure-into-confirmed-tolerant]].
    THE HUMAN HAS RULED HOLD ON THE FIX. This test exists to make the
    defect visible and give the eventual fix something concrete to
    flip, NOT to certify the current behavior as correct.

    THE PROPERTY THIS DOCUMENTS: `GnomadConstraintLookup.is_lof_intolerant()`
    (pipeline/constraint/lookup.py:302-327) wraps its call into the
    ClinGen fallback in a bare `except Exception: ... return False`. If
    that call raises for ANY reason, the exception is swallowed and
    `is_lof_intolerant()` returns bare `False` -- identical to what it
    returns for a gene ClinGen has genuinely curated as dosage-
    tolerant. A caller has no way to distinguish "confirmed tolerant"
    from "the lookup broke."

    WHY A FAKE FALLBACK, NOT A REAL ClinGenDosageLookup: the real
    `ClinGenDosageLookup.is_dosage_sufficient_for_lof()` cannot be made
    to raise without a contrived setup -- `pipeline/utils/http._api_get`
    documents itself as "Never raises" (returns None on final
    failure), and the local-file path only raises during `__init__`
    (already caught there), not during `lookup_gene()`. The fallback
    parameter is duck-typed (`constraint/lookup.py` calls
    `self._clingen_fallback.is_dosage_sufficient_for_lof(gene_symbol)`
    with no ClinGenDosageLookup import or isinstance check), so a
    minimal object with that one method exercises the exact real
    interface without needing an unrealistic trigger.

    DOES NOT TOUCH pipeline/clingen/lookup.py or
    pipeline/constraint/lookup.py -- imports GnomadConstraintLookup
    read-only, same as any test importing production code.

    WHEN KELLY'S FIX LANDS: this test's assertion will need to flip
    (or the class deleted and replaced with a real regression test for
    whatever the fixed contract becomes -- e.g. a raised exception
    surfacing as `None`/a distinct sentinel rather than `False`). Until
    then, deleting or "fixing" this test to make it pass differently
    would silently re-hide the defect -- leave it exactly as failing
    or passing as it currently is unless the underlying code changes.
    """

    class _RaisingFallback:
        def is_dosage_sufficient_for_lof(self, gene_symbol: str) -> bool:
            raise RuntimeError(
                "simulated ClinGen fallback failure -- e.g. a corrupt local file mid-run"
            )

    def test_swallowed_fallback_exception_is_indistinguishable_from_confirmed_tolerant(self):
        from pipeline.constraint.lookup import GnomadConstraintLookup

        # No local TSV, gnomAD API disabled -> backend "disabled" ->
        # lookup() returns None -> falls through to the ClinGen
        # fallback, exactly the path the held defect describes.
        constraint = GnomadConstraintLookup(
            cfg={"gnomad": {"enabled": False}},
            clingen_fallback=self._RaisingFallback(),
        )

        # CURRENT (held-as-wrong) behavior: the raised exception is
        # swallowed and this returns bare False, exactly as it would
        # for a gene ClinGen genuinely confirmed as dosage-tolerant.
        result = constraint.is_lof_intolerant("ANY_GENE")
        assert result is False, (
            "this assertion documents the CURRENT, HELD-AS-WRONG behavior -- it is not a "
            "correctness claim. If this now fails, either the held defect was fixed (update/"
            "retire this test to match the new contract) or something else changed; do not "
            "'fix' this test by changing the assertion without checking which."
        )
