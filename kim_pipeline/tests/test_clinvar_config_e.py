"""
tests/test_clinvar_config_e.py
────────────────────────────────
Regression tests for the ClinVar "Path E" fix (commit b2e819c):
`pipeline.config_validator._validate_clinvar_keys` warns when the
`clinvar:` config section contains a key it does not recognise, most
importantly the `tsv_path` / `tsv_gz_path` mixup (`tsv_path` is a real
key, but for `gnomad_constraint`, not `clinvar` -- see that function's
own docstring).

Shipped with no persisted tests (verified by hand only) -- this file
is what turns that one-off manual verification into something that
stays true. Covers, per the follow-up dispatch:
  - the mistyped-key warning itself, including the cross-section hint
    (tests below, class TestTsvPathMistake)
  - the three silent controls PLUS the human-called-out `clinvar: None`
    case (class TestSilentControls)
  - the cross-section guard: `gnomad_constraint.tsv_path` must never
    warn, since that key is legitimate there (class TestCrossSectionGuard)
  - that ClinVarLookup's own three pre-existing log paths (A: disabled,
    B: successful local load, C: corrupt-file load failure) are
    unaffected by, and stay distinguishable from, the new warning
    (class TestLookupPathsUnaffectedByKeyValidation)
  - that `_CLINVAR_KNOWN_KEYS` cannot silently drift from the keys
    `ClinVarLookup.__init__` actually reads (class TestKnownKeysMatchWhatLookupReads)

`_validate_clinvar_keys` is called directly throughout (no pipeline
run needed, per Kelly's own note) with a bare `{"clinvar": ...}` dict
-- it only ever reads `cfg["clinvar"]`, so a minimal dict is a faithful
input, not a shortcut.
"""

import gzip
import inspect
import re
from pathlib import Path

from pipeline.clinvar.lookup import ClinVarLookup
from pipeline.config_validator import _CLINVAR_KNOWN_KEYS, _validate_clinvar_keys

VALIDATOR_LOGGER = "geper.pipeline.config_validator"
LOOKUP_LOGGER = "geper.pipeline.clinvar.lookup"


class TestTsvPathMistake:
    """Item 3: the mistyped-key warning itself."""

    def test_tsv_path_under_clinvar_warns_and_names_the_key(self, caplog):
        with caplog.at_level("WARNING", logger=VALIDATOR_LOGGER):
            _validate_clinvar_keys({"clinvar": {"tsv_path": "/data/x.tsv.gz"}})

        records = [r for r in caplog.records if r.name == VALIDATOR_LOGGER]
        assert len(records) == 1
        message = records[0].getMessage()
        assert "'tsv_path'" in message
        assert "unrecognised key" in message

    def test_tsv_path_warning_carries_the_gnomad_constraint_hint(self, caplog):
        with caplog.at_level("WARNING", logger=VALIDATOR_LOGGER):
            _validate_clinvar_keys({"clinvar": {"tsv_path": "/data/x.tsv.gz"}})

        message = caplog.records[0].getMessage()
        assert "gnomad_constraint" in message
        assert "tsv_gz_path" in message  # the corrected spelling must be suggested


class TestSilentControls:
    """Item 4, plus the human-called-out `clinvar: None` case: all of
    these must produce NO warning -- each is a legitimate way of
    selecting the API default, not a misconfiguration."""

    def _assert_silent(self, cfg, caplog):
        with caplog.at_level("WARNING", logger=VALIDATOR_LOGGER):
            _validate_clinvar_keys(cfg)
        assert [r for r in caplog.records if r.name == VALIDATOR_LOGGER] == []

    def test_absent_clinvar_section_is_silent(self, caplog):
        self._assert_silent({}, caplog)

    def test_empty_clinvar_mapping_is_silent(self, caplog):
        self._assert_silent({"clinvar": {}}, caplog)

    def test_all_three_canonical_keys_is_silent(self, caplog):
        self._assert_silent(
            {"clinvar": {"enabled": True, "tsv_gz_path": "/data/x.tsv.gz", "ncbi_api_key": "abc"}},
            caplog,
        )

    def test_clinvar_none_is_silent(self, caplog):
        """The human's own instruction: 'confirm the clinvar: None case
        explicitly rather than leaving it to isinstance.' A YAML config
        with a bare `clinvar:` key and nothing under it parses to
        `{"clinvar": None}` -- this must not warn or crash."""
        self._assert_silent({"clinvar": None}, caplog)


class TestCrossSectionGuard:
    """Item 5, the one that matters most per the dispatch: `tsv_path`
    is a REAL, legitimate key under `gnomad_constraint`, not a typo
    there. A check that fires on it would be worse than the bug this
    fix closes -- and this is also what would catch an over-broad key
    check (e.g. one that scanned the whole cfg for 'tsv_path' instead
    of scoping to `clinvar:`) introduced later."""

    def test_gnomad_constraint_tsv_path_alone_does_not_warn(self, caplog):
        with caplog.at_level("WARNING", logger=VALIDATOR_LOGGER):
            _validate_clinvar_keys({"gnomad_constraint": {"tsv_path": "/data/gnomad.tsv"}})
        assert [r for r in caplog.records if r.name == VALIDATOR_LOGGER] == []

    def test_gnomad_constraint_tsv_path_alongside_valid_clinvar_section_does_not_warn(self, caplog):
        cfg = {
            "gnomad_constraint": {"tsv_path": "/data/gnomad.tsv"},
            "clinvar": {"tsv_gz_path": "/data/clinvar.tsv.gz"},
        }
        with caplog.at_level("WARNING", logger=VALIDATOR_LOGGER):
            _validate_clinvar_keys(cfg)
        assert [r for r in caplog.records if r.name == VALIDATOR_LOGGER] == []


