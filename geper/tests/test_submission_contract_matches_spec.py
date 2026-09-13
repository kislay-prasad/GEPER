"""
GEPER_CLINICAL_PLATFORM_SPEC.md section 10.3's contract block must match the
request/response models geper/api/main.py actually serves.

Why this test exists: that block was written before the endpoints existed and
was never reconciled. By 2026-09-13 it named fields the API does not have
(`error`, `run_document_ref`), missed fields it requires (`order_id`,
`sample_id`), and keyed both responses on the wrong field. Nobody noticed,
because nothing compared them. Human ruling that day: the implementation is
the fact and the document is the claim that drifted, so the block moved -- and
a guard is what stops it drifting again.

WHAT THIS COMPARES, precisely: the field names are read out of the LIVE
pydantic models by importing api.main, not copied into this file as strings.
`InterpretationSubmissionRequest`, `InterpretationSubmissionResponse` and
`InterpretationStatusResponse` supply their own `model_fields`; the spec's
fenced block is parsed for its JSON keys; the two sets must be equal. Renaming
`error_message`, adding a request field, or dropping one from the API fails
this test on the next run, without anyone editing this file.

WHAT IT DOES NOT CHECK: types, and the optional/required split within the POST
body -- the block writes types as illustrative placeholders ("<path>",
"GRCh38") rather than as a schema, and `assembly` is a plain string in both
places. Field NAMES and their presence are the coupled part; types are not.

`run_document_ref` is deliberately NOT a model field and deliberately still
named in the block, as the section 11.3 discovery gap. The test asserts both
halves of that: it is absent from every model, and the block still marks it
NOT SERVED. Folding it into a cleanup would erase a real finding, so a future
edit that deletes the marking fails here.
"""

import os
import re
import unittest
from pathlib import Path

_GEPER_ROOT = Path(__file__).resolve().parents[1]
_SPEC = _GEPER_ROOT.parent / "GEPER_CLINICAL_PLATFORM_SPEC.md"

# api.main refuses to import without an auth posture; this opens no port.
os.environ.setdefault("GEPER_DEV_INSECURE", "1")

_JSON_KEY = re.compile(r'"([a-z_]+)"\s*:')


def _contract_block():
    """The fenced block under section 10.3's 'The service contract, as served.'"""
    text = _SPEC.read_text(encoding="utf-8")
    anchor = text.index("**The service contract, as served.**")
    fence_open = text.index("```", anchor)
    fence_close = text.index("```", fence_open + 3)
    return text[fence_open + 3 : fence_close]


def _post_and_get_sections():
    block = _contract_block()
    split = block.index("GET /interpretations")
    return block[:split], block[split:]


def _keys(chunk):
    return set(_JSON_KEY.findall(chunk))


def _model_fields(model):
    return set(model.model_fields)


class TestContractBlockMatchesTheServedModels(unittest.TestCase):
    def setUp(self):
        from api.main import (
            InterpretationStatusResponse,
            InterpretationSubmissionRequest,
            InterpretationSubmissionResponse,
        )

        self.request_model = InterpretationSubmissionRequest
        self.submission_response = InterpretationSubmissionResponse
        self.status_response = InterpretationStatusResponse

    def test_post_section_names_exactly_the_request_and_response_fields(self):
        post, _ = _post_and_get_sections()
        expected = _model_fields(self.request_model) | _model_fields(self.submission_response)
        self.assertEqual(_keys(post), expected)

    def test_get_section_names_exactly_the_status_response_fields(self):
        _, get = _post_and_get_sections()
        self.assertEqual(_keys(get), _model_fields(self.status_response))

    def test_the_required_request_fields_are_all_in_the_block(self):
        post, _ = _post_and_get_sections()
        required = {name for name, f in self.request_model.model_fields.items() if f.is_required()}
        # Named explicitly: wave 113 (2026-09-12) made these two required, and
        # their absence from the block is what made it wrong before.
        self.assertLessEqual({"order_id", "sample_id"}, required)
        self.assertLessEqual(required, _keys(post))

    def test_the_organisation_is_not_a_body_field(self):
        # It comes from the API key. A body field would let a caller assert an
        # organisation it holds no key for.
        self.assertNotIn("org_id", _model_fields(self.request_model))
        post, _ = _post_and_get_sections()
        self.assertNotIn("org_id", _keys(post))
        self.assertIn("organisation is taken from the API key", post)

    def test_both_responses_key_on_id_with_interpretation_id_optional(self):
        for model in (self.submission_response, self.status_response):
            with self.subTest(model=model.__name__):
                self.assertIn("id", _model_fields(model))
                self.assertFalse(model.model_fields["interpretation_id"].is_required())

    def test_the_block_records_the_status_codes_the_route_actually_serves(self):
        """Wave 117, 2026-09-13. The block used to show one outcome, 202, and
        tell clients not to branch on the status code; the route now answers
        202 for a created submission and 200 for a replay.

        Coupled, not copied: 202 is read off the LIVE route's default
        `status_code` (that default IS the create branch -- the replay branch
        overrides it on the response), so changing the route's declared code
        without moving the block fails here. The 200 half is checked as the
        block naming it alongside the word "replay", because a per-branch
        override is set in the handler body and no route attribute exposes it.
        """
        from api.main import app

        post_route = next(
            r
            for r in app.routes
            if getattr(r, "path", None) == "/interpretations" and "POST" in getattr(r, "methods", ())
        )
        post, _ = _post_and_get_sections()
        self.assertEqual(post_route.status_code, 202)
        self.assertIn(f"→ {post_route.status_code}", post)
        self.assertIn("→ 200", post)
        self.assertIn("REPLAYED", post)
        # The retired instruction. Its return would mean the distinction was
        # collapsed again.
        self.assertNotIn("not on the status code", post)

    def test_the_failure_field_is_error_message_not_error(self):
        fields = _model_fields(self.status_response)
        self.assertIn("error_message", fields)
        self.assertNotIn("error", fields)


class TestRunDocumentRefStaysMarkedAsAnOpenGap(unittest.TestCase):
    """Section 11.3's discovery gap. The spec is right that it is missing; the
    reconciliation must not erase that by deleting the mention."""

    def setUp(self):
        from api.main import InterpretationStatusResponse, InterpretationSubmissionResponse

        self.responses = (InterpretationSubmissionResponse, InterpretationStatusResponse)

    def test_no_response_model_serves_it(self):
        for model in self.responses:
            with self.subTest(model=model.__name__):
                self.assertNotIn("run_document_ref", _model_fields(model))

    def test_it_is_not_a_key_in_the_contract_block(self):
        self.assertNotIn("run_document_ref", _keys(_contract_block()))

    def test_the_spec_still_records_it_as_not_served(self):
        text = _SPEC.read_text(encoding="utf-8")
        self.assertIn("**NOT SERVED TODAY: `run_document_ref`.**", text)
        self.assertIn("discovery gap", text)


if __name__ == "__main__":
    unittest.main()
