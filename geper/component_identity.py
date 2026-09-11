"""
geper/component_identity.py
---------------------------
What this tree calls itself -- the ONE definition, the geper-side
equivalent of kim_pipeline/pipeline/reporting/component_identity.py.

Human's rule (2026-09-11): "The defect was never the words 'Bij AI' -- it
was a component claiming to be the whole product." This tree is the
VARIANT-INTERPRETATION component of Bij AI, not the product. Every
code-emitted string that says which component is running or acting (CLI
help, API title/summaries, startup and banner lines, error messages, logs,
verify_environment) is built from these constants. Import-free on purpose:
verify_environment.py is imported before the interpreter-version guard.

Not used for clinician-facing report text: that wording is ruled separately
(report/summary.py::SCOPE_LINE; the conflict/ACMG/explainability/priority
report strings are awaiting their own ruling).
"""

COMPONENT_NAME = "Bij AI variant-interpretation component (GEPER)"
SHORT_NAME = "GEPER"
