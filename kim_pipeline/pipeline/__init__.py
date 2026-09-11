# pipeline/__init__.py
# Top-level package of the Bij AI sequencing-analysis component (Kim).
#
# Kim's version has ONE source, pipeline/reporting/component_identity.py
# (human-approved 2026-09-11); these names read it. They used to be four
# hand-typed twelve-dot-zero-dot-zero literals under a comment naming this
# the top-level package for GEPER at version twelve -- the platform's
# inherited numbering, typed in the first commit (cb79edc) and never read by
# any code. (Spelled out here so the version-literal scan in
# tests/test_defect_regression.py::TestVersionSync stays exact.)
#
# CHECKPOINT_VERSION and REPORT_VERSION were REMOVED rather than re-pointed:
# nothing read them, no checkpoint.json or report stores a format version,
# and resume compatibility is checked only against
# checkpoint["reference_versions"] (orchestration/runner.py). A constant
# named for a format check that does not exist invites trusting one.
from pipeline.reporting.component_identity import PIPELINE_VERSION, VERSION as __version__

__all__ = ["PIPELINE_VERSION", "__version__"]
