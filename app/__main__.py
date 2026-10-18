"""Console entry point."""

from __future__ import annotations

import argparse
import os


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the PyWeKnora server")
    parser.add_argument("--host", default=os.getenv("WEKNORA_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("WEKNORA_PORT", "8080")))
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()
    import uvicorn

    uvicorn.run("app.main:app", host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
