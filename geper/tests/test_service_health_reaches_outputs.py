"""
Finding 5: a handled external-source failure must be visible in the
report, not only on the console.

Every external client retries transient failures itself, so a failure
that was retried raises nothing that reaches a variant's `errors` list
(that list collects stage exceptions -- see `pipeline/orchestrator.py`).
Before this, that meant a degraded run wrote `errors: []`,
`run_complete: true`, and four output artefacts that said nothing,
while the console printed the degradation and exited.

Observed 2026-08-29 on a real run: Ensembl probed Online (1155 ms) at
startup, then failed 3/3 attempts fetching a sequence region and 3/3
fetching an exon annotation. The console printed
`Ensembl ... Intermittent (2 failures)`. Searching all four output
artefacts for `500|503|Service Unavailable|after 3 attempts` returned
one hit -- an unrelated version-endpoint note. The one visible
consequence was the exon-annotation failure rendered as the biological
claim "no exon annotation found near this variant", for a canonical
splice-acceptor variant.

These tests drive the REAL retry loop in `pipeline/sequence_context.py`
with a forced 503 rather than calling `note_failure` directly, so they
exercise the path that actually produced that run.
"""

import os
import re
import tempfile
import unittest
from unittest import mock

import requests

from report.json_builder import JSONResultBuilder
from report.report_generator import ReportGenerator
from report.summary import generate_pdf
from report.summary_short import generate_short_pdf
from utils.service_health import HEALTH

try:
    from pypdf import PdfReader

    _PYPDF_AVAILABLE = True
except ImportError:  # pragma: no cover - environment-dependent
    _PYPDF_AVAILABLE = False


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _pdf_text(path: str) -> str:
    return "\n".join(page.extract_text() or "" for page in PdfReader(path).pages)


def _resp_503():
    """A response object whose raise_for_status() raises like a real 503."""
    response = mock.Mock()
    response.status_code = 503
    response.raise_for_status.side_effect = requests.HTTPError("503 Server Error: Service Unavailable")
    return response


def _force_ensembl_503():
    """
    Drive the real retry loop in SequenceContextGenerator._fetch_region
    to exhaustion with a forced 503, so HEALTH records the failure the
    same way the observed run did.
    """
    from pipeline.sequence_context import SequenceContextGenerator
    from utils.exceptions import ExternalAPIError

    generator = SequenceContextGenerator(assembly="GRCh38")
    with (
        mock.patch.object(generator._session, "get", return_value=_resp_503()),
        mock.patch("pipeline.sequence_context.time.sleep", return_value=None),
    ):
        try:
            generator._fetch_region("17", 43106034, 43107034)
        except ExternalAPIError:
            pass


def _document_from_a_degraded_run() -> dict:
    """
    A run document built through the real JSONResultBuilder with the
    real registry -- not a hand-written dict -- so these tests fail if
    the orchestrator-to-document wiring is what breaks.
    """
    HEALTH.reset()
    _force_ensembl_503()
    builder = JSONResultBuilder(
        input_vcf_path="finding5_test.vcf",
        assembly="GRCh38",
        service_health_registry=HEALTH,
    )
    builder.run_complete = True
    return builder.build()


class TestForcedFailureIsRecorded(unittest.TestCase):
    def test_a_forced_503_through_the_real_retry_loop_is_recorded(self):
        HEALTH.reset()
        _force_ensembl_503()
        snapshot = HEALTH.snapshot()

        ensembl = [s for s in snapshot if s["service"] == "Ensembl"]
        self.assertTrue(
            ensembl,
            "the forced 503 did not reach the health registry at all, so every other assertion in "
            "this file would be testing nothing. Treat this as the test going blind (the retry loop "
            "or its HEALTH.note_failure call moved), not as the reporting feature being absent.",
        )
        self.assertGreater(
            ensembl[0]["failure_count"],
            0,
            f"Ensembl is in the registry but with no recorded failure: {ensembl[0]!r}",
        )
        self.assertTrue(ensembl[0]["degraded"], f"a recorded failure did not mark the source degraded: {ensembl[0]!r}")

    def test_a_clean_run_is_not_reported_as_degraded(self):
        """
        Negative control. Without this, every assertion below would pass
        against a renderer that unconditionally printed a warning.
        """
        HEALTH.reset()
        document = JSONResultBuilder(input_vcf_path="clean.vcf", service_health_registry=HEALTH).build()
        self.assertEqual(document["service_health"], [])
        markdown = ReportGenerator().generate(document)
        self.assertNotIn("failed at least once during this run", markdown)


