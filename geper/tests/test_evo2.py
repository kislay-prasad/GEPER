"""
Unit tests for models/evo2.py.

These test Evo2Model's own logic (availability gating, pooling,
truncation, error translation) without requiring a real GPU, the real
`evo2` package, or downloading the 7B checkpoint -- the `evo2` package
and its wrapper object are mocked, mirroring the "pure function,
independently testable" approach used elsewhere in this project.
"""

import sys
import types
import unittest
from unittest import mock

import torch

from models.evo2 import Evo2Model
from utils.exceptions import ModelLoadError


class TestEvo2Availability(unittest.TestCase):
    def test_unavailable_without_gpu(self):
        # No practical CPU path exists for Evo 2 (see models/evo2.py
        # module docstring) -- is_available() must return False
        # immediately on a CPU-only environment, without even
        # attempting the (potentially slow) package install.
        with mock.patch("torch.cuda.is_available", return_value=False), \
             mock.patch("models.evo2.ensure_pip_package_available") as mock_ensure:
            self.assertFalse(Evo2Model.is_available())
            mock_ensure.assert_not_called()

    def test_available_when_gpu_and_package_present(self):
        with mock.patch("torch.cuda.is_available", return_value=True), \
             mock.patch("models.evo2.get_cuda_compute_capability", return_value=(8, 0)), \
             mock.patch("models.evo2.ensure_pip_package_available", return_value=True) as mock_ensure:
            self.assertTrue(Evo2Model.is_available())
            mock_ensure.assert_called_once_with("evo2")

    def test_unavailable_when_gpu_present_but_package_missing(self):
        with mock.patch("torch.cuda.is_available", return_value=True), \
             mock.patch("models.evo2.get_cuda_compute_capability", return_value=(8, 0)), \
             mock.patch("models.evo2.ensure_pip_package_available", return_value=False):
            self.assertFalse(Evo2Model.is_available())

    def test_unavailable_on_turing_t4_regardless_of_package(self):
        # Compute capability 7.5 == Turing (Tesla T4). FlashAttention-2's
        # official CUDA backend requires >= 8.0, so Evo 2 must be
        # reported unavailable *without ever calling
        # ensure_pip_package_available* -- checking the package is
        # pointless (and, on a real T4, exactly how the ungraceful
        # `No module named 'flash_attn_2_cuda'` failure used to
        # surface) once the hardware floor itself isn't met.
        with mock.patch("torch.cuda.is_available", return_value=True), \
             mock.patch("models.evo2.get_cuda_compute_capability", return_value=(7, 5)), \
             mock.patch("torch.cuda.get_device_name", return_value="Tesla T4"), \
             mock.patch("models.evo2.ensure_pip_package_available") as mock_ensure:
            self.assertFalse(Evo2Model.is_available())
            mock_ensure.assert_not_called()

    def test_available_on_hopper(self):
        with mock.patch("torch.cuda.is_available", return_value=True), \
             mock.patch("models.evo2.get_cuda_compute_capability", return_value=(9, 0)), \
             mock.patch("models.evo2.ensure_pip_package_available", return_value=True):
            self.assertTrue(Evo2Model.is_available())

    def test_unavailability_reason_names_the_gpu_and_capability_on_turing(self):
        with mock.patch("torch.cuda.is_available", return_value=True), \
             mock.patch("models.evo2.get_cuda_compute_capability", return_value=(7, 5)), \
             mock.patch("torch.cuda.get_device_name", return_value="Tesla T4"):
            reason = Evo2Model.unavailability_reason()
        self.assertIn("Tesla T4", reason)
        self.assertIn("7.5", reason)
        self.assertIn("8.0", reason)

    def test_unavailability_reason_when_no_gpu(self):
        with mock.patch("torch.cuda.is_available", return_value=False):
            reason = Evo2Model.unavailability_reason()
        self.assertIn("no CUDA GPU", reason)


