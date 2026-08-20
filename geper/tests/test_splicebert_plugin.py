"""
Unit tests for pipeline/models/splicebert_plugin.py.

Mirrors tests/test_spliceformer_plugin.py's own structure and mocking
boundary: the official checkpoint is a standard HuggingFace
BertForMaskedLM, but building/loading it for real requires
`transformers` and reaching Zenodo, which this test environment
cannot always do -- so every test here mocks at that exact boundary
(`splicebert_loader.download_and_extract_checkpoint`/
`build_model_and_tokenizer`, or `self.model`/`self.tokenizer`
directly), never the surrounding GEPER logic. This means the plugin's
own code (sequence preparation, differing-position selection, masked-
marginal scoring, delta/classification computation) runs for real and
is what's actually under test.
"""

import unittest
from unittest import mock

import torch

from pipeline.models.manager import ModelManager, PluginUnavailableError
from pipeline.models.registry import ModelRegistry
from pipeline.models.splicebert_plugin import (
    SPLICEBERT_MAX_CONTENT_LENGTH,
    SpliceBERTPlugin,
)
from utils.exceptions import ModelLoadError

_VOCAB = {"[PAD]": 0, "[UNK]": 1, "[CLS]": 2, "[SEP]": 3, "[MASK]": 4, "N": 5, "A": 6, "C": 7, "G": 8, "T": 9}


class _FakeTokenizer:
    mask_token_id = _VOCAB["[MASK]"]

    def encode(self, text, return_tensors=None):
        chars = text.split(" ")
        ids = [_VOCAB["[CLS]"]] + [_VOCAB.get(c, _VOCAB["[UNK]"]) for c in chars] + [_VOCAB["[SEP]"]]
        if return_tensors == "pt":
            return torch.tensor([ids])
        return ids

    @staticmethod
    def convert_tokens_to_ids(token):
        return _VOCAB.get(token, _VOCAB["[UNK]"])


class _FakeOutput:
    def __init__(self, logits):
        self.logits = logits


class _FakeSpliceBERTModel:
    """Returns caller-controlled logits per masked row so tests can
    pin an exact softmax outcome, mirroring how
    tests/test_spliceformer_plugin.py's `_FakeSpliceFormerModel`
    returns caller-controlled fill values -- SpliceBERT's plugin
    applies softmax internally (SpliceFormer's does not), so this
    fake returns raw logits rather than already-final probabilities,
    but the "caller fully controls the outcome" shape is identical.
    `row_logits[i]` is a dict {token_id: logit}; unset ids default to
    0.0. `vocab_size` matches the real checkpoint's vocab.txt (10)."""

    def __init__(self, row_logits, vocab_size=10, expected_device="cpu"):
        self.row_logits = row_logits
        self.vocab_size = vocab_size
        self.expected_device = expected_device
        self.calls = 0
        self.to_calls = []
        self.eval_called = False

    def to(self, device):
        self.to_calls.append(device)
        return self

    def eval(self):
        self.eval_called = True
        return self

    def __call__(self, input_ids=None):
        self.calls += 1
        assert isinstance(input_ids, torch.Tensor)
        assert str(input_ids.device) == self.expected_device
        batch, seq_len = input_ids.shape
        logits = torch.zeros(batch, seq_len, self.vocab_size)
        for row in range(batch):
            for token_id, value in self.row_logits[row].items():
                logits[row, :, token_id] = value
        return _FakeOutput(logits)