class TestLookupPathsUnaffectedByKeyValidation:
    """Items 6 and 7: ClinVarLookup's three pre-existing log paths (A:
    disabled, B: successful local load, C: corrupt-file load failure,
    which falls back to the API) must be completely unchanged by the
    new key-validation warning, and the two mechanisms -- the new
    config-time warning and ClinVarLookup's own init-time
    info/warning messages -- must stay distinguishable: different
    logger, different wording, and (for path C in particular) the new
    warning must not fire at all when every key used IS recognised."""

    def _write_valid_tsv_gz(self, tmp_path: Path) -> Path:
        gz_path = tmp_path / "variant_summary.txt.gz"
        header = (
            "#AlleleID\tType\tGeneSymbol\tClinicalSignificance\tReviewStatus\t"
            "NumberSubmitters\tChromosome\tStart\tReferenceAllele\tAlternateAllele\n"
        )
        with gzip.open(gz_path, "wt", encoding="utf-8") as fh:
            fh.write(header)
        return gz_path

    def _write_corrupt_tsv_gz(self, tmp_path: Path) -> Path:
        """A file that exists (so ClinVarLookup attempts to load it)
        but is not valid gzip -- reproduces the real-world 'corrupt
        download' failure mode."""
        path = tmp_path / "variant_summary.txt.gz"
        path.write_bytes(b"this is not gzip data, it is garbage\x00\x01\x02")
        return path

    def test_path_a_disabled_messages_intact_and_no_key_warning(self, caplog, tmp_path):
        cfg = {"clinvar": {"enabled": False}}

        with caplog.at_level("INFO"):
            ClinVarLookup(cfg=cfg)
        lookup_messages = [r.getMessage() for r in caplog.records if r.name == LOOKUP_LOGGER]
        assert "ClinVar lookup skipped (disabled)" in lookup_messages
        assert any("clinvar.enabled: false" in m for m in lookup_messages)

        caplog.clear()
        with caplog.at_level("WARNING", logger=VALIDATOR_LOGGER):
            _validate_clinvar_keys(cfg)
        assert [r for r in caplog.records if r.name == VALIDATOR_LOGGER] == []

    def test_path_b_successful_load_messages_intact_and_no_key_warning(self, caplog, tmp_path):
        gz_path = self._write_valid_tsv_gz(tmp_path)
        cfg = {"clinvar": {"tsv_gz_path": str(gz_path)}}

        with caplog.at_level("INFO"):
            ClinVarLookup(cfg=cfg)
        lookup_messages = [r.getMessage() for r in caplog.records if r.name == LOOKUP_LOGGER]
        assert any("Loaded local TSV" in m for m in lookup_messages)
        assert not any("Failed to load" in m for m in lookup_messages)
        assert not any("unrecognised key" in m for m in lookup_messages)

        caplog.clear()
        with caplog.at_level("WARNING", logger=VALIDATOR_LOGGER):
            _validate_clinvar_keys(cfg)
        assert [r for r in caplog.records if r.name == VALIDATOR_LOGGER] == []

    def test_path_c_corrupt_file_warning_intact_and_distinguishable_from_key_warning(
        self, caplog, tmp_path
    ):
        gz_path = self._write_corrupt_tsv_gz(tmp_path)
        cfg = {"clinvar": {"tsv_gz_path": str(gz_path)}}

        with caplog.at_level("WARNING"):
            ClinVarLookup(cfg=cfg)
        lookup_warnings = [r.getMessage() for r in caplog.records if r.name == LOOKUP_LOGGER]
        assert len(lookup_warnings) == 1
        assert "Failed to load local TSV" in lookup_warnings[0]
        # The two failure modes must stay textually distinguishable --
        # a corrupt file is not an unrecognised key, and must never be
        # reported as one.
        assert "unrecognised key" not in lookup_warnings[0]

        # tsv_gz_path IS a recognised key -- the new validator must
        # stay silent even though the file it points to is garbage.
        # Validation is about the KEY, never the value.
        caplog.clear()
        with caplog.at_level("WARNING", logger=VALIDATOR_LOGGER):
            _validate_clinvar_keys(cfg)
        assert [r for r in caplog.records if r.name == VALIDATOR_LOGGER] == []


class TestKnownKeysMatchWhatLookupReads:
    """Flagged as worth pinning if cheap: `_CLINVAR_KNOWN_KEYS` is a
    hand-maintained set in config_validator.py; the keys it must track
    actually live in ClinVarLookup.__init__ (lookup.py:101-103). If
    `clinvar:` grows a fourth key and nobody updates the set, the new
    key would warn as unrecognised while working fine. Source-inspects
    __init__ rather than touching either production file (both are out
    of scope for this dispatch) -- this is a real mechanism, not a
    restatement of the constant, because it fails if the two drift."""

    def test_known_keys_set_matches_keys_read_by_init(self):
        source = inspect.getsource(ClinVarLookup.__init__)
        keys_read = set(re.findall(r'cv_cfg\.get\(\s*["\'](\w+)["\']', source))
        assert keys_read, (
            "regex found nothing -- ClinVarLookup.__init__ no longer reads cv_cfg.get(...) as expected"
        )
        assert keys_read == set(_CLINVAR_KNOWN_KEYS)
