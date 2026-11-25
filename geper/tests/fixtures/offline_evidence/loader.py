"""
Loads the offline-evidence fixture produced by `extract_fixture.py`,
guarded by the same schema `pipeline/stage_schemas.py::build_raw_evidence_bundle`
validates the real pipeline's evidence-to-report boundary against.

WHY THE GUARD, NOT JUST A LOAD
--------------------------------
A recorded fixture drifts silently if the stage dict shapes it captured
stop matching what production actually emits (a renamed/removed key
upstream, without the fixture being regenerated) -- the residual risk
identified when this fixture mechanism was designed (see round 11's
report and this directory's README). `pipeline/stage_schemas.py`
already has a validated schema for 11 of this fixture's evidence dicts
(`RawEvidenceBundle` -- clinvar/dbsnp/protein/blast/alphamissense/
mmsplice/gnomad/clingen/uniprot/interpro/alphafold; see that module's
own docstring for exactly why those 11 and not the rest). `load_fixture`
below runs every variant's evidence through that same
`build_raw_evidence_bundle` boundary function before handing it back --
if a captured dict no longer matches the shape `RawEvidenceBundle`
expects, this raises immediately and loudly (`FixtureSchemaError`),
never silently degrades to a `{"skipped": True}`-shaped stand-in the
way `build_raw_evidence_bundle`'s own production callers are required
to tolerate (production must keep running on a bad evidence dict; a
test fixture must not).

Production's `build_raw_evidence_bundle` deliberately never raises --
it returns `(None, error_message)` and callers are documented to fall
back to the original unvalidated dict, because a live clinical run must
never crash over a validation-layer bug (see that module's docstring).
That graceful-degradation contract is correct for production and wrong
for this loader: a fixture that fails validation without anyone
noticing is exactly the drift this guard exists to catch, so here a
`(None, error_message)` result is turned into a hard failure instead of
being swallowed.

SCOPE OF THE GUARD -- NOT ALL EVIDENCE DICTS ARE COVERED
------------------------------------------------------------
`RawEvidenceBundle` covers 11 of this fixture's ~19 evidence dicts.
The rest (conservation, transcript, clinvar_codon, hpo, orphanet,
functional_evidence, rna, ensemble, spliceformer, splicebert,
normalization) have no existing Pydantic schema in `stage_schemas.py`
to validate against -- that module's own docstring documents this as a
deliberate scope decision ("Provider-internal shapes ... are NOT typed
here"), not an oversight this loader can silently work around. Those
fields are loaded as-is, unvalidated, exactly as production itself
treats them. If one of them drifts, this loader will not catch it --
only re-running `extract_fixture.py` against a fresh real run will.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, cast

from pipeline.stage_schemas import build_raw_evidence_bundle

_FIXTURE_DIR = os.path.dirname(os.path.abspath(__file__))


# The build the recorded run used: `nuclear_test_with_mt.vcf` declares `##reference=GRCh38`. The PS1/PM5 codon
# lookup now states its build in every result (ClinVar audit C3) because PS1/PM5 judge "is this ClinVar record the
# variant itself?" in that build; a fixture captured before that key existed is completed with it here, at load
# time, so the recorded JSON stays exactly as extracted. Without it the codon result would read as "assembly
# unresolved" (by design: never silently GRCh38) and PM5/PS1 would be withheld, which the recorded run did not do.
_RECORDED_RUN_ASSEMBLY = "GRCh38"


class FixtureSchemaError(RuntimeError):
    """Raised when a captured evidence dict no longer matches the
    schema `build_raw_evidence_bundle` validates production's own
    evidence-to-report boundary against -- meaning this fixture is
    stale relative to the current dict shapes and must be regenerated
    (see this directory's README.md)."""


def load_fixture(filename: str) -> Dict[str, Dict[str, Any]]:
    """
    Returns `{label: {"evidence": {...kwargs for interpret()/
    build_variant_result()...}, "expected": {...}}}`.

    Raises `FixtureSchemaError` immediately if any variant's captured
    evidence no longer validates against `RawEvidenceBundle` -- see
    this module's docstring for why that is the correct behavior here
    (unlike production's own graceful-degradation policy at the same
    boundary).
    """
    path = filename if os.path.isabs(filename) else os.path.join(_FIXTURE_DIR, filename)
    with open(path, "r", encoding="utf-8") as fh:
        document = json.load(fh)

    variants = document.get("variants", {})
    for label, entry in variants.items():
        evidence = entry["evidence"]
        codon_result = evidence.get("clinvar_codon_result")
        if isinstance(codon_result, dict) and codon_result.get("matches") and "assembly" not in codon_result:
            codon_result["assembly"] = _RECORDED_RUN_ASSEMBLY
        bundle, error = build_raw_evidence_bundle(
            variant_ref=label,
            clinvar_result=evidence.get("clinvar_result"),
            dbsnp_result=evidence.get("dbsnp_result"),
            protein_result=evidence.get("protein_result"),
            blast_result=evidence.get("blast_result"),
            alphamissense_result=evidence.get("alphamissense_result"),
            mmsplice_result=evidence.get("mmsplice_result"),
            gnomad_result=evidence.get("gnomad_result"),
            clingen_result=evidence.get("clingen_result"),
            uniprot_result=evidence.get("uniprot_result"),
            interpro_result=evidence.get("interpro_result"),
            alphafold_result=evidence.get("alphafold_result"),
        )
        if bundle is None:
            raise FixtureSchemaError(
                f"'{filename}' variant '{label}' failed RawEvidenceBundle validation "
                f"(this fixture is stale relative to the current stage-dict schema; "
                f"regenerate it per this directory's README.md): {error}"
            )

    return cast(Dict[str, Dict[str, Any]], variants)