class TestDegradationReachesAllFourArtefacts(unittest.TestCase):
    """
    The indicator must appear in the JSON, the Markdown, the full PDF
    and the short PDF. Four surfaces, because a reader of any one of
    them is entitled to know the run was degraded, and the short PDF is
    the one a clinician is most likely to be handed.
    """

    def setUp(self):
        self.document = _document_from_a_degraded_run()
        # Precondition: if the document itself has no degradation, the
        # artefact assertions cannot mean anything.
        degraded = [s for s in self.document.get("service_health", []) if s.get("degraded")]
        self.assertTrue(
            degraded,
            "the run document records no degraded source, so the four artefact checks below would "
            "pass or fail for the wrong reason. This is the wiring between the registry and "
            "JSONResultBuilder, not the renderers.",
        )

    def tearDown(self):
        HEALTH.reset()

    def test_artefact_1_json(self):
        entry = [s for s in self.document["service_health"] if s["service"] == "Ensembl"][0]
        self.assertTrue(entry["degraded"])
        self.assertGreater(entry["failure_count"], 0)
        self.assertIn("Intermittent", entry["status"])

    def test_artefact_2_markdown(self):
        markdown = _normalize(ReportGenerator().generate(self.document))
        self.assertIn("failed at least once during this run and were retried", markdown)
        self.assertIn("Ensembl", markdown)

    @unittest.skipUnless(_PYPDF_AVAILABLE, "pypdf not installed")
    def test_artefact_3_full_pdf(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "full.pdf")
            generate_pdf(self.document, path)
            text = _normalize(_pdf_text(path))
        self.assertIn("failed at least once during this run and were retried", text)
        self.assertIn("Ensembl", text)

    @unittest.skipUnless(_PYPDF_AVAILABLE, "pypdf not installed")
    def test_artefact_4_short_pdf(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "short.pdf")
            generate_short_pdf(self.document, path)
            text = _normalize(_pdf_text(path))
        self.assertIn("failed at least once during this run and were retried", text)
        self.assertIn("Ensembl", text)


class TestDegradationSurvivesAReRender(unittest.TestCase):
    """
    `review/signoff.py` loads a stored geper_results.json back IN A
    FRESH PROCESS and re-invokes both PDF renderers and the Markdown
    generator against it. In that process the HEALTH singleton has
    never run a check, so anything derived from the live registry is
    gone -- which is how approving a run could produce a SIGNED,
    clinician-facing PDF with the availability caveat silently dropped.

    Same failure, and the same fix, as `qc_metrics` on
    JSONResultBuilder: a caveat in the document survives, an argument
    passed once does not.
    """

    def test_caveat_is_rendered_from_the_document_not_the_live_registry(self):
        document = _document_from_a_degraded_run()

        # Simulate the fresh process signoff.py runs in.
        HEALTH.reset()
        self.assertEqual(
            HEALTH.snapshot(),
            [],
            "the registry was not actually cleared, so this test would pass even if the caveat "
            "still depended on live process state -- exactly what it exists to rule out.",
        )

        markdown = _normalize(ReportGenerator().generate(document))
        self.assertIn("failed at least once during this run and were retried", markdown)
        self.assertIn("Ensembl", markdown)


if __name__ == "__main__":
    unittest.main()
