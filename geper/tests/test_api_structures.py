"""
Tests for api/main.py's structure-annotation endpoint (`GET /structures/{accession}`).

Two layers, deliberately kept separate:
  - `_map_to_response` is a pure function (raw AlphaFoldLookup dict -> the
    ratified StructureAnnotationResponse shape) and is tested directly,
    against real dict shapes copied from what
    `pipeline/alphafold/lookup.py::AlphaFoldLookup.query_variant` actually
    returns for each of its distinct code paths -- not invented shapes.
  - The route itself is tested through FastAPI's TestClient, with
    `AlphaFoldLookup` swapped out via `app.dependency_overrides` (matching
    kim_pipeline/tests/test_gap_fixes.py's own TestClient pattern), to
    prove the wiring (route registration, query-param handling, response
    serialization) independently of the mapping logic already covered above.

`mapping_status` for a "found, not mapped" result splits into `NO_POSITION`
(no protein position was ever resolved) vs. `NOT_MAPPABLE` (a real position
existed but the gate rejected it on the merits), derived from `raw`'s own
`protein_position` field -- see api/main.py's module docstring for why that
field is a reliable signal (route (c), ruled 2026-08-26, verified by
execution before implementation).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any, Dict, Optional

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# `api.main` imports `fastapi` at ITS OWN module level (`from fastapi import
# Depends, FastAPI`) -- this file's own module-level import of `api.main` was
# therefore unguarded too, and on a machine lacking fastapi that raised
# ModuleNotFoundError during COLLECTION of this file, which (this repo's
# pytest invocations carry no `--continue-on-collection-errors`, confirmed
# against both pytest.ini and kim_pipeline/pyproject.toml) aborts collection
# of the whole suite run alongside it. Guarded the same way this repo already
# guards other optional-dependency test modules (b01ae0a,
# test_enformer_plugin.py/test_borzoi_plugin.py): a module-level try/except
# flag plus a class-level `unittest.skipUnless`, not a different mechanism.
try:
    from api.main import EntryStatus, MappingStatus, _map_to_response

    _HAS_FASTAPI = True
except ImportError:
    _HAS_FASTAPI = False

_FASTAPI_SKIP_REASON = (
    "fastapi is not installed in this environment -- api/main.py requires it "
    "at module scope; fastapi/uvicorn are newly-added, optional-until-you-"
    "run-the-API dependencies (see requirements.txt), not a broken environment."
)


# ─── Fixtures: real shapes AlphaFoldLookup.query_variant actually returns ─────
# Each one copied from reading pipeline/alphafold/lookup.py and
# pipeline/alphafold/provider.py directly, not guessed at.


def _skipped_disabled() -> Dict[str, Any]:
    """CONFIG.alphafold.ENABLED is False -- lookup.py's `_SKIPPED_RESULT`,
    returned before `accession` is even computed, so no "accession" key
    at all (not even None)."""
    return {
        "skipped": True,
        "reason": "AlphaFold integration disabled via GEPER_ENABLE_ALPHAFOLD=false",
        "found": False,
    }


def _no_accession() -> Dict[str, Any]:
    """query_variant's early return when the UniProt stage resolved no accession."""
    return {
        "skipped": False,
        "found": False,
        "accession": None,
        "error": None,
        "reason": "no UniProt accession available for this variant's gene; AlphaFold DB annotation requires one",
    }


def _lookup_error(accession: str = "P04637") -> Dict[str, Any]:
    """AlphaFoldAnnotation.from_error(...).to_dict(), as returned when the
    AlphaFold DB summary query itself fails (network/parse error)."""
    return {
        "skipped": False,
        "accession": accession,
        "source": "error",
        "found": False,
        "model_version": None,
        "pdb_url": None,
        "cif_url": None,
        "uniprot_start": None,
        "uniprot_end": None,
        "mean_plddt": None,
        "mean_plddt_band": None,
        "protein_position": None,
        "affected_residue_plddt": None,
        "affected_residue_band": None,
        "protein_position_basis": None,
        "structure_fetched": False,
        "error": "AlphaFold DB API request failed after 3 attempts",
        "mapping_unavailable_reason": None,
    }


