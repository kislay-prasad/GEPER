"""
The splice-family delta thresholds are ONE shared pair, not per-model copies.

WHY THIS TEST EXISTS, AND WHY IT IS SHAPED THIS WAY.
The two numbers below (0.1 "no effect", 0.5 "moderate effect") were
duplicated as private literals in five files: spliceformer_plugin,
splicebert_plugin, ensemble, spip_plugin (moderate only) and the
standalone Kaggle verification script. Every copy agreed, and every
copy's own comment said it was deliberately the same scale as the
others'.

*** SIX AGREEING LITERALS ARE NOT A SHARED CONSTANT. THEY AGREE ONLY
UNTIL THE FIRST EDIT. *** The failure this guards against is not a
present-day mismatch -- there was none -- but a future one: someone
changes the value in `config.py`, believes it took effect everywhere,
and the sites that were never repointed silently keep the old number.
That failure would arrive with no error message and no mismatched
test, which is why it needs a test that fails LOUDLY the moment a
call site stops reading the shared source.

So these tests do not assert the VALUES (a test that only pinned 0.1
and 0.5 would pass just as happily on five unlinked copies). They
CHANGE the shared constant and assert every call site's bucketing
moves with it. A partial extraction fails here, which is the point.

SPiP IS DELIBERATELY DIFFERENT AND IS ASSERTED TO STAY THAT WAY: it
has no no-effect constant, because it reads "no effect" from SPiP's
own NTR interpretation rather than reapplying this family's generic
0.1 cutoff. `test_spip_keeps_its_own_no_effect_shape` exists so that a
tidy-minded reader who "completes the pair" gets a red test instead of
a silent behaviour change.
"""

import unittest
from unittest import mock

import config
from pipeline.models import ensemble, spip_plugin, splicebert_plugin, spliceformer_plugin

# The three plugins whose bucketing is the identical three-way split.
# spip_plugin is handled separately: its shape is deliberately not this one.
_SHARED_SHAPE_CLASSIFIERS = (
    ("spliceformer_plugin", lambda score: spliceformer_plugin._classify(score)),
    ("splicebert_plugin", lambda score: splicebert_plugin._classify(score)),
    ("ensemble", lambda score: ensemble._classify(score)),
)


class SharedSpliceDeltaThresholdsTest(unittest.TestCase):
    def test_config_exposes_the_shared_pair(self):
        self.assertEqual(config.SPLICE_DELTA_NO_EFFECT_THRESHOLD, 0.1)
        self.assertEqual(config.SPLICE_DELTA_MODERATE_EFFECT_THRESHOLD, 0.5)

    def test_no_repointed_module_keeps_a_private_literal(self):
        """
        The extraction is only real if the old private copies are GONE.
        A module that kept its literal would still pass every behavioural
        test below that it happened not to be wired into, so this is
        asserted directly rather than inferred.
        """
        for module in (spliceformer_plugin, splicebert_plugin, ensemble):
            for name in ("_NO_EFFECT_THRESHOLD", "_MODERATE_EFFECT_THRESHOLD"):
                self.assertFalse(
                    hasattr(module, name),
                    f"{module.__name__} still defines its own {name}; it must read "
                    f"the shared constant from config instead.",
                )
        self.assertFalse(
            hasattr(spip_plugin, "_MODERATE_EFFECT_THRESHOLD"),
            "spip_plugin still defines its own _MODERATE_EFFECT_THRESHOLD.",
        )

    def test_every_site_moves_with_the_shared_no_effect_threshold(self):
        """0.3 is 'moderate' by default; raising the no-effect cutoff past it
        must reclassify it at EVERY site, not just the ones we remembered."""
        for label, classify in _SHARED_SHAPE_CLASSIFIERS:
            with self.subTest(site=label):
                self.assertEqual(classify(0.3), "moderate_effect")

        with mock.patch.object(config, "SPLICE_DELTA_NO_EFFECT_THRESHOLD", 0.4):
            for label, classify in _SHARED_SHAPE_CLASSIFIERS:
                with self.subTest(site=label):
                    self.assertEqual(
                        classify(0.3),
                        "no_significant_effect",
                        f"{label} did not follow the shared no-effect threshold; "
                        f"it is probably still reading a private literal.",
                    )

    def test_every_site_moves_with_the_shared_moderate_threshold(self):
        """0.6 is 'large' by default; raising the moderate cutoff past it must
        reclassify it everywhere -- INCLUDING SPiP, which shares this one."""
        for label, classify in _SHARED_SHAPE_CLASSIFIERS:
            with self.subTest(site=label):
                self.assertEqual(classify(0.6), "large_effect")
        self.assertEqual(spip_plugin._classify("SPiP_effect", 0.6, 0.6), "large_effect")

        with mock.patch.object(config, "SPLICE_DELTA_MODERATE_EFFECT_THRESHOLD", 0.7):
            for label, classify in _SHARED_SHAPE_CLASSIFIERS:
                with self.subTest(site=label):
                    self.assertEqual(classify(0.6), "moderate_effect", f"{label} did not follow it")
            self.assertEqual(
                spip_plugin._classify("SPiP_effect", 0.6, 0.6),
                "moderate_effect",
                "spip_plugin did not follow the shared moderate threshold.",
            )

    def test_spip_keeps_its_own_no_effect_shape(self):
        """
        SPiP reads 'no effect' from its own NTR interpretation, NOT from this
        family's 0.1 cutoff. Raising the shared no-effect threshold must NOT
        change SPiP, and NTR must still win regardless of score.
        """
        self.assertEqual(spip_plugin._classify("NTR", 0.9, 0.9), "no_significant_effect")
        self.assertEqual(spip_plugin._classify("SPiP_effect", -1.0, 0.9), "no_significant_effect")
        # 0.3 sits above SPiP's (absent) no-effect cutoff and below moderate.
        self.assertEqual(spip_plugin._classify("SPiP_effect", 0.3, 0.3), "moderate_effect")
        with mock.patch.object(config, "SPLICE_DELTA_NO_EFFECT_THRESHOLD", 0.4):
            self.assertEqual(
                spip_plugin._classify("SPiP_effect", 0.3, 0.3),
                "moderate_effect",
                "SPiP must NOT gain a no-effect cutoff from the shared constant.",
            )


if __name__ == "__main__":
    unittest.main()
