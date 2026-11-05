"""Normal app.main with a dev-only observer and bounded graceful shutdown."""
from __future__ import annotations

import runpy
import sys
from contextlib import contextmanager
from functools import partial
from pathlib import Path


@contextmanager
def bounded_shutdown():
    """Dev-only: cancel lingering streams after 5s, then run normal lifespan."""
    import uvicorn

    original = uvicorn.Config
    uvicorn.Config = partial(original, timeout_graceful_shutdown=5)
    try:
        yield
    finally:
        uvicorn.Config = original


def main():
    from app import serve
    from scripts.devloop.observer import ActivityObserver

    receipt = Path(sys.argv[1])
    original = serve.run

    def run(app, settings):
        with bounded_shutdown():
            return original(ActivityObserver(app, receipt), settings)

    serve.run = run
    # run_module retains app.main's __main__ branch/logger and its exact normal
    # app.serve startup, sockets and lifespan. Only the dev observer and bounded
    # graceful shutdown differ. No app/ changes or awake traffic forwarding.
    sys.argv = [sys.argv[0]]
    try:
        runpy.run_module("app.main", run_name="__main__", alter_sys=True)
    finally:
        serve.run = original


if __name__ == "__main__":
    main()
