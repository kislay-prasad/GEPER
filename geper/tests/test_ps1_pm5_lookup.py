"""
Round 27: `pipeline/ps1_pm5/lookup.py::ClinVarCodonLookup._request_json`
used to raise `ExternalAPIError` with the full request URL (and the raw
underlying exception text) folded into its message via an f-string --
the same shape `database/clinvar_client.py` had before round 26's fix,
which this module's own comment ("mirrors database/clinvar_client.py's
shape") already pointed at.

That message is caught per-codon-position in `query_codon`, joined into
the returned dict's `error` field, and from there reaches TWO
unconditional sinks (traced round 27, not assumed):
  - `pipeline/orchestrator.py::_run_clinvar_codon_stage` folds it into
    the shared `errors` list, embedded verbatim in geper_results.json's
    top-level `errors` key by `report/json_builder.py`, AND rendered
    unconditionally into every Markdown report's "### ⚠ Stage
    Warnings / Errors" section by `report/report_generator.py` whenever
    that list is non-empty.
  - `report/json_builder.py` also embeds the raw `clinvar_codon_result`
    dict (error field and all) verbatim under geper_results.json's
    `clinvar_codon_matches` key.

Full detail still reaches the log; what's raised/returned to callers
must not carry the URL.
"""

import unittest
from unittest import mock

from pipeline.ps1_pm5.lookup import ClinVarCodonLookup
from utils.exceptions import ExternalAPIError


class TestRequestJsonIsSanitized(unittest.TestCase):
    def test_retry_exhausted_raises_sanitized_message(self):
        import requests as real_requests

        lookup = ClinVarCodonLookup(cache=None)
        with (
            mock.patch(
                "pipeline.ps1_pm5.lookup.requests.get",
                side_effect=real_requests.ConnectionError("Failed to establish a new connection"),
            ),
            mock.patch("pipeline.ps1_pm5.lookup.time.sleep"),
        ):
            with self.assertRaises(ExternalAPIError) as ctx:
                lookup._esearch("17[chr] AND 7674221[chrpos38]")

        message = str(ctx.exception)
        self.assertNotIn("http", message)
        self.assertNotIn("eutils.ncbi.nlm.nih.gov", message)
        self.assertNotIn("Failed to establish a new connection", message)
        self.assertEqual(message, "PS1/PM5 ClinVar request failed after 3 attempts")

    def test_query_codon_error_field_never_carries_the_url(self):
        """
        End-to-end through `query_codon`'s own per-position catch
        (`errors.append(str(exc))` at line ~100) -- the exact string
        this module hands back to `pipeline/orchestrator.py`, and from
        there to both report sinks described in this file's own
        docstring.
        """
        import requests as real_requests

        class _FakeTranscript:
            transcript_id = "ENST00000269305"
            chrom = "17"

            @staticmethod
            def genomic_positions_for_codon(codon_number):
                return [7674221]

        lookup = ClinVarCodonLookup(cache=None)
        with (
            mock.patch(
                "pipeline.ps1_pm5.lookup.requests.get",
                side_effect=real_requests.ConnectionError("Failed to establish a new connection"),
            ),
            mock.patch("pipeline.ps1_pm5.lookup.time.sleep"),
        ):
            result = lookup.query_codon(_FakeTranscript(), codon_number=175, assembly="GRCh38")

        self.assertIsNotNone(result["error"])
        self.assertNotIn("http", result["error"])
        self.assertNotIn("eutils.ncbi.nlm.nih.gov", result["error"])
        self.assertNotIn("Failed to establish a new connection", result["error"])


if __name__ == "__main__":
    unittest.main()