class TestEvo2LoadImpl(unittest.TestCase):
    def test_load_impl_raises_without_gpu(self):
        model = Evo2Model.__new__(Evo2Model)  # bypass BaseGenomicModel.__init__ (no device probe needed)
        with mock.patch("torch.cuda.is_available", return_value=False):
            with self.assertRaises(ModelLoadError):
                model._load_impl()

    def test_load_impl_raises_when_package_install_fails(self):
        model = Evo2Model.__new__(Evo2Model)
        with mock.patch("torch.cuda.is_available", return_value=True), \
             mock.patch("models.evo2.get_cuda_compute_capability", return_value=(8, 0)), \
             mock.patch("models.evo2.ensure_pip_package_available", return_value=False):
            with self.assertRaises(ModelLoadError):
                model._load_impl()

    def test_load_impl_raises_on_turing_t4(self):
        # Same hardware floor as is_available(): forcing _load_impl()
        # directly (e.g. a caller that skipped is_available()) must
        # still fail loudly and clearly on a T4, rather than reaching
        # `from evo2 import Evo2` and surfacing a raw
        # `No module named 'flash_attn_2_cuda'` ImportError instead.
        model = Evo2Model.__new__(Evo2Model)
        with mock.patch("torch.cuda.is_available", return_value=True), \
             mock.patch("models.evo2.get_cuda_compute_capability", return_value=(7, 5)), \
             mock.patch("torch.cuda.get_device_name", return_value="Tesla T4"):
            with self.assertRaises(ModelLoadError) as ctx:
                model._load_impl()
        self.assertIn("Tesla T4", str(ctx.exception))

    def test_load_impl_constructs_evo2_with_configured_variant(self):
        model = Evo2Model.__new__(Evo2Model)
        model.device = torch.device("cuda")

        fake_evo2_instance = mock.MagicMock()
        fake_evo2_instance.tokenizer = mock.MagicMock()
        fake_evo2_cls = mock.MagicMock(return_value=fake_evo2_instance)
        fake_evo2_module = types.ModuleType("evo2")
        fake_evo2_module.Evo2 = fake_evo2_cls

        with mock.patch("torch.cuda.is_available", return_value=True), \
             mock.patch("models.evo2.get_cuda_compute_capability", return_value=(8, 0)), \
             mock.patch("models.evo2.ensure_pip_package_available", return_value=True), \
             mock.patch.dict(sys.modules, {"evo2": fake_evo2_module}):
            model._load_impl()

        fake_evo2_cls.assert_called_once()
        self.assertIs(model._evo2, fake_evo2_instance)
        self.assertIs(model.model, fake_evo2_instance)
        self.assertIs(model.tokenizer, fake_evo2_instance.tokenizer)
        self.assertEqual(model._dtype, torch.bfloat16)

    def test_verify_materialized_is_a_no_op(self):
        # The Evo2 wrapper is not an nn.Module (no named_parameters/
        # named_buffers), so the base class's generic meta-tensor scan
        # must not be applied to it.
        model = Evo2Model.__new__(Evo2Model)
        model.model = mock.MagicMock()  # would raise if scanned generically
        self.assertIsNone(model._verify_materialized())


class TestEvo2Infer(unittest.TestCase):
    def _make_model_with_fake_evo2(self, layer_name="blocks.28.mlp.l3", embed_dim=4):
        model = Evo2Model.__new__(Evo2Model)
        model.device = torch.device("cpu")
        model.logger = mock.MagicMock()
        model.tokenizer = mock.MagicMock()
        model.tokenizer.tokenize.side_effect = lambda seq: [1] * len(seq)
        model._dtype = torch.bfloat16

        embedding_tensor = torch.arange(1 * 3 * embed_dim, dtype=torch.float32).reshape(1, 3, embed_dim)

        def fake_forward(input_ids, return_embeddings=False, layer_names=None):
            self.assertTrue(return_embeddings)
            self.assertEqual(layer_names, [layer_name])
            return (None, {layer_name: embedding_tensor})

        model._evo2 = mock.MagicMock(side_effect=fake_forward)
        return model, embedding_tensor

    def test_infer_impl_pools_embeddings(self):
        model, embedding_tensor = self._make_model_with_fake_evo2()
        result = model._infer_impl("ACG")

        expected = embedding_tensor.mean(dim=1).squeeze(0).tolist()
        self.assertEqual(result["embedding_mean"], expected)
        self.assertEqual(result["embedding_dim"], embedding_tensor.shape[-1])
        self.assertFalse(result["truncated"])
        self.assertEqual(result["embedding_layer"], "blocks.28.mlp.l3")
        self.assertEqual(result["dtype"], str(torch.bfloat16))

    def test_infer_impl_truncates_oversized_sequence(self):
        model, _ = self._make_model_with_fake_evo2()
        from config import CONFIG

        oversized_sequence = "A" * (CONFIG.models.EVO2_MAX_SAFE_TOKENS + 500)
        result = model._infer_impl(oversized_sequence)

        self.assertTrue(result["truncated"])
        model.logger.warning.assert_called_once()


if __name__ == "__main__":
    unittest.main()
