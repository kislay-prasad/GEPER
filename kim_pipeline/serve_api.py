#!/usr/bin/env python3
"""
serve_api.py
─────────────
Launch the GEPER FastAPI server.

Usage:
    python serve_api.py
    python serve_api.py --host 0.0.0.0 --port 8000
    python serve_api.py --reload   # development hot-reload

Environment overrides:
    GEPER_CONFIG_PATH   Path to YAML config (default: config/default.yaml)
    GEPER_OUTPUT_DIR    Root output directory (default: /tmp/geper_runs)
    GEPER_UPLOAD_DIR    Upload directory (default: /tmp/geper_uploads)
    GEPER_LOG_LEVEL     Logging level (default: INFO)
"""

import argparse
import os
import sys
from pathlib import Path

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).resolve().parent))


_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _is_loopback(host: str) -> bool:
    return host in _LOOPBACK_HOSTS


def build_parser() -> argparse.ArgumentParser:
    """Split out from main() so the parsed default is directly readable
    (`build_parser().parse_args([]).host`) without importing uvicorn or
    binding a port -- see the loopback-default fix this function was
    extracted for."""
    parser = argparse.ArgumentParser(description="Bij AI genomics pipeline API server")
    # Default flipped from 0.0.0.0 to 127.0.0.1 (loopback-only): the
    # documented "Development (keyless local)" recipe in
    # kim_pipeline/docs/INSTALL.md runs this with no --host override, and
    # was binding every network interface while auth is off by default --
    # "keyless local" was not actually local unless the operator also
    # happened to be on a machine with no reachable interface. This flip
    # makes it local by default rather than by luck; pass --host 0.0.0.0
    # explicitly to bind broadly (a real deployment, not this script's
    # bare default).
    parser.add_argument("--host", default=os.getenv("GEPER_API_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("GEPER_API_PORT", "8000")))
    parser.add_argument("--reload", action="store_true", help="Enable hot-reload (dev only)")
    parser.add_argument(
        "--log-level",
        default=os.getenv("GEPER_LOG_LEVEL", "info").lower(),
        choices=["debug", "info", "warning", "error"],
    )
    return parser


def _warn_if_bind_all_without_auth(host: str) -> None:
    """Runtime warning at bind time, not only in a doc the operator may
    have read past. Deliberately conditioned on the PAIR, not either half:
    `--host 0.0.0.0` with real GEPER_API_KEYS set is a normal deployment
    and must not warn (a warning that fires on the normal case teaches
    people to ignore it) -- only bind-all *combined with* auth currently
    off is the dangerous case this exists to name.

    Imports api.main to read its own `_API_KEYS`/gate state -- the same
    module uvicorn.run("api.main:app", ...) below would import anyway, so
    this adds no new import, just runs it a moment earlier. If api.main's
    own FIX #3 gate is going to sys.exit(1) (no keys, no CORS origins, no
    GEPER_DEV_INSECURE=1), it does so here, identically to doing so inside
    uvicorn's lazy import.
    """
    if _is_loopback(host):
        return
    import importlib

    # importlib.import_module (not `import api.main as x`) so this reads
    # sys.modules["api.main"] directly -- the `as` form instead resolves
    # via an attribute on the already-imported parent package, which is
    # invisible to a `sys.modules` patch and made an earlier version of
    # this function's own test unreliable across test order.
    _api_app = importlib.import_module("api.main")

    if _api_app._API_KEYS is None:
        print(
            f"WARNING: binding to {host} (all interfaces) with authentication "
            "disabled (GEPER_API_KEYS is not set). This API will be reachable, "
            "unauthenticated, from any host that can route to this machine's "
            "network interfaces -- not just this machine. Set GEPER_API_KEYS "
            "before binding broadly, or drop --host to keep the default "
            "(127.0.0.1, local-only).",
            file=sys.stderr,
        )


def main() -> None:
    args = build_parser().parse_args()

    try:
        import uvicorn
    except ImportError:
        print("ERROR: uvicorn not installed. Run: pip install uvicorn[standard]", file=sys.stderr)
        sys.exit(1)

    _warn_if_bind_all_without_auth(args.host)

    print(f"Starting Bij AI API on http://{args.host}:{args.port}")
    print(f"Swagger UI: http://{args.host}:{args.port}/docs")
    print(f"OpenAPI JSON: http://{args.host}:{args.port}/openapi.json")

    uvicorn.run(
        "api.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level=args.log_level,
    )


if __name__ == "__main__":
    main()
