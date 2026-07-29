"""
Unit tests for pipeline/models/spip_plugin.py.

Mirrors tests/test_spliceformer_plugin.py's / test_splicebert_plugin.py's
own structure and mocking boundary: SPiP is vendored, unmodified R
source (pipeline/models/spip/vendor/), but actually running it
requires R + its CRAN packages + ~400MB of reference data, which this
test environment cannot always assume -- so every test here mocks at
that exact boundary (`spip_loader.find_rscript`/`ensure_r_packages`/
`prepare_runtime_dir`/`run_spip`), never the surrounding GEPER logic.
This means the plugin's own code (output parsing, transcript-row
selection, score/classification derivation) runs for real and is what's
actually under test.
"""

import unittest
from unittest import mock
from pathlib import Path

from pipeline.models.manager import ModelManager, PluginUnavailableError
from pipeline.models.registry import ModelRegistry
from pipeline.models.spip_plugin import SpipPlugin, _parse_spip_output
from utils.exceptions import ModelLoadError, ModelInferenceError

_HEADER = (
    "CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tvarID\tInterpretation\t"
    "InterConfident\tSPiPscore\tstrand\tgNomen\tvarType\tntChange\tExonInfo\t"
    "exonSize\ttranscript\tgene\tNearestSS\tDistSS\tRegType\tSPiCEproba\t"
    "SPiCEinter_2thr\tdeltaMES\tBP\tmutInPBarea\tdeltaESRscore\tposCryptMut\t"
    "sstypeCryptMut\tprobaCryptMut\tclassProbaCryptMut\tnearestSStoCrypt\t"
    "nearestPosSStoCrypt\tnearestDistSStoCrypt\tposCryptWT\tprobaCryptWT\t"
    "classProbaCryptWT\tposSSPhysio\tprobaSSPhysio\tclassProbaSSPhysio\t"
    "probaSSPhysioMut\tclassProbaSSPhysioMut"
)


def _make_row(**overrides):
    values = {
        "CHROM": "chr17", "POS": "29527614", "ID": "c.1062+1G>A", "REF": "G", "ALT": "A",
        "QUAL": ".", "FILTER": ".", "INFO": ".",
        "varID": "chr17:29527614G>A", "Interpretation": "Alter by SPiCE", "InterConfident": "90%",
        "SPiPscore": "0.85", "strand": "+", "gNomen": "29527614", "varType": "substitution",
        "ntChange": "c.1062+1G>A", "ExonInfo": "Exon 9", "exonSize": "100", "transcript": "NM_000267",
        "gene": "NF1", "NearestSS": "Donor", "DistSS": "1", "RegType": "IntronCons",
        "SPiCEproba": "0.95", "SPiCEinter_2thr": "Alter by SPiCE", "deltaMES": "8.5", "BP": "",
        "mutInPBarea": "No", "deltaESRscore": "0.1", "posCryptMut": "", "sstypeCryptMut": "",
        "probaCryptMut": "", "classProbaCryptMut": "", "nearestSStoCrypt": "", "nearestPosSStoCrypt": "",
        "nearestDistSStoCrypt": "", "posCryptWT": "", "probaCryptWT": "", "classProbaCryptWT": "",
        "posSSPhysio": "", "probaSSPhysio": "", "classProbaSSPhysio": "", "probaSSPhysioMut": "",
        "classProbaSSPhysioMut": "",
    }
    values.update(overrides)
    return "\t".join(values[col] for col in _HEADER.split("\t"))


def _make_output(*rows):
    return "\n".join([_HEADER, *rows]) + "\n"


class TestParseSpipOutput(unittest.TestCase):
    def test_empty_output_returns_empty_list(self):
        self.assertEqual(_parse_spip_output(""), [])

    def test_header_only_returns_empty_list(self):
        self.assertEqual(_parse_spip_output(_HEADER + "\n"), [])

    def test_single_row_parsed_into_dict(self):
        rows = _parse_spip_output(_make_output(_make_row()))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["gene"], "NF1")
        self.assertEqual(rows[0]["SPiPscore"], "0.85")

    def test_multiple_rows_parsed(self):
        rows = _parse_spip_output(_make_output(_make_row(gene="NF1"), _make_row(gene="NF1-AS1")))
        self.assertEqual(len(rows), 2)
        self.assertEqual([r["gene"] for r in rows], ["NF1", "NF1-AS1"])


