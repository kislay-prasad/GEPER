"""
`list_pending` must distinguish "this run used no such model" from
"this run never recorded which models it used".

WHY THIS MATTERS MORE THAN AN ORDINARY PROJECTION FIELD.
An impact query built on this projection is used to decide WHO GETS
RECALLED. If a run that never recorded its model identifiers simply
produces no hits, the reader sees "no affected reports" when the truth is
"we cannot tell". *** A PARTIAL ANSWER READS AS A COMPLETE ONE. That is
not a degraded feature; it is a wrong answer with a confident face, and
the wrongness is in the direction of not recalling someone. ***

So a run whose document predates model-checkpoint capture must surface an
EXPLICIT UNRECORDED STATE. `report/json_builder.py` already draws exactly
this distinction when carrying a prior document forward -- `known =
"model_checkpoints" in prior_document and checkpoints is not None` -- and
this projection must not throw it away at the last step.

VOCABULARY: `VersionStatus`, provenance.py's own, rather than a parallel
one invented here. NOT_CONSULTED already means "never queried this run",
which is precisely the state of a run that recorded no model identifiers,
and it is deliberately a different member from UNKNOWN ("queried, nothing
obtainable").
"""

import json
import os
import tempfile
import unittest

from pipeline.provenance import VersionStatus
from review.signoff import list_pending

RESULTS = "geper_results.json"


def _write_run(root: str, name: str, document: dict) -> str:
    run_dir = os.path.join(root, name)
    os.makedirs(run_dir)
    with open(os.path.join(run_dir, RESULTS), "w", encoding="utf-8") as fh:
        json.dump(document, fh)
    return run_dir


class ListPendingModelProvenanceTest(unittest.TestCase):
    def test_a_run_that_recorded_checkpoints_exposes_them(self):
        with tempfile.TemporaryDirectory() as root:
            _write_run(
                root,
                "recorded",
                {
                    "variants": [],
                    "variant_count": 0,
                    "model_checkpoints": {"esm2": {"identifier": "facebook/esm2 @ 08e4846e"}},
                },
            )
            (entry,) = list_pending(root)
            self.assertIs(entry["model_provenance_status"], VersionStatus.VERSION_KNOWN)
            self.assertIn("esm2", entry["model_checkpoints"])

    def test_a_run_missing_the_key_is_UNRECORDED_not_empty(self):
        """
        *** THE CENTRAL ASSERTION. *** A document written before checkpoint
        capture existed must NOT look like a run that used no models.
        """
        with tempfile.TemporaryDirectory() as root:
            _write_run(root, "legacy", {"variants": [], "variant_count": 0})
            (entry,) = list_pending(root)
            self.assertIs(
                entry["model_provenance_status"],
                VersionStatus.NOT_CONSULTED,
                "a run with no recorded identifiers must be explicitly unrecorded, "
                "never silently indistinguishable from one that matched nothing",
            )
            self.assertIsNone(entry["model_checkpoints"])

    def test_an_explicit_null_is_also_UNRECORDED(self):
        """`json_builder` writes None when a carried-forward document had no
        checkpoints; present-but-null is the same gap as absent."""
        with tempfile.TemporaryDirectory() as root:
            _write_run(root, "nulled", {"variants": [], "variant_count": 0, "model_checkpoints": None})
            (entry,) = list_pending(root)
            self.assertIs(entry["model_provenance_status"], VersionStatus.NOT_CONSULTED)

    def test_unrecorded_and_recorded_runs_are_both_returned(self):
        """
        The unrecorded run must appear in the results at all. If it were
        filtered out, a caller counting rows would again read "no affected
        reports" where the truth is "we cannot tell for this one".
        """
        with tempfile.TemporaryDirectory() as root:
            _write_run(root, "legacy", {"variants": [], "variant_count": 0})
            _write_run(root, "recorded", {"variants": [], "variant_count": 0, "model_checkpoints": {"esm2": {}}})
            statuses = {e["model_provenance_status"] for e in list_pending(root)}
            self.assertEqual(statuses, {VersionStatus.NOT_CONSULTED, VersionStatus.VERSION_KNOWN})

    def test_existing_projection_fields_are_untouched(self):
        with tempfile.TemporaryDirectory() as root:
            _write_run(root, "r", {"variants": [], "variant_count": 3, "generated_at": "2026-09-10"})
            (entry,) = list_pending(root)
            for key in ("output_dir", "report_date", "num_variants", "has_conflicting_evidence", "status"):
                self.assertIn(key, entry)
            self.assertEqual(entry["num_variants"], 3)
            self.assertEqual(entry["status"], "DRAFT")


if __name__ == "__main__":
    unittest.main()
