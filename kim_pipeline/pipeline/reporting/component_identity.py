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
PIPELINE_VERSION = f"{COMPONENT_NAME} v8"

# Ratified wording (2026-09-11, Option B): what this tree is, said on every
# report (HTML via stage.py, PDF via pdf_report.py) and, verbatim, in
# main.py --help. Clinician-facing text -- change only with human sign-off.
SCOPE_LINE = (
    "Sequencing analysis from FASTQ — produced by the Bij AI sequencing-analysis "
    "component (Kim): QC, alignment and variant calling, with ACMG classification "
    "of the variants called here."
)
