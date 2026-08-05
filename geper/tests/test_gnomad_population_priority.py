"""
Tests for `CONFIG.gnomad.POPULATION_PRIORITY` support in
`pipeline/acmg_rules.py::ACMGRuleEngine._pm2`/`_ba1_bs1` (India-
deployment feature: prefer a configured population's own gnomAD AF --
e.g. "sas" for South Asian -- over global/popmax AF, with an honest
fallback + disclosure when that population's data is unavailable for
a given variant).

Exercises `ACMGRuleEngine` directly (unlike `tests/test_gnomad_acmg.py`,
which tests the separate, older `InterpretationEngine._gnomad_acmg_evidence`
aggregator) -- `CONFIG` is patched at `pipeline.acmg_rules.CONFIG`, the
same target module every `_pm2`/`_ba1_bs1` call reads `CONFIG.gnomad`
from.
"""

import unittest
from unittest import mock

from pipeline.acmg_rules import ACMGRuleEngine


class _FakeGnomadConfig:
    BA1_AF_THRESHOLD = 0.05
    BS1_AF_THRESHOLD = 0.01
    PM2_AF_THRESHOLD = 0.0001
    USE_POPMAX_FOR_BA1_BS1 = True
    POPULATION_PRIORITY = ""


def _patch_config(**overrides):
    cfg = _FakeGnomadConfig()
    for key, value in overrides.items():
        setattr(cfg, key, value)
    patcher = mock.patch("pipeline.acmg_rules.CONFIG")
    fake_config = patcher.start()
    fake_config.gnomad = cfg
    return patcher


class TestPopulationPriorityDisabledByDefault(unittest.TestCase):
    """POPULATION_PRIORITY="" (the default) must reproduce the exact pre-existing global/popmax-only behavior."""

    def setUp(self):
        self.addCleanup(mock.patch.stopall)

    def test_pm2_unaffected_when_no_priority_configured(self):
        _patch_config()
        result = ACMGRuleEngine._pm2(
            {"skipped": False, "found": True, "global_af": 0.00001, "population_breakdown": {"sas": {"af": 0.5}}}
        )
        self.assertEqual(result.status, "triggered")
        self.assertNotIn("sas", result.rationale.lower())
        self.assertNotIn("South Asian", result.rationale)

    def test_ba1_bs1_unaffected_when_no_priority_configured(self):
        _patch_config()
        ba1, bs1 = ACMGRuleEngine._ba1_bs1(
            {"skipped": False, "found": True, "global_af": 0.2, "population_breakdown": {"sas": {"af": 0.9}}}
        )
        self.assertEqual(ba1.status, "triggered")
        self.assertNotIn("South Asian", ba1.rationale)


class TestPm2PopulationPriority(unittest.TestCase):
    def setUp(self):
        self.addCleanup(mock.patch.stopall)

    def test_priority_af_rare_triggers_with_population_named(self):
        _patch_config(POPULATION_PRIORITY="sas")
        result = ACMGRuleEngine._pm2(
            {"skipped": False, "found": True, "global_af": 0.2, "population_breakdown": {"sas": {"af": 0.00001}}}
        )
        self.assertEqual(result.status, "triggered")
        self.assertIn("AF_sas", result.rationale)
        self.assertIn("South Asian", result.rationale)

    def test_priority_af_common_does_not_trigger_even_if_global_looked_rare(self):
        # The key scenario: global AF alone would have called this rare
        # (PM2-eligible), but the prioritized population shows it's
        # actually common -- PM2 must NOT trigger, and the rationale
        # must explain why in terms of the prioritized population.
        _patch_config(POPULATION_PRIORITY="sas")
        result = ACMGRuleEngine._pm2(
            {"skipped": False, "found": True, "global_af": 0.00001, "population_breakdown": {"sas": {"af": 0.05}}}
        )
        self.assertEqual(result.status, "not_triggered")
        self.assertIn("AF_sas", result.rationale)
        self.assertIn("not rare enough for PM2", result.rationale)
        self.assertIn("global AF = 1.00e-05", result.rationale)

    def test_priority_configured_but_unavailable_falls_back_with_disclosure(self):
        _patch_config(POPULATION_PRIORITY="sas")
        result = ACMGRuleEngine._pm2(
            {"skipped": False, "found": True, "global_af": 0.00001, "population_breakdown": {}}
        )
        self.assertEqual(result.status, "triggered")  # falls back to global AF, which is rare
        self.assertIn("global allele frequency", result.rationale)
        self.assertIn("population-priority 'sas'", result.rationale)
        self.assertIn("not available", result.rationale)
        self.assertIn("falling back to gnomAD global", result.rationale)

    def test_absent_from_gnomad_unaffected_by_priority(self):
        _patch_config(POPULATION_PRIORITY="sas")
        result = ACMGRuleEngine._pm2({"skipped": False, "found": False})
        self.assertEqual(result.status, "triggered")
        self.assertIn("absent from gnomAD", result.rationale)