class TestPrepareSequence(unittest.TestCase):
    def test_short_sequence_is_centered_and_padded_with_n(self):
        prepared = SpliceBERTPlugin._prepare_sequence("ACGT", target_length=10)
        self.assertEqual(len(prepared), 10)
        self.assertEqual(prepared, "NNNACGTNNN")

    def test_exact_length_sequence_is_unchanged(self):
        seq = "A" * 20
        self.assertEqual(SpliceBERTPlugin._prepare_sequence(seq, target_length=20), seq)

    def test_long_sequence_is_truncated_symmetrically(self):
        seq = "N" * 5 + "ACGTACGTAC" + "N" * 5
        prepared = SpliceBERTPlugin._prepare_sequence(seq, target_length=10)
        self.assertEqual(len(prepared), 10)
        self.assertEqual(prepared, "ACGTACGTAC")

    def test_default_target_length_matches_official_configuration(self):
        prepared = SpliceBERTPlugin._prepare_sequence("A")
        self.assertEqual(len(prepared), SPLICEBERT_MAX_CONTENT_LENGTH)
        self.assertEqual(SPLICEBERT_MAX_CONTENT_LENGTH, 1024)

    def test_lowercase_input_is_uppercased(self):
        prepared = SpliceBERTPlugin._prepare_sequence("acgt", target_length=4)
        self.assertEqual(prepared, "ACGT")

    def test_u_is_replaced_with_t(self):
        prepared = SpliceBERTPlugin._prepare_sequence("acgu", target_length=4)
        self.assertEqual(prepared, "ACGT")


class TestDifferingPositions(unittest.TestCase):
    def _instance(self):
        instance = SpliceBERTPlugin.__new__(SpliceBERTPlugin)
        instance.logger = mock.Mock()
        instance.device = "cpu"
        return instance

    def test_identical_sequences_yield_no_positions(self):
        instance = self._instance()
        self.assertEqual(instance._differing_positions("ACGT", "ACGT"), [])

    def test_single_snv_returns_its_position(self):
        instance = self._instance()
        self.assertEqual(instance._differing_positions("ACGT", "ACGA"), [3])

    def test_capped_and_closest_to_center_first(self):
        instance = self._instance()
        ref = "A" * 100
        # differ at every position -- all 100 differ, must cap at 32
        alt = "T" * 100
        positions = instance._differing_positions(ref, alt)
        self.assertEqual(len(positions), 32)
        center = 50
        # first returned position must be the closest one to center
        self.assertEqual(min(positions, key=lambda i: abs(i - center)), positions[0])