def _clean_not_found(accession: str = "Q99999") -> Dict[str, Any]:
    """AlphaFoldAnnotation.not_found(...).to_dict() -- a genuine 404, no entry exists."""
    return {
        "skipped": False,
        "accession": accession,
        "source": "alphafold_db_api",
        "found": False,
        "model_version": None,
        "pdb_url": None,
        "cif_url": None,
        "uniprot_start": None,
        "uniprot_end": None,
        "mean_plddt": None,
        "mean_plddt_band": None,
        "protein_position": None,
        "affected_residue_plddt": None,
        "affected_residue_band": None,
        "protein_position_basis": None,
        "structure_fetched": False,
        "error": None,
        "mapping_unavailable_reason": None,
    }


def _found_mapped(accession: str = "P04637", position: int = 175) -> Dict[str, Any]:
    """Entry found, mapping gate returned (True, None) -- the clean success case."""
    return {
        "skipped": False,
        "accession": accession,
        "source": "alphafold_db_api",
        "found": True,
        "model_version": "4",
        "pdb_url": f"https://alphafold.ebi.ac.uk/files/AF-{accession}-F1-model_v4.pdb",
        "cif_url": f"https://alphafold.ebi.ac.uk/files/AF-{accession}-F1-model_v4.cif",
        "uniprot_start": 1,
        "uniprot_end": 393,
        "mean_plddt": 91.2,
        "mean_plddt_band": "very_high",
        "protein_position": position,
        "affected_residue_plddt": 95.63,
        "affected_residue_band": "very_high",
        "protein_position_basis": "transcript_cds",
        "structure_fetched": True,
        "error": None,
        "mapping_unavailable_reason": None,
    }


def _found_not_mapped_no_position(accession: str = "P04637") -> Dict[str, Any]:
    """Entry found, but no protein position was ever resolved upstream --
    the gate's own first branch. `protein_position` stays None (it's just
    an echo of what was passed in)."""
    return {
        "skipped": False,
        "accession": accession,
        "source": "alphafold_db_api",
        "found": True,
        "model_version": "4",
        "pdb_url": f"https://alphafold.ebi.ac.uk/files/AF-{accession}-F1-model_v4.pdb",
        "cif_url": f"https://alphafold.ebi.ac.uk/files/AF-{accession}-F1-model_v4.cif",
        "uniprot_start": 1,
        "uniprot_end": 393,
        "mean_plddt": 91.2,
        "mean_plddt_band": "very_high",
        "protein_position": None,
        "affected_residue_plddt": None,
        "affected_residue_band": None,
        "protein_position_basis": None,
        "structure_fetched": True,
        "error": None,
        "mapping_unavailable_reason": "no protein position was resolved for this variant",
    }


def _found_not_mapped_out_of_range(accession: str = "P04637", position: int = 5000) -> Dict[str, Any]:
    """Entry found, a real protein position existed, gate rejected it on
    the merits (out of the declared UniProt span)."""
    return {
        "skipped": False,
        "accession": accession,
        "source": "alphafold_db_api",
        "found": True,
        "model_version": "4",
        "pdb_url": f"https://alphafold.ebi.ac.uk/files/AF-{accession}-F1-model_v4.pdb",
        "cif_url": f"https://alphafold.ebi.ac.uk/files/AF-{accession}-F1-model_v4.cif",
        "uniprot_start": 1,
        "uniprot_end": 393,
        "mean_plddt": 91.2,
        "mean_plddt_band": "very_high",
        "protein_position": position,
        "affected_residue_plddt": None,
        "affected_residue_band": None,
        "protein_position_basis": "transcript_cds",
        "structure_fetched": True,
        "error": None,
        "mapping_unavailable_reason": (
            f"protein position {position} lies outside the AlphaFold entry's declared UniProt span 1-393"
        ),
    }


@unittest.skipUnless(_HAS_FASTAPI, _FASTAPI_SKIP_REASON)
class TestMapToResponseNotRun(unittest.TestCase):
    def test_config_disabled_is_not_run_with_reason_and_no_accession(self):
        resp = _map_to_response(_skipped_disabled())
        self.assertEqual(resp.entry_status, EntryStatus.NOT_RUN)
        self.assertEqual(resp.entry_status_reason, "AlphaFold integration disabled via GEPER_ENABLE_ALPHAFOLD=false")
        self.assertIsNone(resp.accession)
        self.assertIsNone(resp.pdb_url)
        self.assertIsNone(resp.model_version)
        self.assertIsNone(resp.mapping_status)
        self.assertIsNone(resp.mapped_residue)

    def test_no_accession_is_also_not_run_distinct_reason(self):
        resp = _map_to_response(_no_accession())
        self.assertEqual(resp.entry_status, EntryStatus.NOT_RUN)
        self.assertIn("no UniProt accession available", resp.entry_status_reason)
        self.assertIsNone(resp.accession)


