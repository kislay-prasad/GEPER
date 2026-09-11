"""
The two SpliceAI threshold keys are removed -- and they were DEAD before they
were removed. Both halves are asserted here, because a deletion with no proof
that the thing governed nothing is indistinguishable from a deletion that
silently changed a verdict.

THE TWO THRESHOLDS: `acmg_thresholds.pp3_spliceai` (0.2) and
`acmg_thresholds.bp4_spliceai` (0.1), set in `config/default.yaml`, returned by
`GET /config` through `api/main.py::_CONFIG_ALLOWLIST`, and overridable from
the command line with `analyze --spliceai`.

WHY THEY WERE DEAD: the SpliceAI INPUT was closed (b394bfa) -- no path
populates `spliceai_score` any more -- and the PP3/BP4 SpliceAI vote itself was
removed from `classifier.py` (e007401), which also dropped the keys from
`config_validator.py`. After that nothing READ either threshold. A threshold
that governs nothing is worse than an absent one: THE NEXT PERSON READS A
NUMBER THAT GOVERNS NOTHING AND ASSUMES IT GOVERNS SOMETHING.

HOW THIS FILE IS SPLIT, AND WHY THE SPLIT IS THE POINT:

  * `TheDeadKeysAreGone`         -- RED before the removal, GREEN after.
  * `TheKeysNeverGovernedAVerdict` -- GREEN BEFORE AND AFTER. It is the proof
    that the removal changes no classification; if it ever went red at master
    the keys were live and the removal would have been a clinical change
    disguised as cleanup.
  * `LegitimateSpliceEvidenceStillCounts` -- GREEN BEFORE AND AFTER. A removal
    that swept "anything splice-shaped" would pass the first class and break
    this one: canonical splice-donor/acceptor variants are real splice
    evidence that has nothing to do with SpliceAI, and they must still reach
    PVS1.

LICENSING PREMISE, NAMED SO IT CAN BE CHECKED RATHER THAN INFERRED: SpliceAI's
pretrained models are CC BY-NC 4.0 and its code GPL-3.0; both are commercial
blockers, and commercial NABL/ICMR clinical deployment is the stated intent.
If that intent ever changes, the reason for this removal changes with it.
"""

import os
import sys
from unittest.mock import patch

import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.acmg.classifier import AcmgClassifier, VariantEvidence  # noqa: E402
from pipeline.orchestration.shared import run_acmg_evidence_batch  # noqa: E402

_KIM = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DEAD_KEYS = ("pp3_spliceai", "bp4_spliceai")


def _default_thresholds() -> dict:
    with open(os.path.join(_KIM, "config", "default.yaml"), encoding="utf-8") as handle:
        return (yaml.safe_load(handle) or {}).get("acmg_thresholds", {}) or {}


# ─────────────────────────────────────────────────────────────────────────────
# RED BEFORE, GREEN AFTER
# ─────────────────────────────────────────────────────────────────────────────


