"""
pipeline/reporting/component_identity.py
─────────────────────────────────────────
What this tree calls itself -- the ONE definition, shared by the reports
(pipeline/reporting/stage.py re-exports PIPELINE_VERSION for the JSON, HTML
and PDF; clinical_sections.py re-exports SCOPE_LINE) and every non-report
surface that names this component: the CLIs (main.py, the runner, serve_api.py),
the API's OpenAPI identity, and the environment/dependency checks.

Ruled by the human 2026-09-11 (Option B): this tree is a COMPONENT of Bij AI,
not the whole product -- "the version field must name the same thing the
scope line does." Kept in a module with no imports so `main.py --version`
and `--help` can read it without pulling in the reporting stage's
dependencies.
"""

COMPONENT_NAME = "Bij AI sequencing-analysis component"

# Kim's version -- the ONLY place the number is written (human-approved
# 2026-09-11: "make 8 Kim's single version source"). A plain string literal on
# purpose: setuptools reads it statically for the package metadata
# (pyproject.toml [tool.setuptools.dynamic]) without importing anything.
# Every other surface derives from it: the label below (reports, CLI), the
# API's OpenAPI version and /health, and pipeline.__version__.
VERSION = "8.0.0"
PIPELINE_VERSION = f"{COMPONENT_NAME} v{VERSION.split('.')[0]}"

# Ratified wording (2026-09-11, Option B): what this tree is, said on every
# report (HTML via stage.py, PDF via pdf_report.py) and, verbatim, in
# main.py --help. Clinician-facing text -- change only with human sign-off.
SCOPE_LINE = (
    "Sequencing analysis from FASTQ — produced by the Bij AI sequencing-analysis "
    "component (Kim): QC, alignment and variant calling, with ACMG classification "
    "of the variants called here."
)
