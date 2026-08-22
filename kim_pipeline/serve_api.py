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


def main() -> None:
    parser = argparse.ArgumentParser(description="Bij AI genomics pipeline API server")
    parser.add_argument("--host", default=os.getenv("GEPER_API_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("GEPER_API_PORT", "8000")))
    parser.add_argument("--reload", action="store_true", help="Enable hot-reload (dev only)")
    parser.add_argument(
        "--log-level",
        default=os.getenv("GEPER_LOG_LEVEL", "info").lower(),
        choices=["debug", "info", "warning", "error"],
    )
    args = parser.parse_args()

    try:
        import uvicorn
    except ImportError:
        print("ERROR: uvicorn not installed. Run: pip install uvicorn[standard]", file=sys.stderr)
        sys.exit(1)

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
