"""
pytest configuration for kim_pipeline.

Ensures the repo root is on sys.path so shared/ package is importable
when running tests from the kim_pipeline/ directory.
"""

import sys
from pathlib import Path

# Add repo root to sys.path so shared/ package is importable
_repo_root = Path(__file__).resolve().parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))