class TestSpliceBERTInference(unittest.TestCase):
    def _make_instance(self, fake_model, fake_tokenizer=None):
        instance = SpliceBERTPlugin.__new__(SpliceBERTPlugin)
        instance.logger = mock.Mock()
        instance.device = "cpu"
        instance.model = fake_model
        instance.tokenizer = fake_tokenizer or _FakeTokenizer()
        return instance

    def test_identical_ref_and_alt_yields_no_significant_effect_without_calling_model(self):
        fake_model = _FakeSpliceBERTModel(row_logits=[])
        instance = self._make_instance(fake_model)
        result = instance._infer_impl("A" * 10, "A" * 10)
        self.assertEqual(result["score"], 0.0)
        self.assertEqual(result["classification"], "no_significant_effect")
        self.assertEqual(result["confidence"], 0.0)
        self.assertEqual(fake_model.calls, 0)

    def test_large_delta_yields_large_effect_classification(self):
        # ref="A"*10, alt="T"*10, single scored position (last, index 9,
        # closest to center for length 10) -- ref token id 6 ('A') gets
        # a large positive logit, alt token id 9 ('T') a large negative
        # one, so softmax gives P(ref)~=1, P(alt)~=0.
        fake_model = _FakeSpliceBERTModel(row_logits=[{6: 20.0, 9: -20.0}])
        instance = self._make_instance(fake_model)
        result = instance._infer_impl("A" * 10, "A" * 9 + "T")
        self.assertGreater(result["score"], 0.99)
        self.assertEqual(result["classification"], "large_effect")
        self.assertEqual(fake_model.calls, 1)

    def test_near_uniform_logits_yield_no_significant_effect(self):
        fake_model = _FakeSpliceBERTModel(row_logits=[{}])  # all-zero logits -> uniform softmax
        instance = self._make_instance(fake_model)
        result = instance._infer_impl("A" * 10, "A" * 9 + "T")
        self.assertLess(result["score"], 1e-6)
        self.assertEqual(result["classification"], "no_significant_effect")

    def test_score_matches_independently_computed_softmax_difference(self):
        # Verify the plugin's wiring (indexing into the right row/
        # position/token id) against an expectation computed directly
        # from the same logits, rather than hand-derived arithmetic.
        row_logit_overrides = {6: 2.0, 9: 0.5}
        fake_model = _FakeSpliceBERTModel(row_logits=[row_logit_overrides])
        instance = self._make_instance(fake_model)
        result = instance._infer_impl("A" * 10, "A" * 9 + "T")

        logits = torch.zeros(10)
        for token_id, value in row_logit_overrides.items():
            logits[token_id] = value
        probs = torch.softmax(logits, dim=-1)
        expected = (probs[6] - probs[9]).item()  # P(ref='A') - P(alt='T')
        self.assertAlmostEqual(result["details"]["prob_change_scores"][0], expected, places=5)
        self.assertAlmostEqual(result["score"], abs(expected), places=5)

    def test_details_report_expected_fields_and_calibration_status(self):
        fake_model = _FakeSpliceBERTModel(row_logits=[{6: 20.0, 9: -20.0}])
        instance = self._make_instance(fake_model)
        result = instance._infer_impl("A" * 10, "A" * 9 + "T")
        details = result["details"]
        self.assertIn("uncalibrated", details["calibration_status"])
        self.assertEqual(details["strand"], "+")
        self.assertEqual(details["num_scored_positions"], 1)
        self.assertEqual(details["num_total_differing_positions"], 1)
        self.assertIn("checkpoint", details)

    def test_predict_via_base_class_tags_meta_correctly(self):
        fake_model = _FakeSpliceBERTModel(row_logits=[{6: 20.0, 9: -20.0}])
        instance = self._make_instance(fake_model)
        instance._loaded = True
        result = instance.predict("A" * 10, "A" * 9 + "T")
        self.assertEqual(result["meta"]["model"], "splicebert")
        self.assertIn("device", result["meta"])

    def test_minus_strand_kwarg_is_forwarded_and_recorded(self):
        fake_model = _FakeSpliceBERTModel(row_logits=[{6: 20.0, 9: -20.0}])
        instance = self._make_instance(fake_model)
        result = instance._infer_impl("A" * 10, "A" * 9 + "T", strand="-")
        self.assertEqual(result["details"]["strand"], "-")