class TestSpipInference(unittest.TestCase):
    def _make_instance(self):
        instance = SpipPlugin.__new__(SpipPlugin)
        instance.logger = mock.Mock()
        instance.device = "cpu"
        instance._rscript_path = "Rscript"
        instance._runtime_dir = Path("fake_runtime_dir")
        instance._genome = "hg19"
        return instance

    def test_missing_coordinates_raises_value_error(self):
        instance = self._make_instance()
        with self.assertRaises(ValueError):
            instance._infer_impl("A" * 10, "T" * 10)

    def test_large_effect_when_score_above_threshold_and_not_ntr(self):
        instance = self._make_instance()
        with mock.patch(
            "pipeline.models.spip_plugin.spip_loader.run_spip",
            return_value=_make_output(_make_row(SPiPscore="0.85", Interpretation="Alter by SPiCE")),
        ):
            result = instance._infer_impl("A" * 10, "T" * 10, chrom="17", pos=29527614, ref="G", alt="A")
        self.assertAlmostEqual(result["score"], 0.85)
        self.assertEqual(result["classification"], "large_effect")

    def test_moderate_effect_when_score_below_threshold_and_not_ntr(self):
        instance = self._make_instance()
        with mock.patch(
            "pipeline.models.spip_plugin.spip_loader.run_spip",
            return_value=_make_output(_make_row(SPiPscore="0.3", Interpretation="Alter by complex event")),
        ):
            result = instance._infer_impl("A" * 10, "T" * 10, chrom="17", pos=29527614, ref="G", alt="A")
        self.assertEqual(result["classification"], "moderate_effect")

    def test_ntr_interpretation_yields_no_significant_effect_regardless_of_score(self):
        instance = self._make_instance()
        with mock.patch(
            "pipeline.models.spip_plugin.spip_loader.run_spip",
            return_value=_make_output(_make_row(SPiPscore="0.9", Interpretation="NTR")),
        ):
            result = instance._infer_impl("A" * 10, "T" * 10, chrom="17", pos=29527614, ref="G", alt="A")
        self.assertEqual(result["classification"], "no_significant_effect")

    def test_negative_sentinel_score_yields_no_significant_effect_and_clamps_to_zero(self):
        instance = self._make_instance()
        with mock.patch(
            "pipeline.models.spip_plugin.spip_loader.run_spip",
            return_value=_make_output(_make_row(SPiPscore="-1", Interpretation="Alter by SPiCE")),
        ):
            result = instance._infer_impl("A" * 10, "T" * 10, chrom="17", pos=29527614, ref="G", alt="A")
        self.assertEqual(result["score"], 0.0)
        self.assertEqual(result["classification"], "no_significant_effect")

    def test_no_output_rows_raises_runtime_error(self):
        instance = self._make_instance()
        with mock.patch(
            "pipeline.models.spip_plugin.spip_loader.run_spip", return_value=_HEADER + "\n"
        ):
            with self.assertRaises(RuntimeError):
                instance._infer_impl("A" * 10, "T" * 10, chrom="17", pos=29527614, ref="G", alt="A")

    def test_gene_kwarg_selects_matching_row_among_multiple_transcripts(self):
        instance = self._make_instance()
        rows_text = _make_output(
            _make_row(gene="GENE_A", SPiPscore="0.1", Interpretation="NTR"),
            _make_row(gene="GENE_B", SPiPscore="0.9", Interpretation="Alter by SPiCE"),
        )
        with mock.patch("pipeline.models.spip_plugin.spip_loader.run_spip", return_value=rows_text):
            result = instance._infer_impl(
                "A" * 10, "T" * 10, chrom="17", pos=29527614, ref="G", alt="A", gene="GENE_B"
            )
        self.assertEqual(result["details"]["gene"], "GENE_B")
        self.assertEqual(result["classification"], "large_effect")
        self.assertEqual(result["details"]["num_transcripts_matched"], 2)

    def test_details_contain_expected_fields(self):
        instance = self._make_instance()
        with mock.patch(
            "pipeline.models.spip_plugin.spip_loader.run_spip", return_value=_make_output(_make_row())
        ):
            result = instance._infer_impl("A" * 10, "T" * 10, chrom="17", pos=29527614, ref="G", alt="A")
        details = result["details"]
        for key in (
            "interpretation", "inter_confident", "spip_score", "region_type",
            "spice_proba", "delta_mes", "gene", "transcript", "calibration_status",
        ):
            self.assertIn(key, details)

    def test_predict_via_base_class_tags_meta_correctly(self):
        instance = self._make_instance()
        instance._loaded = True
        with mock.patch(
            "pipeline.models.spip_plugin.spip_loader.run_spip", return_value=_make_output(_make_row())
        ):
            result = instance.predict("A" * 10, "T" * 10, chrom="17", pos=29527614, ref="G", alt="A")
        self.assertEqual(result["meta"]["model"], "spip")

    def test_missing_coordinates_via_public_api_is_caught_as_inference_error(self):
        instance = self._make_instance()
        instance._loaded = True
        with self.assertRaises(ModelInferenceError):
            instance.predict("A" * 10, "T" * 10)


