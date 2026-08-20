# Data Provenance — kim_pipeline

kim_pipeline had no data-provenance/license-audit document of its own
before this entry. This file starts with the one external source added
after OMIM's retirement (2026-08-20); it is not a full audit of every
data source kim_pipeline already used (ClinVar, gnomAD, VEP, etc.) —
only new sources should be added here going forward, or this file
expanded into a full audit as a separate piece of work.

## ClinGen Dosage Sensitivity (`pipeline/clingen/`)

**Added**: 2026-08-20, as the LoF-intolerance fallback for
`pipeline/constraint/lookup.py::GnomadConstraintLookup.is_lof_intolerant()`
when a gene has no gnomAD pLI/LOEUF coverage — the role OMIM's
phenotype-text heuristic (`is_lof_intolerant()`, removed the same day)
used to serve, replaced with a real ClinGen curation score instead of a
regex over free-text phenotype descriptions.

**Source**: ClinGen's own published Dosage Sensitivity curation
download (`https://search.clinicalgenome.org/kb/dosage/download`, also
mirrored at `ftp.clinicalgenome.org`). Primary path is a
deployer-provisioned local flat-file copy (`clingen.dosage_sensitivity_path`
in `config/default.yaml`); an HTTP fallback exists but is **disabled by
default** (`clingen.api_enabled: false`) because its request/response
shape has not been exercised against a live `clinicalgenome.org`
response in this development environment (no network route to that
host was available) — see `pipeline/clingen/lookup.py`'s module
docstring. Enable it only after a live spot-check.

**License**: ClinGen's curated content (gene-disease validity, dosage
sensitivity, and its Evidence Repository) is published under **CC0 1.0
Universal — Public Domain Dedication**
(`https://clinicalgenome.org/docs/terms-of-use/`, quoted verbatim:
"All curated content published by ClinGen is available free of
restriction under the CC0 1.0 Universal (CC0 1.0) Public Domain
Dedication." Attribution with access date is requested, not required).
This was independently verified for the sibling `geper/` pipeline in
`geper/DATA_SOURCE_LICENSE_AUDIT.md` (row: "ClinGen (gene validity,
dosage sensitivity, ERepo)"). That verification is not re-used here
unchecked: CC0 is an unconditional public-domain dedication with no
field-of-use, deployment-context, or per-consumer restriction, so its
terms cover kim_pipeline's separate commercial deployment exactly as
they cover geper/'s — this is a property of the license itself, not an
assumption carried over from another project's approval. **No
commercial-use restriction, no field-of-use restriction, attribution
optional.**

**Not shared code with `geper/pipeline/clingen/`**: that module is
built for gene *resolution* (Ensembl overlap + PVS1 transcript + MANE
disambiguation to map a variant to a gene symbol) and is threaded
through geper-specific globals (`config.py::CONFIG`, `utils.logger`,
`utils.service_health`). kim_pipeline already has gene symbols from VEP
annotation upstream and only needed a gene-symbol → dosage-score
lookup, so `pipeline/clingen/lookup.py` here is a new, lightweight,
kim_pipeline-native module following this codebase's own cfg-dict +
local-file-then-API pattern (matching `pipeline/constraint/lookup.py`
and the retired `pipeline/omim/lookup.py`). It ports only ClinGen's
published column-name/scale knowledge from `geper/`, not any of
`geper/`'s application code — a direct import was ruled out because the
two subprojects each have their own separately-packaged top-level
`pipeline` package (see `kim_pipeline/pyproject.toml` vs
`geper/pipeline/`); placing both on `sys.path` at once would collide on
that name.

**Not currently live-verified against ClinGen's real local-dataset file
format** in this session (no such file was fetched/parsed against a live
download here) — the preamble-tolerant parsing logic was ported from
`geper/pipeline/clingen/provider.py::_clingen_export_rows`, which
*was* live-verified there against real downloaded files (see
`geper/DATA_PROVENANCE.md`'s `clingen/` row: "Live-verified in this
session... both functions called fresh, confirmed real files
downloaded"). Recommend a follow-up live download + parse check against
kim_pipeline's own configured path before production reliance, same
caution as any newly wired source.