class TestSpliceBERTLoadImpl(unittest.TestCase):
    def setUp(self):
        self.instance = SpliceBERTPlugin.__new__(SpliceBERTPlugin)
        self.instance.logger = mock.Mock()
        self.instance.device = "cpu"
        self.instance._loaded = False
        self.instance.model = None
        self.instance.tokenizer = None
        self.instance._weight_cache = mock.Mock()
        self.instance._weight_cache.ensure_dir.return_value = "fake_cache_dir"

    def test_successful_load_downloads_and_builds_model(self):
        fake_model = _FakeSpliceBERTModel(row_logits=[])
        fake_tokenizer = _FakeTokenizer()
        with (
            mock.patch("pipeline.models.splicebert_plugin.is_pip_package_installed", return_value=True),
            mock.patch("pipeline.models.splicebert_plugin.splicebert_loader.is_checkpoint_cached", return_value=False),
            mock.patch(
                "pipeline.models.splicebert_plugin.splicebert_loader.download_and_extract_checkpoint"
            ) as mock_download,
            mock.patch(
                "pipeline.models.splicebert_plugin.splicebert_loader.checkpoint_dir_for", return_value="ckpt_dir"
            ),
            mock.patch(
                "pipeline.models.splicebert_plugin.splicebert_loader.build_model_and_tokenizer",
                return_value=(fake_model, fake_tokenizer),
            ) as mock_build,
        ):
            self.instance._load_impl()

        mock_download.assert_called_once()
        mock_build.assert_called_once_with("ckpt_dir")
        self.assertIs(self.instance.model, fake_model)
        self.assertIs(self.instance.tokenizer, fake_tokenizer)
        self.assertEqual(fake_model.to_calls, ["cpu"])
        self.assertTrue(fake_model.eval_called)

    def test_skips_download_when_checkpoint_already_cached(self):
        fake_model = _FakeSpliceBERTModel(row_logits=[])
        fake_tokenizer = _FakeTokenizer()
        with (
            mock.patch("pipeline.models.splicebert_plugin.is_pip_package_installed", return_value=True),
            mock.patch("pipeline.models.splicebert_plugin.splicebert_loader.is_checkpoint_cached", return_value=True),
            mock.patch(
                "pipeline.models.splicebert_plugin.splicebert_loader.download_and_extract_checkpoint"
            ) as mock_download,
            mock.patch(
                "pipeline.models.splicebert_plugin.splicebert_loader.checkpoint_dir_for", return_value="ckpt_dir"
            ),
            mock.patch(
                "pipeline.models.splicebert_plugin.splicebert_loader.build_model_and_tokenizer",
                return_value=(fake_model, fake_tokenizer),
            ),
        ):
            self.instance._load_impl()

        mock_download.assert_not_called()

    def test_network_failure_is_sanitized(self):
        with (
            mock.patch("pipeline.models.splicebert_plugin.is_pip_package_installed", return_value=True),
            mock.patch("pipeline.models.splicebert_plugin.splicebert_loader.is_checkpoint_cached", return_value=False),
            mock.patch(
                "pipeline.models.splicebert_plugin.splicebert_loader.download_and_extract_checkpoint",
                side_effect=ConnectionError("could not reach zenodo.org"),
            ),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                self.instance._load_impl()

        self.assertEqual(str(ctx.exception), "SpliceBERT model unavailable")
        self.assertNotIn("zenodo.org", str(ctx.exception))

    def test_load_timeout_message_is_sanitized_but_keeps_the_isolation_correction(self):
        # Round 21: the raised message must not repeat the checkpoint
        # directory path a real TimeoutError's own str() carries (round
        # 20's leak class, on the timeout branch this time -- confirmed
        # live: a real 2026-08-14 run's clinical PDF printed a full
        # '/content/geper_cache/...' path in its Data Source Provenance
        # section). It must still say the load succeeds in isolation and
        # stalls only inside the full pipeline process -- that's round
        # 18's correction of a disproven "TensorFlow-backend detection"
        # story, and silently dropping it back to a bare "unavailable"
        # would undo that correction for a real report reader.
        fake_path = "/content/geper_cache/plugin_model_cache/splicebert/SpliceBERT.1024nt"
        with (
            mock.patch("pipeline.models.splicebert_plugin.is_pip_package_installed", return_value=True),
            mock.patch("pipeline.models.splicebert_plugin.splicebert_loader.is_checkpoint_cached", return_value=True),
            mock.patch(
                "pipeline.models.splicebert_plugin.splicebert_loader.checkpoint_dir_for", return_value=fake_path
            ),
            mock.patch(
                "pipeline.models.splicebert_plugin.splicebert_loader.build_model_and_tokenizer",
                side_effect=TimeoutError(
                    f"Loading SpliceBERT checkpoint from '{fake_path}' did not complete within 180s"
                ),
            ),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                self.instance._load_impl()

        message = str(ctx.exception)
        self.assertNotIn(fake_path, message)
        self.assertNotIn("/content/", message)
        self.assertIn("succeeds quickly in isolation", message)
        self.assertIn("full pipeline process", message)

    def test_missing_transformers_raises_clear_error(self):
        with mock.patch("pipeline.models.splicebert_plugin.is_pip_package_installed", return_value=False):
            with self.assertRaises(RuntimeError) as ctx:
                self.instance._load_impl()
        self.assertIn("transformers", str(ctx.exception))

    def test_full_load_via_public_api_wraps_in_model_load_error_on_failure(self):
        with (
            mock.patch("pipeline.models.splicebert_plugin.is_pip_package_installed", return_value=True),
            mock.patch("pipeline.models.splicebert_plugin.splicebert_loader.is_checkpoint_cached", return_value=False),
            mock.patch(
                "pipeline.models.splicebert_plugin.splicebert_loader.download_and_extract_checkpoint",
                side_effect=OSError("network unreachable"),
            ),
        ):
            with self.assertRaises(ModelLoadError):
                self.instance.load()


class TestSpliceBERTMetadataAndAvailability(unittest.TestCase):
    def test_metadata_reports_commercial_use_allowed(self):
        meta = SpliceBERTPlugin.metadata()
        self.assertTrue(meta.commercial_use_allowed)
        self.assertIn("BSD-3-Clause", meta.license_name)
        self.assertIn("biomed-AI/SpliceBERT", meta.source)

    def test_disabled_by_default_flag_off(self):
        with mock.patch("pipeline.models.splicebert_plugin.CONFIG") as mock_config:
            mock_config.splicing.ENABLE_SPLICEBERT = False
            self.assertFalse(SpliceBERTPlugin.is_available())
            self.assertIn("ENABLE_SPLICEBERT", SpliceBERTPlugin.unavailability_reason())

    def test_available_when_flag_on_and_transformers_installed(self):
        with (
            mock.patch("pipeline.models.splicebert_plugin.CONFIG") as mock_config,
            mock.patch("pipeline.models.splicebert_plugin.is_pip_package_installed", return_value=True),
        ):
            mock_config.splicing.ENABLE_SPLICEBERT = True
            self.assertTrue(SpliceBERTPlugin.is_available())

    def test_unavailable_when_flag_on_but_transformers_missing(self):
        with (
            mock.patch("pipeline.models.splicebert_plugin.CONFIG") as mock_config,
            mock.patch("pipeline.models.splicebert_plugin.is_pip_package_installed", return_value=False),
        ):
            mock_config.splicing.ENABLE_SPLICEBERT = True
            self.assertFalse(SpliceBERTPlugin.is_available())
            self.assertIn("transformers", SpliceBERTPlugin.unavailability_reason())


class TestSpliceBERTThroughModelManager(unittest.TestCase):
    def test_predict_returns_none_when_disabled(self):
        registry = ModelRegistry()
        registry.register("splicebert", SpliceBERTPlugin)
        manager = ModelManager(registry=registry)
        with mock.patch("pipeline.models.splicebert_plugin.CONFIG") as mock_config:
            mock_config.splicing.ENABLE_SPLICEBERT = False
            result = manager.predict("splicebert", "A" * 10, "T" * 10)
        self.assertIsNone(result)

    def test_get_raises_plugin_unavailable_when_disabled(self):
        registry = ModelRegistry()
        registry.register("splicebert", SpliceBERTPlugin)
        manager = ModelManager(registry=registry)
        with mock.patch("pipeline.models.splicebert_plugin.CONFIG") as mock_config:
            mock_config.splicing.ENABLE_SPLICEBERT = False
            with self.assertRaises(PluginUnavailableError):
                manager.get("splicebert")

    def test_splicebert_disabled_by_default(self):
        """Regression test: SpliceBERT reliably fails to load on
        transformers 5.13.1 (2026-08-09 timeouts, see DATA_PROVENANCE.md),
        so ENABLE_SPLICEBERT must default to False -- a defect fix, not a
        policy call. Unlike the other tests in this file, this one does
        NOT mock CONFIG: it exercises the real, unmodified config default
        and the real object graph GEPER would use at startup
        (`pipeline.models.pending_plugins.build_default_registry()`), the
        same way `tests/test_new_plugins_integration.py`'s
        TestBothPluginsDisabledByDefault does for Enformer/Borzoi.
        """
        from config import CONFIG
        from pipeline.models.pending_plugins import build_default_registry

        self.assertFalse(CONFIG.splicing.ENABLE_SPLICEBERT)
        self.assertFalse(SpliceBERTPlugin.is_available())

        registry = build_default_registry()
        loaded_models = registry.available_keys()
        self.assertNotIn("splicebert", loaded_models)
        self.assertFalse(any("splicebert" in str(m).lower() for m in loaded_models))

        manager = ModelManager(registry=registry)
        self.assertIsNone(manager.predict("splicebert", "A" * 10, "T" * 10))


if __name__ == "__main__":
    unittest.main()