class TestSpipLoadImpl(unittest.TestCase):
    def setUp(self):
        self.instance = SpipPlugin.__new__(SpipPlugin)
        self.instance.logger = mock.Mock()
        self.instance.device = "cpu"
        self.instance._loaded = False
        self.instance.model = None
        self.instance._rscript_path = None
        self.instance._runtime_dir = None
        self.instance._genome = None
        self.instance._weight_cache = mock.Mock()
        self.instance._weight_cache.ensure_dir.return_value = Path("fake_cache_dir")

    def test_successful_load_prepares_runtime_dir(self):
        with mock.patch(
            "pipeline.models.spip_plugin.spip_loader.find_rscript", return_value="Rscript"
        ), mock.patch(
            "pipeline.models.spip_plugin.spip_loader.ensure_r_packages", return_value=True
        ), mock.patch(
            "pipeline.models.spip_plugin.spip_loader.prepare_runtime_dir",
            return_value=Path("fake_runtime_dir"),
        ) as mock_prepare:
            self.instance._load_impl()

        mock_prepare.assert_called_once()
        self.assertEqual(self.instance._rscript_path, "Rscript")
        self.assertEqual(self.instance._runtime_dir, Path("fake_runtime_dir"))
        self.assertIsNotNone(self.instance.model)

    def test_missing_rscript_raises_clear_error(self):
        with mock.patch(
            "pipeline.models.spip_plugin.spip_loader.find_rscript", return_value=None
        ):
            with self.assertRaises(RuntimeError) as ctx:
                self.instance._load_impl()
        self.assertIn("Rscript", str(ctx.exception))

    def test_r_package_install_failure_raises_clear_error(self):
        with mock.patch(
            "pipeline.models.spip_plugin.spip_loader.find_rscript", return_value="Rscript"
        ), mock.patch(
            "pipeline.models.spip_plugin.spip_loader.ensure_r_packages", return_value=False
        ):
            with self.assertRaises(RuntimeError) as ctx:
                self.instance._load_impl()
        self.assertIn("R package", str(ctx.exception))

    def test_network_failure_is_sanitized(self):
        with mock.patch(
            "pipeline.models.spip_plugin.spip_loader.find_rscript", return_value="Rscript"
        ), mock.patch(
            "pipeline.models.spip_plugin.spip_loader.ensure_r_packages", return_value=True
        ), mock.patch(
            "pipeline.models.spip_plugin.spip_loader.prepare_runtime_dir",
            side_effect=ConnectionError("could not reach sourceforge.net"),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                self.instance._load_impl()
        self.assertEqual(str(ctx.exception), "SPiP model unavailable")
        self.assertNotIn("sourceforge.net", str(ctx.exception))

    def test_full_load_via_public_api_wraps_in_model_load_error_on_failure(self):
        with mock.patch(
            "pipeline.models.spip_plugin.spip_loader.find_rscript", return_value=None
        ):
            with self.assertRaises(ModelLoadError):
                self.instance.load()


class TestSpipMetadataAndAvailability(unittest.TestCase):
    def test_metadata_reports_commercial_use_allowed(self):
        meta = SpipPlugin.metadata()
        self.assertTrue(meta.commercial_use_allowed)
        self.assertEqual(meta.license_name, "MIT")
        self.assertIn("LBGC-CFB/SPiP", meta.source)

    def test_disabled_by_default_flag_off(self):
        with mock.patch("pipeline.models.spip_plugin.CONFIG") as mock_config:
            mock_config.splicing.ENABLE_SPIP = False
            self.assertFalse(SpipPlugin.is_available())
            self.assertIn("ENABLE_SPIP", SpipPlugin.unavailability_reason())

    def test_available_when_flag_on_and_rscript_found(self):
        with mock.patch("pipeline.models.spip_plugin.CONFIG") as mock_config, mock.patch(
            "pipeline.models.spip_plugin.spip_loader.find_rscript", return_value="Rscript"
        ):
            mock_config.splicing.ENABLE_SPIP = True
            self.assertTrue(SpipPlugin.is_available())

    def test_unavailable_when_flag_on_but_rscript_missing(self):
        with mock.patch("pipeline.models.spip_plugin.CONFIG") as mock_config, mock.patch(
            "pipeline.models.spip_plugin.spip_loader.find_rscript", return_value=None
        ):
            mock_config.splicing.ENABLE_SPIP = True
            self.assertFalse(SpipPlugin.is_available())
            self.assertIn("Rscript", SpipPlugin.unavailability_reason())


class TestSpipThroughModelManager(unittest.TestCase):
    def test_predict_returns_none_when_disabled(self):
        registry = ModelRegistry()
        registry.register("spip", SpipPlugin)
        manager = ModelManager(registry=registry)
        with mock.patch("pipeline.models.spip_plugin.CONFIG") as mock_config:
            mock_config.splicing.ENABLE_SPIP = False
            result = manager.predict("spip", "A" * 10, "T" * 10)
        self.assertIsNone(result)

    def test_get_raises_plugin_unavailable_when_disabled(self):
        registry = ModelRegistry()
        registry.register("spip", SpipPlugin)
        manager = ModelManager(registry=registry)
        with mock.patch("pipeline.models.spip_plugin.CONFIG") as mock_config:
            mock_config.splicing.ENABLE_SPIP = False
            with self.assertRaises(PluginUnavailableError):
                manager.get("spip")


if __name__ == "__main__":
    unittest.main()
