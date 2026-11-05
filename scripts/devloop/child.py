"""Run app.main as __main__, adding only devloop's observational ASGI wrapper."""
from __future__ import annotations

import runpy
import sys
from pathlib import Path


def main():
    from app import serve
    from scripts.devloop.observer import ActivityObserver

    receipt = Path(sys.argv[1])
    original = serve.run

    def run(app, settings):
        return original(ActivityObserver(app, receipt), settings)

    serve.run = run
    # run_module retains app.main's __main__ branch/logger and its exact normal
    # app.serve startup, sockets, lifespan and uvicorn settings. No app/ changes.
    sys.argv = [sys.argv[0]]
    runpy.run_module("app.main", run_name="__main__", alter_sys=True)


if __name__ == "__main__":
    main()
