# REVIEW_MANIFEST — 72aec5a landing verification (2026-08-24)

Tracked changes landed in 72aec5a:
- eper/DATA_SOURCE_LICENSE_AUDIT.md
- geper/pipeline/acmg_rules.py
- geper/pipeline/confidence_engine.py
- geper/pipeline/interpretation.py
- geper/pipeline/orchestrator.py
- geper/pipeline/prioritization_engine.py
- geper/pipeline/pvs1/utils.py
- geper/report/report_generator.py
- geper/verify_environment.py
- kim_pipeline/pipeline/acmg/classifier.py

Untracked (new files, not in index):
- geper/tests/test_gnomad_lookup_failure_not_absent.py [FLAG: untracked, won't appear in git diff]
- kim_pipeline/tests/test_pp3_bp4_spliceai_removal_invariance.py [FLAG: untracked, won't appear in git diff]

Note: geper/tests/test_gnomad_lookup_failure_not_absent.py is NEW, untracked, and critical for the gnomAD sentinel defect fix. Reviewers working from git diff will not see this test, only the 10 production guard-site changes. The test must be reviewed separately to understand what the guard changes defend against.
