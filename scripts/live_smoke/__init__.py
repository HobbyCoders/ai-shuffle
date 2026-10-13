"""Scripted live smoke for real workers, and the release gate that reads it.

Run with ``python -m scripts.live_smoke`` from the repo root; see
``docs/internal/live-smoke.md``. Only ``client`` needs third-party packages
(httpx, websockets); ``gate``, ``results`` and ``scenarios`` are stdlib-only so
the release helper can import them anywhere.
"""
