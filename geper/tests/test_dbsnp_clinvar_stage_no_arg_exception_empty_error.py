"""
FIX #10: `_run_dbsnp_stage` and `_run_clinvar_stage` build their failure
result with a bare `str(exc)` (pipeline/orchestrator.py:2442, :2453).
`str(exc)` is the empty string for any exception raised with no
message (`ExternalAPIError()`, a bare `ConnectionError()`, ...), so a
stage that genuinely failed returns `{"found": False, "error": ""}`.
Every downstream consumer that gates on the error field by truthiness
reads `""` as "no error" -- a failed lookup becomes a confirmed
negative. Same defect class as d1128ee (gnomAD), which fixed the
identical mechanism in its producer with:

    detail = str(exc) or type(exc).__name__

This file matches that shape for the two sites named in FIX #10.

WHAT THIS FILE ASSERTS

  A. THE DANGEROUS CASE: a no-argument exception must not produce an
     empty `error` string. This is the red-first case -- pre-fix, both
     tests below fail because `error` comes back `""`.
  B. THE CONTROL: an exception raised WITH a message is still reported
     verbatim, unchanged -- the fix must not paper over or rewrite a
     real message, only supply one when there wasn't any.
"""

import unittest
from types import SimpleNamespace
from unittest import mock

from pipeline.orchestrator import GeperPipeline
from utils.exceptions import ExternalAPIError


def _variant():
    return SimpleNamespace(chrom="17", pos=43057051, ref="A", alt="T", build="GRCh38", gene="BRCA1")


def _pipeline():
    p = GeperPipeline.__new__(GeperPipeline)
    p.enable_profiling = False
    p.sequence_context_gen = SimpleNamespace(assembly="GRCh38")
    return p


class TestDbSNPStageNoArgExceptionDoesNotProduceEmptyError(unittest.TestCase):
    def test_no_arg_exception_error_field_is_not_empty(self):
        p = _pipeline()
        client = mock.MagicMock()
        client.lookup_variant.side_effect = ExternalAPIError()
        p.dbsnp_client = client
        errors: list = []
        result = p._run_dbsnp_stage(_variant(), errors)
        self.assertEqual(result["found"], False)
        self.assertNotEqual(
            result["error"],
            "",
            "a no-argument ExternalAPIError must not collapse to an empty error string -- "
            "a truthiness-guarded downstream consumer reads '' as no error at all",
        )

    def test_a_real_message_is_still_reported_verbatim(self):
        p = _pipeline()
        client = mock.MagicMock()
        client.lookup_variant.side_effect = ExternalAPIError("dbSNP timed out after 3 attempts")
        p.dbsnp_client = client
        errors: list = []
        result = p._run_dbsnp_stage(_variant(), errors)
        self.assertEqual(result["error"], "dbSNP timed out after 3 attempts")


class TestClinVarStageNoArgExceptionDoesNotProduceEmptyError(unittest.TestCase):
    def test_no_arg_exception_error_field_is_not_empty(self):
        p = _pipeline()
        client = mock.MagicMock()
        client.query_variant.side_effect = ExternalAPIError()
        p.clinvar_client = client
        errors: list = []
        result = p._run_clinvar_stage(_variant(), {"rsid": None}, errors)
        self.assertEqual(result["found"], False)
        self.assertNotEqual(
            result["error"],
            "",
            "a no-argument ExternalAPIError must not collapse to an empty error string -- "
            "a truthiness-guarded downstream consumer reads '' as no error at all",
        )

    def test_a_real_message_is_still_reported_verbatim(self):
        p = _pipeline()
        client = mock.MagicMock()
        client.query_variant.side_effect = ExternalAPIError("ClinVar returned 503")
        p.clinvar_client = client
        errors: list = []
        result = p._run_clinvar_stage(_variant(), {"rsid": None}, errors)
        self.assertEqual(result["error"], "ClinVar returned 503")


if __name__ == "__main__":
    unittest.main()
