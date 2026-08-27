"""
Unit tests for AM-06 (AlphaMissense catalogue pinning,
`models/alphamissense.py`): the fresh-download and cache-reuse pin
checks themselves need `requests`/`tabix`/a real ~9GB file and aren't
covered here (see `verify_alphamissense_integration.py` for that), but
the pure logic underneath both -- extracting GCS's `x-goog-hash` MD5,
deciding whether a build is pin-eligible, and the pin values' own
well-formedness -- has no such dependency and is cheap to get wrong
silently, so it's covered directly.
"""

import base64
import os
import unittest
from unittest import mock

from models.alphamissense import _CATALOGUE_PINS, _extract_goog_md5, _pin_for_build


class TestExtractGoogMd5(unittest.TestCase):
    def test_parses_md5_from_x_goog_hash(self):
        headers = {"x-goog-hash": "crc32c=n03x6A==,md5=n9Fnc18Wobh9pus+TCX8tQ=="}
        self.assertEqual(_extract_goog_md5(headers), "n9Fnc18Wobh9pus+TCX8tQ==")

    def test_parses_md5_when_it_is_the_only_component(self):
        headers = {"x-goog-hash": "md5=n9Fnc18Wobh9pus+TCX8tQ=="}
        self.assertEqual(_extract_goog_md5(headers), "n9Fnc18Wobh9pus+TCX8tQ==")

    def test_none_when_header_absent(self):
        self.assertIsNone(_extract_goog_md5({}))

    def test_none_when_header_has_no_md5_component(self):
        self.assertIsNone(_extract_goog_md5({"x-goog-hash": "crc32c=n03x6A=="}))


class TestPinForBuild(unittest.TestCase):
    def test_pin_applies_by_default(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GEPER_ALPHAMISSENSE_HG38_URL", None)
            self.assertEqual(_pin_for_build("hg38"), _CATALOGUE_PINS["hg38"])

    def test_pin_does_not_apply_when_url_overridden(self):
        with mock.patch.dict(os.environ, {"GEPER_ALPHAMISSENSE_HG38_URL": "https://example.com/mirror.tsv.gz"}):
            self.assertIsNone(_pin_for_build("hg38"))

    def test_unknown_build_has_no_pin(self):
        self.assertIsNone(_pin_for_build("hg17"))


class TestPinValuesWellFormed(unittest.TestCase):
    """Sanity check on the pin values themselves (verified live via the
    GCS JSON API, per the dispatch that supplied them) -- catches a
    transcription error (wrong base64 padding, wrong length) rather
    than re-verifying the values are the CORRECT ones, which nothing
    offline can do."""

    def test_both_builds_pinned(self):
        self.assertIn("hg38", _CATALOGUE_PINS)
        self.assertIn("hg19", _CATALOGUE_PINS)

    def test_md5_values_decode_to_16_bytes(self):
        for build, pin in _CATALOGUE_PINS.items():
            with self.subTest(build=build):
                decoded = base64.b64decode(pin["md5"])
                self.assertEqual(len(decoded), 16, f"{build}'s pinned MD5 does not decode to a 16-byte digest")

    def test_generations_are_numeric_strings(self):
        for build, pin in _CATALOGUE_PINS.items():
            with self.subTest(build=build):
                self.assertTrue(pin["generation"].isdigit(), f"{build}'s pinned generation is not numeric")


if __name__ == "__main__":
    unittest.main()