@unittest.skipUnless(_HAS_FASTAPI, _FASTAPI_SKIP_REASON)
class TestMapToResponseError(unittest.TestCase):
    def test_lookup_failure_is_error_not_not_found(self):
        resp = _map_to_response(_lookup_error())
        self.assertEqual(resp.entry_status, EntryStatus.ERROR)
        self.assertEqual(resp.entry_status_reason, "AlphaFold DB API request failed after 3 attempts")
        self.assertEqual(resp.accession, "P04637")
        self.assertIsNone(resp.pdb_url)
        self.assertIsNone(resp.mapping_status)


@unittest.skipUnless(_HAS_FASTAPI, _FASTAPI_SKIP_REASON)
class TestMapToResponseNotFound(unittest.TestCase):
    def test_clean_404_is_not_found_no_reason_needed(self):
        resp = _map_to_response(_clean_not_found())
        self.assertEqual(resp.entry_status, EntryStatus.NOT_FOUND)
        self.assertIsNone(resp.entry_status_reason)
        self.assertEqual(resp.accession, "Q99999")
        self.assertIsNone(resp.pdb_url)
        self.assertIsNone(resp.mapping_status)

    def test_not_found_is_distinct_from_error(self):
        """The whole point of NOT_FOUND vs ERROR existing as separate
        states: a client must be able to tell "AlphaFold DB has no entry"
        from "we don't know, the query failed" without string-sniffing."""
        not_found = _map_to_response(_clean_not_found())
        error = _map_to_response(_lookup_error())
        self.assertNotEqual(not_found.entry_status, error.entry_status)


@unittest.skipUnless(_HAS_FASTAPI, _FASTAPI_SKIP_REASON)
class TestMapToResponseFoundMapped(unittest.TestCase):
    def test_mapped_populates_all_five_ratified_fields(self):
        resp = _map_to_response(_found_mapped())
        self.assertEqual(resp.entry_status, EntryStatus.FOUND)
        self.assertIsNone(resp.entry_status_reason)
        self.assertEqual(resp.accession, "P04637")
        self.assertEqual(resp.pdb_url, "https://alphafold.ebi.ac.uk/files/AF-P04637-F1-model_v4.pdb")
        self.assertEqual(resp.model_version, "4")
        self.assertEqual(resp.mapping_status, MappingStatus.MAPPED)
        self.assertEqual(resp.mapped_residue, 175)
        self.assertEqual(resp.mapping_confidence_band, "very_high")
        self.assertIsNone(resp.mapping_unavailable_reason)

    def test_mapping_confidence_band_is_the_residue_band_not_the_mean(self):
        """Ratified reading: mapping_confidence_band is the AFFECTED
        RESIDUE's band (affected_residue_band), never the whole-protein
        mean_plddt_band -- construct a fixture where the two disagree and
        assert the residue-specific one wins."""
        raw = _found_mapped()
        raw["mean_plddt_band"] = "confident"  # deliberately different from affected_residue_band
        raw["affected_residue_band"] = "very_low"
        resp = _map_to_response(raw)
        self.assertEqual(resp.mapping_confidence_band, "very_low")


@unittest.skipUnless(_HAS_FASTAPI, _FASTAPI_SKIP_REASON)
class TestMapToResponseFoundNoPosition(unittest.TestCase):
    """No protein position was ever resolved upstream -- distinguished from
    NOT_MAPPABLE by `raw["protein_position"] is None`."""

    def test_no_position_case_is_no_position_absent_never_guessed(self):
        resp = _map_to_response(_found_not_mapped_no_position())
        self.assertEqual(resp.entry_status, EntryStatus.FOUND)
        self.assertEqual(resp.mapping_status, MappingStatus.NO_POSITION)
        self.assertIsNone(resp.mapped_residue)
        self.assertIsNone(resp.mapping_confidence_band)
        self.assertEqual(resp.mapping_unavailable_reason, "no protein position was resolved for this variant")
        # Entry-level facts are still populated -- they're true of the
        # protein regardless of which residue was asked about.
        self.assertIsNotNone(resp.pdb_url)
        self.assertIsNotNone(resp.model_version)


