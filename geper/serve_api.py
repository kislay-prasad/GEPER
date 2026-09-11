#!/usr/bin/env python3
"""
serve_api.py
─────────────
Launch the Bij AI variant-interpretation component (GEPER) structures API server.

Usage:
    python serve_api.py
    python serve_api.py --host 0.0.0.0 --port 8001
    python serve_api.py --reload   # development hot-reload

Environment overrides:
    GEPER_STRUCTURES_API_HOST       Bind host (default: 127.0.0.1 -- loopback-only;
                                     pass --host 0.0.0.0 or set this var explicitly
                                     to bind every interface)
    GEPER_STRUCTURES_API_PORT       Bind port (default: 8001)
    GEPER_STRUCTURES_API_LOG_LEVEL  Logging level (default: info)

Port/env-var naming, chosen deliberately rather than inherited from
kim_pipeline/serve_api.py: that script defaults to port 8000 via
GEPER_API_HOST/GEPER_API_PORT. Reusing those exact names here would mean a
single GEPER_API_PORT setting silently controlled whichever of the two
servers happened to read it last if both processes ever shared one
environment (a shell, a compose `environment:` block, a `.env` file) --
and defaulting to the same port 8000 would collide outright if both were
ever run on one host. This script uses its own GEPER_STRUCTURES_API_*
names (matching this codebase's existing per-subsystem env-var prefixing
convention -- GEPER_ALPHAFOLD_*, GEPER_BLAST_*, GEPER_CLINGEN_*, etc. in
config.py) and its own default port, 8001, so the two servers do not
collide by default and cannot cross-read each other's setting by accident.
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
    from component_identity import COMPONENT_NAME

    parser = argparse.ArgumentParser(description=f"{COMPONENT_NAME} structures API server")
    # Default flipped from 0.0.0.0 to 127.0.0.1 (loopback-only) -- same fix
    # and same reason as kim_pipeline/serve_api.py's own default flip: this
    # script is launched with no --host override by anyone following a
    # "run it locally" recipe, and was binding every network interface
    # while auth is off by default. This flip makes it local by default
    # rather than by luck; pass --host 0.0.0.0 explicitly to bind broadly.
    parser.add_argument("--host", default=os.getenv("GEPER_STRUCTURES_API_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("GEPER_STRUCTURES_API_PORT", "8001")))
    parser.add_argument("--reload", action="store_true", help="Enable hot-reload (dev only)")
    parser.add_argument(
        "--log-level",
        default=os.getenv("GEPER_STRUCTURES_API_LOG_LEVEL", "info").lower(),
        choices=["debug", "info", "warning", "error"],
    )
    return parser


def _warn_if_bind_all_without_auth(host: str) -> None:
    """Runtime warning at bind time -- see kim_pipeline/serve_api.py's
    identical helper for the full reasoning (conditioned on the pair:
    bind-all AND auth currently off, not either alone)."""
    if _is_loopback(host):
        return
    import importlib

    # importlib.import_module (not `import api.main as x`) -- see
    # kim_pipeline/serve_api.py's identical helper for why: the `as` form
    # resolves via an attribute on the parent package, invisible to a
    # sys.modules patch in tests.
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

    from component_identity import COMPONENT_NAME

    print(f"Starting the {COMPONENT_NAME} Structures API on http://{args.host}:{args.port}")
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
