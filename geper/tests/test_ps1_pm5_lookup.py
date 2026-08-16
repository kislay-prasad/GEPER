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

import json
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


class _FakeCodon175Transcript:
    transcript_id = "ENST00000269305"
    chrom = "17"

    @staticmethod
    def genomic_positions_for_codon(codon_number):
        return [7675089]


class TestQueryCodonCapturesSubmitters(unittest.TestCase):
    """Round 30: `query_codon`'s returned matches carry `submitters`,
    fetched via a second, batched `efetch` request -- see
    `ClinVarCodonLookup._fetch_submitters`."""

    _ESUMMARY_ENTRY = {
        "uid": "12374",
        "accession": "VCV000012374",
        "title": "NM_000546.6(TP53):c.524G>A (p.Arg175His)",
        "variation_set": [{"canonical_spdi": "NC_000017.11:7675088:G:A"}],
        "germline_classification": {
            "description": "Pathogenic",
            "review_status": "reviewed by expert panel",
            "trait_set": [],
        },
    }

    def test_matches_carry_submitters_from_second_request(self):
        vcv_xml = """<?xml version="1.0"?>
<ClinVarResult-Set>
  <VariationArchive VariationID="12374">
    <ClassifiedRecord>
      <ClinicalAssertionList>
        <ClinicalAssertion ID="1">
          <ClinVarAccession Accession="SCV000042" SubmitterName="ClinGen LDCV" OrgID="7"/>
        </ClinicalAssertion>
      </ClinicalAssertionList>
    </ClassifiedRecord>
  </VariationArchive>
</ClinVarResult-Set>"""
        responses = [
            mock.Mock(
                status_code=200,
                raise_for_status=lambda: None,
                text=json.dumps({"esearchresult": {"idlist": ["12374"]}}),
            ),
            mock.Mock(
                status_code=200,
                raise_for_status=lambda: None,
                text=json.dumps({"result": {"uids": ["12374"], "12374": self._ESUMMARY_ENTRY}}),
            ),
            mock.Mock(status_code=200, raise_for_status=lambda: None, text=vcv_xml),
        ]
        lookup = ClinVarCodonLookup(cache=None)
        with mock.patch("pipeline.ps1_pm5.lookup.requests.get", side_effect=responses):
            result = lookup.query_codon(_FakeCodon175Transcript(), codon_number=175, assembly="GRCh38")

        self.assertTrue(result["found"])
        self.assertEqual(
            result["matches"][0]["submitters"],
            [{"name": "ClinGen LDCV", "org_id": "7", "scv": "SCV000042"}],
        )

    def test_submitter_fetch_failure_does_not_break_matches(self):
        responses = [
            mock.Mock(
                status_code=200,
                raise_for_status=lambda: None,
                text=json.dumps({"esearchresult": {"idlist": ["12374"]}}),
            ),
            mock.Mock(
                status_code=200,
                raise_for_status=lambda: None,
                text=json.dumps({"result": {"uids": ["12374"], "12374": self._ESUMMARY_ENTRY}}),
            ),
            # 3rd call (submitter efetch) has no valid JSON/XML text mock configured beyond this --
            # raise_for_status itself fails, exercising the best-effort catch.
            mock.Mock(status_code=500, raise_for_status=mock.Mock(side_effect=Exception("efetch down"))),
        ]
        lookup = ClinVarCodonLookup(cache=None)
        with mock.patch("pipeline.ps1_pm5.lookup.requests.get", side_effect=responses):
            result = lookup.query_codon(_FakeCodon175Transcript(), codon_number=175, assembly="GRCh38")

        self.assertTrue(result["found"])
        self.assertIsNone(result["matches"][0]["submitters"])


if __name__ == "__main__":
    unittest.main()
