#!/usr/bin/env python3
"""
serve_api.py
─────────────
Launch the Bij AI structures API server.

Usage:
    python serve_api.py
    python serve_api.py --host 0.0.0.0 --port 8001
    python serve_api.py --reload   # development hot-reload

Environment overrides:
    GEPER_STRUCTURES_API_HOST       Bind host (default: 0.0.0.0)
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Bij AI structures API server")
    parser.add_argument("--host", default=os.getenv("GEPER_STRUCTURES_API_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("GEPER_STRUCTURES_API_PORT", "8001")))
    parser.add_argument("--reload", action="store_true", help="Enable hot-reload (dev only)")
    parser.add_argument(
        "--log-level",
        default=os.getenv("GEPER_STRUCTURES_API_LOG_LEVEL", "info").lower(),
        choices=["debug", "info", "warning", "error"],
    )
    args = parser.parse_args()

    try:
        import uvicorn
    except ImportError:
        print("ERROR: uvicorn not installed. Run: pip install uvicorn[standard]", file=sys.stderr)
        sys.exit(1)

    print(f"Starting Bij AI Structures API on http://{args.host}:{args.port}")
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