class TestTheDeadKeysAreGone:
    """Every surface that carried the two keys must stop carrying them."""

    def test_default_config_no_longer_sets_either_key(self):
        present = [k for k in _DEAD_KEYS if k in _default_thresholds()]
        assert not present, (
            f"config/default.yaml still sets {present} -- a threshold that no code "
            "reads, which a reader will assume governs something"
        )

    def test_get_config_no_longer_allowlists_either_key(self):
        # *** READ FROM THE SOURCE WITH `ast`, NOT BY IMPORTING api.main. ***
        # Importing it needs fastapi, which is not installed in every
        # environment this suite runs in -- and an ImportError would make this
        # test RED FOR A REASON THAT HAS NOTHING TO DO WITH THE KEYS. The first
        # draft of this test did exactly that. `_CONFIG_ALLOWLIST` is a pure
        # literal, so evaluating it statically reads the real value.
        import ast

        with open(os.path.join(_KIM, "api", "main.py"), encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        allowlist = None
        for node in ast.walk(tree):
            target = getattr(node, "target", None) or (getattr(node, "targets", None) or [None])[0]
            if (
                isinstance(target, ast.Name)
                and target.id == "_CONFIG_ALLOWLIST"
                and node.value is not None
            ):
                allowlist = ast.literal_eval(node.value)
        assert allowlist is not None, (
            "could not find _CONFIG_ALLOWLIST in api/main.py -- the check cannot run"
        )
        present = [k for k in _DEAD_KEYS if k in allowlist.get("acmg_thresholds", {})]
        assert not present, (
            f"api/main.py still allowlists {present} for GET /config -- the endpoint "
            "would advertise a threshold that governs nothing"
        )

    #: `analyze` has two REQUIRED arguments. Supplying them is what makes an
    #: argparse rejection attributable to the flag under test and nothing else.
    _REQUIRED = ["analyze", "--r1", "reads_R1.fastq.gz", "--ref", "ref.fa"]

    def test_the_analyze_spliceai_flag_is_gone(self):
        """`analyze --spliceai` wrote both keys. It must no longer parse.

        *** THE FIRST DRAFT OF THIS TEST PASSED AT MASTER, WHERE THE FLAG WAS
        STILL PRESENT. *** It treated ANY argparse exit as "the flag is gone",
        and the exit came from the missing required --r1/--ref. So it now
        supplies them, and it requires the rejection to NAME --spliceai.
        """
        import contextlib
        import io

        import main as kim_main

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            try:
                kim_main._build_parser().parse_args(self._REQUIRED + ["--spliceai", "0.5"])
            except SystemExit:
                pass
            else:
                raise AssertionError(
                    "`analyze --spliceai` still parses -- it writes pp3_spliceai/bp4_spliceai, "
                    "two thresholds nothing reads"
                )
        assert "--spliceai" in stderr.getvalue(), (
            "argparse exited, but NOT because of --spliceai -- this check cannot tell "
            "the flag's absence from some other error: " + stderr.getvalue()
        )

    def test_the_other_analyze_threshold_flags_still_parse(self):
        """The control for the test above: removing --spliceai must not have
        taken --cadd or --revel with it, which are LIVE thresholds. It is also
        what proves `_REQUIRED` is enough for a clean parse."""
        import main as kim_main

        args = kim_main._build_parser().parse_args(
            self._REQUIRED + ["--cadd", "22", "--revel", "0.6"]
        )
        assert args.cadd == 22.0 and args.revel == 0.6


# ─────────────────────────────────────────────────────────────────────────────
# GREEN BEFORE AND AFTER -- THE PROOF THAT THE REMOVAL MOVES NO VERDICT
# ─────────────────────────────────────────────────────────────────────────────

#: Variants chosen so that PP3 and BP4 are each decided by a narrow margin.
#: If either SpliceAI threshold were still read anywhere, setting it to an
#: EXTREME value would be the likeliest way to move one of these calls.
_BORDERLINE = [
    dict(is_missense=True, cadd_phred=25.0, revel_score=0.30, alphamissense_score=0.60),
    dict(is_missense=True, cadd_phred=21.0, revel_score=0.55, alphamissense_score=0.40),
    dict(is_missense=False, cadd_phred=25.0),
    dict(is_missense=True, cadd_phred=8.0, revel_score=0.10, alphamissense_score=0.10),
    dict(is_missense=False, cadd_phred=5.0),
]


def _criteria(thresholds: dict, fields: dict) -> set:
    evidence = VariantEvidence(chrom="17", pos=43057051, ref="A", alt="T", gene="BRCA1", **fields)
    return set(AcmgClassifier(cfg={"acmg_thresholds": thresholds}).classify(evidence).criteria_met)


class TestTheKeysNeverGovernedAVerdict:
    """*** This must pass at master, BEFORE the removal. *** That is what makes
    the commit's claim -- "they were dead" -- a measurement rather than an
    assertion."""

    def test_extreme_spliceai_thresholds_change_no_criterion(self):
        base = {k: v for k, v in _default_thresholds().items() if k not in _DEAD_KEYS}
        for fields in _BORDERLINE:
            without = _criteria(base, fields)
            for extreme in (0.0, 1.0):
                with_keys = _criteria(
                    {**base, "pp3_spliceai": extreme, "bp4_spliceai": extreme}, fields
                )
                assert with_keys == without, (
                    f"setting the SpliceAI thresholds to {extreme} changed the criteria met "
                    f"for {fields}: {sorted(without)} -> {sorted(with_keys)}. THE KEYS ARE "
                    "LIVE, and removing them is a clinical change, not a cleanup."
                )

    def test_the_borderline_fixtures_really_are_borderline(self):
        """Without this, the test above could pass on fixtures where nothing
        could ever move. PP3 must be met for at least one fixture and not met
        for another, and likewise BP4 -- so a live threshold WOULD have room
        to flip something."""
        base = {k: v for k, v in _default_thresholds().items() if k not in _DEAD_KEYS}
        results = [_criteria(base, f) for f in _BORDERLINE]
        assert any("PP3" in r for r in results) and any("PP3" not in r for r in results), (
            f"PP3 did not vary across the fixtures: {results}"
        )
        assert any("BP4" in r for r in results) and any("BP4" not in r for r in results), (
            f"BP4 did not vary across the fixtures: {results}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# GREEN BEFORE AND AFTER -- LEGITIMATE NON-SpliceAI SPLICE EVIDENCE SURVIVES
# ─────────────────────────────────────────────────────────────────────────────


def _evidence_through_the_real_batch(consequence: str) -> VariantEvidence:
    """Drive `run_acmg_evidence_batch` offline and capture the VariantEvidence it
    builds. Uses the REAL consequence -> is_lof mapping in
    orchestration/shared.py, not a hand-set `is_lof` -- setting the flag by hand
    would test the classifier and skip the very mapping that could be damaged."""
    captured: dict = {}
    real_classify = AcmgClassifier.classify

    def _spy(self, evidence):
        captured["evidence"] = evidence
        return real_classify(self, evidence)

    variant = {
        "chrom": "17",
        "pos": 43057051,
        "ref": "A",
        "alt": "T",
        "gene_name": "BRCA1",
        "consequence": consequence,
    }
    cfg = {"gnomad": {"enabled": False}, "clinvar": {"enabled": False}, "vep": {"enabled": False}}
    with patch.object(AcmgClassifier, "classify", _spy):
        run_acmg_evidence_batch([variant], cfg, sample_id="TESTSAMPLE")
    return captured["evidence"]


class TestLegitimateSpliceEvidenceStillCounts:
    """*** THE CONTROL THE RULING ASKED FOR: otherwise the fix can pass by
    deleting splice evidence wholesale. *** A canonical splice-site variant is
    splice evidence that owes nothing to SpliceAI."""

    def test_splice_donor_is_still_loss_of_function(self):
        assert _evidence_through_the_real_batch("splice_donor_variant").is_lof is True

    def test_splice_acceptor_is_still_loss_of_function(self):
        assert _evidence_through_the_real_batch("splice_acceptor_variant").is_lof is True

    def test_the_control_is_not_vacuous(self):
        """A synonymous variant must NOT come out as LoF -- otherwise the two
        checks above would pass on a mapping that marks everything LoF."""
        assert _evidence_through_the_real_batch("synonymous_variant").is_lof is False

    def test_a_canonical_splice_variant_still_reaches_pvs1(self):
        """The outcome, not just the flag. `lof_gene_intolerant` is supplied
        here because it comes from a constraint lookup that is disabled
        offline -- it is not the thing under test; the splice-derived `is_lof`
        is."""
        evidence = _evidence_through_the_real_batch("splice_donor_variant")
        evidence.lof_gene_intolerant = True
        assert "PVS1" in AcmgClassifier(cfg={}).classify(evidence).criteria_met