@unittest.skipUnless(_HAS_FASTAPI, _FASTAPI_SKIP_REASON)
class TestMapToResponseFoundNotMappable(unittest.TestCase):
    """A real protein position existed but the mapping gate rejected it on
    the merits -- distinguished from NO_POSITION by `raw["protein_position"]`
    being non-None (the rejected position, echoed unconditionally by
    provider.py::_build_annotation regardless of the gate's verdict)."""

    def test_out_of_range_case_is_not_mappable_distinct_from_no_position(self):
        resp = _map_to_response(_found_not_mapped_out_of_range())
        self.assertEqual(resp.entry_status, EntryStatus.FOUND)
        self.assertEqual(resp.mapping_status, MappingStatus.NOT_MAPPABLE)
        self.assertNotEqual(MappingStatus.NOT_MAPPABLE, MappingStatus.NO_POSITION)
        self.assertIsNone(resp.mapped_residue)
        self.assertIsNone(resp.mapping_confidence_band)
        self.assertIn("lies outside", resp.mapping_unavailable_reason)

    def test_mapped_residue_never_guessed_when_gate_rejects(self):
        """The one hard requirement from the dispatch: mapped_residue must
        be ABSENT, not a best-effort guess, whenever the gate did not
        approve it -- even though the raw dict's own `protein_position`
        field IS populated in the out-of-range case (the position that was
        rejected, and the very field NOT_MAPPABLE is derived from), it must
        never leak into `mapped_residue`."""
        raw = _found_not_mapped_out_of_range(position=5000)
        self.assertEqual(raw["protein_position"], 5000)  # sanity: the raw dict does carry it
        resp = _map_to_response(raw)
        self.assertEqual(resp.mapping_status, MappingStatus.NOT_MAPPABLE)
        self.assertIsNone(resp.mapped_residue)  # but the response must not


@unittest.skipUnless(_HAS_FASTAPI, _FASTAPI_SKIP_REASON)
class TestMapToResponseNeverRaises(unittest.TestCase):
    def test_missing_optional_keys_do_not_raise(self):
        """`_map_to_response` must tolerate a minimal found=True dict
        missing keys it doesn't strictly need for a given branch (e.g. a
        future AlphaFoldAnnotation field addition) rather than KeyError."""
        minimal_not_found = {"skipped": False, "found": False, "accession": "X", "error": None}
        resp = _map_to_response(minimal_not_found)
        self.assertEqual(resp.entry_status, EntryStatus.NOT_FOUND)


# ─── Route-level tests: prove the wiring, not the mapping logic again ─────────


class _FakeLookup:
    def __init__(self, fixed_result: Dict[str, Any]):
        self.fixed_result = fixed_result
        self.calls = []

    def query_variant(self, uniprot_result: Optional[Dict[str, Any]], protein_position: Optional[int] = None):
        self.calls.append((uniprot_result, protein_position))
        return dict(self.fixed_result)


@unittest.skipUnless(_HAS_FASTAPI, _FASTAPI_SKIP_REASON)
class TestStructureEndpointRoute(unittest.TestCase):
    def _client(self, fake_lookup: "_FakeLookup"):
        from fastapi.testclient import TestClient

        import api.main as api_main

        api_main.app.dependency_overrides[api_main.get_alphafold_lookup] = lambda: fake_lookup
        self.addCleanup(api_main.app.dependency_overrides.clear)
        return TestClient(api_main.app, raise_server_exceptions=False)

    def test_found_mapped_round_trips_through_http(self):
        fake = _FakeLookup(_found_mapped(accession="P04637", position=175))
        client = self._client(fake)

        resp = client.get("/structures/P04637", params={"protein_position": 175})

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["entry_status"], "found")
        self.assertEqual(body["mapping_status"], "mapped")
        self.assertEqual(body["mapped_residue"], 175)
        self.assertEqual(body["accession"], "P04637")
        self.assertEqual(fake.calls, [({"accession": "P04637"}, 175)])

    def test_omitted_protein_position_is_passed_through_as_none(self):
        fake = _FakeLookup(_found_not_mapped_no_position(accession="P04637"))
        client = self._client(fake)

        resp = client.get("/structures/P04637")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(fake.calls, [({"accession": "P04637"}, None)])
        body = resp.json()
        self.assertEqual(body["mapping_status"], "no_position")
        self.assertIsNone(body["mapped_residue"])

    def test_not_found_returns_200_with_status_field_not_an_http_error(self):
        """A missing AlphaFold entry is a normal, expected outcome for a
        real accession -- not an HTTP-layer error. The status lives in the
        body's entry_status field, not the HTTP status code."""
        fake = _FakeLookup(_clean_not_found(accession="Q99999"))
        client = self._client(fake)

        resp = client.get("/structures/Q99999")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["entry_status"], "not_found")


if __name__ == "__main__":
    unittest.main()
