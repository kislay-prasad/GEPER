"""
Centralized logging configuration for GEPER.

All modules obtain their logger via `get_logger(__name__)` so that log
formatting, level, and destination (console + rotating file) stay
consistent across the entire pipeline.
"""

import logging
import os
import sys
from logging.handlers import RotatingFileHandler

_LOG_DIR = os.environ.get("GEPER_LOG_DIR", os.path.join(os.getcwd(), "logs"))
_LOG_FILE = os.path.join(_LOG_DIR, "geper.log")
_LOG_LEVEL = os.environ.get("GEPER_LOG_LEVEL", "INFO").upper()

_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"

_initialized = False


def _initialize_root_logger() -> None:
    """Configure the root GEPER logger exactly once (idempotent)."""
    global _initialized
    if _initialized:
        return

    os.makedirs(_LOG_DIR, exist_ok=True)

    root = logging.getLogger("geper")
    root.setLevel(_LOG_LEVEL)
    root.propagate = False

    formatter = logging.Formatter(_FORMAT, datefmt=_DATEFMT)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    console_handler.setLevel(_LOG_LEVEL)

    try:
        file_handler = RotatingFileHandler(_LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
        file_handler.setFormatter(formatter)
        file_handler.setLevel(_LOG_LEVEL)
        root.addHandler(file_handler)
    except OSError:
        # File system may be read-only (e.g. some Colab / serverless
        # environments) -- degrade gracefully to console-only logging.
        pass

    root.addHandler(console_handler)
    _initialized = True


def get_logger(name: str) -> logging.Logger:
    """Return a namespaced logger nested under the 'geper' root logger."""
    _initialize_root_logger()
    if not name.startswith("geper"):
        name = f"geper.{name}"
    return logging.getLogger(name)
