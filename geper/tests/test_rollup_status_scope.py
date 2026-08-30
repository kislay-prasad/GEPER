"""
MEDIUM-1 (ruled): a run-level model status field must state its own
scope. `pipeline.models.status.rollup_run_status` collapses every
variant's per-variant `build_ai_model_status()` output into one
run-level entry per model, using `_ROLLUP_PRIORITY` (USED beats
FAILED beats DISABLED beats SKIPPED) -- untouched by this fix, and not
what this file tests. What it copied verbatim, before this fix, was
the *reason string* of whichever single per-variant entry won that
priority contest -- and every such string is written in the singular
("Ran for THIS variant..."). At one variant that referent is
invisible; at more, it has no antecedent, or is outright false for
some of them.

Kelly's five-variant run is the concrete case this reproduces: MMSplice
ran (used) for 2 of 5 variants and was skipped for the other 3, yet the
run-level `model_checkpoints["mmsplice"]["reason"]` read "Ran for this
variant and produced a splicing prediction." -- a sentence with no
valid referent describing a run of five.
"""

import unittest

from pipeline.models.status import rollup_run_status


class TestRunLevelReasonStatesItsScope(unittest.TestCase):
    def test_mmsplice_used_reason_names_how_many_of_the_run_it_covers(self):
        """
        Reproduces Kelly's exact five-variant MMSplice sequence
        (used, skipped, used, skipped, skipped -- 2 of 5 used).

        Before this fix: `rollup_run_status` returned the winning
        (used) entry's reason completely unchanged --
        "Ran for this variant and produced a splicing prediction." --
        identical to what a *one*-variant run where MMSplice ran would
        also produce, so a reader cannot tell the two runs apart from
        this field alone. Confirmed against the unmodified module by a
        direct probe before any edit was made:

            STATUS: used
            REASON: 'Ran for this variant and produced a splicing prediction.'

        This assertion fails on that text for the right reason: it
        does not merely check the reason changed, it checks the
        specific per-variant referent ("this variant") is gone and a
        run-level count ("2 of 5") -- naming exactly how many of the
        run's variants MMSplice actually ran for -- is present instead.
        """
        per_variant = [
            {"mmsplice": {"status": "used", "reason": "Ran for this variant and produced a splicing prediction."}},
            {"mmsplice": {"status": "skipped", "reason": "Variant is outside MMSplice's supported splice window."}},
            {"mmsplice": {"status": "used", "reason": "Ran for this variant and produced a splicing prediction."}},
            {"mmsplice": {"status": "skipped", "reason": "Variant is outside MMSplice's supported splice window."}},
            {"mmsplice": {"status": "skipped", "reason": "Variant is outside MMSplice's supported splice window."}},
        ]

        result = rollup_run_status(per_variant)

        self.assertEqual(result["mmsplice"]["status"], "used")  # rollup priority itself: untouched, out of scope
        reason = result["mmsplice"]["reason"]
        self.assertNotIn(
            "this variant",
            reason,
            f"run-level reason still carries per-variant wording with no valid referent: {reason!r}",
        )
        self.assertIn(
            "2 of 5",
            reason,
            f"run-level reason does not state how many of the run's 5 variants it covers: {reason!r}",
        )
        self.assertEqual(reason, "Ran for 2 of 5 variants and produced a splicing prediction.")

    def test_single_variant_run_still_states_scope_without_odd_pluralization(self):
        """A one-variant run is the degenerate case where the old per-variant
        wording was merely misleading-by-omission rather than false; the
        fixed text must still name the scope ("1 of 1 variant"), not read
        as if pluralized ("1 of 1 variants")."""
        per_variant = [
            {"esm2": {"status": "used", "reason": "Ran for this variant's translated protein context."}},
        ]
        result = rollup_run_status(per_variant)
        self.assertEqual(result["esm2"]["reason"], "Ran for 1 of 1 variant's translated protein context.")

    def test_reason_with_no_per_variant_referent_still_gets_a_scope_count_appended(self):
        """An environment-level DISABLED reason never claimed anything
        about "this variant" to begin with, but the field it rides on is
        still run-level -- per the ruling, EVERY model's run-level status
        must state its scope, not only the ones whose old text happened to
        say "this variant"."""
        per_variant = [
            {
                "evo2": {
                    "status": "disabled",
                    "reason": "Not available in this environment (missing optional dependency, or -- for Evo2 -- unsupported hardware).",
                }
            }
            for _ in range(5)
        ]
        result = rollup_run_status(per_variant)
        reason = result["evo2"]["reason"]
        self.assertIn("5 of 5 variants", reason)
        self.assertTrue(reason.startswith("Not available in this environment"))


if __name__ == "__main__":
    unittest.main()
