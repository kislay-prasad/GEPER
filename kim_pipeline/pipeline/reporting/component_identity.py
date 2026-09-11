"""
pipeline/reporting/component_identity.py
─────────────────────────────────────────
What this tree calls itself -- the ONE definition, shared by the reports
(pipeline/reporting/stage.py re-exports PIPELINE_VERSION for the JSON, HTML
and PDF) and the CLI (main.py: --version, help description, subcommand help).

Ruled by the human 2026-09-11 (Option B): this tree is a COMPONENT of Bij AI,
not the whole product -- "the version field must name the same thing the
scope line does." Kept in a module with no imports so `main.py --version`
and `--help` can read it without pulling in the reporting stage's
dependencies.
"""

COMPONENT_NAME = "Bij AI sequencing-analysis component"
PIPELINE_VERSION = f"{COMPONENT_NAME} v8"