class TestBa1Bs1PopulationPriority(unittest.TestCase):
    def setUp(self):
        self.addCleanup(mock.patch.stopall)

    def test_priority_af_common_triggers_ba1_with_contrast_note(self):
        _patch_config(POPULATION_PRIORITY="sas")
        ba1, bs1 = ACMGRuleEngine._ba1_bs1(
            {"skipped": False, "found": True, "global_af": 0.001, "population_breakdown": {"sas": {"af": 0.15}}}
        )
        self.assertEqual(ba1.status, "triggered")
        self.assertIn("AF_sas", ba1.rationale)
        self.assertIn("Common in the South Asian population", ba1.rationale)
        self.assertIn("though rare in the global population", ba1.rationale)
        self.assertEqual(bs1.status, "not_triggered")  # superseded by BA1

    def test_priority_af_common_triggers_bs1_with_contrast_note(self):
        # Between BS1 and BA1 thresholds -- BS1 (not BA1) should fire,
        # still carrying the same contrast disclosure.
        _patch_config(POPULATION_PRIORITY="sas")
        ba1, bs1 = ACMGRuleEngine._ba1_bs1(
            {"skipped": False, "found": True, "global_af": 0.0005, "population_breakdown": {"sas": {"af": 0.02}}}
        )
        self.assertEqual(ba1.status, "not_triggered")
        self.assertEqual(bs1.status, "triggered")
        self.assertIn("Common in the South Asian population", bs1.rationale)

    def test_no_contrast_note_when_global_already_agrees(self):
        # Priority AF triggers, but global AF was already high too --
        # no "though rare in the global population" framing should be
        # added, since that would misrepresent the global figure.
        _patch_config(POPULATION_PRIORITY="sas")
        ba1, _ = ACMGRuleEngine._ba1_bs1(
            {"skipped": False, "found": True, "global_af": 0.2, "population_breakdown": {"sas": {"af": 0.25}}}
        )
        self.assertEqual(ba1.status, "triggered")
        self.assertNotIn("though rare in the global population", ba1.rationale)

    def test_priority_configured_but_unavailable_falls_back_to_popmax_with_disclosure(self):
        _patch_config(POPULATION_PRIORITY="sas", USE_POPMAX_FOR_BA1_BS1=True)
        ba1, bs1 = ACMGRuleEngine._ba1_bs1(
            {
                "skipped": False,
                "found": True,
                "global_af": 0.001,
                "population_breakdown": {"fin": {"af": 0.2}},  # no "sas" entry at all
            }
        )
        self.assertEqual(ba1.status, "triggered")  # popmax (fin=0.2) still used
        self.assertIn("population-priority 'sas'", ba1.rationale)
        self.assertIn("not available", ba1.rationale)
        self.assertIn("used gnomAD popmax AF instead", ba1.rationale)

    def test_priority_unavailable_and_popmax_disabled_falls_back_to_global_with_disclosure(self):
        _patch_config(POPULATION_PRIORITY="sas", USE_POPMAX_FOR_BA1_BS1=False)
        ba1, bs1 = ACMGRuleEngine._ba1_bs1(
            {"skipped": False, "found": True, "global_af": 0.2, "population_breakdown": {}}
        )
        self.assertEqual(ba1.status, "triggered")
        self.assertIn("used gnomAD global AF instead", ba1.rationale)

    def test_not_found_unaffected_by_priority(self):
        _patch_config(POPULATION_PRIORITY="sas")
        ba1, bs1 = ACMGRuleEngine._ba1_bs1({"skipped": False, "found": False})
        self.assertEqual(ba1.status, "not_evaluated")
        self.assertEqual(bs1.status, "not_evaluated")


if __name__ == "__main__":
    unittest.main()
