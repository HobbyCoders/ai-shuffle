"""Record which repository functions each backend test file runs.

Loaded only by CI Backend's full run (``-p tests._test_map`` with
``BACKEND_TEST_MAP_DIR`` set). Each pytest process (every xdist worker)
writes one JSON file; ``scripts/ci/backend_test_map.py merge`` combines them
into the map pull requests select tests from.

Uses ``sys.monitoring`` (Python 3.12+): every function start fires once per
test file and is then disabled until the next file, so the cost is roughly
one callback per distinct function per test file.

Session- and package-scoped fixtures run once for many test files, so code
they run is recorded as shared: a pull request that changes it runs the full
suite.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SHARED = "<shared>"
TRACKED = ("app/", "fluid/fluid/", "tests/", "scripts/", "docker/", "run_local.py")
TOOL_ID = 3  # sys.monitoring ids 3 and 4 are unassigned; 1 is coverage.py's


def qualname(code) -> str:
    # Nested functions, lambdas and generator expressions belong to the
    # function that defines them: a change to them changes that function.
    return code.co_qualname.split(".<locals>", 1)[0]


class Recorder:
    def __init__(self, out_dir: str):
        self.out_dir = Path(out_dir)
        self.prefix = os.path.normcase(str(ROOT)) + os.sep
        self.context: str | None = None
        self.hits: dict[str, set[tuple[str, str]]] = {}
        self.paths: dict[str, str | None] = {}

    def start(self) -> None:
        monitoring = sys.monitoring
        monitoring.use_tool_id(TOOL_ID, "backend-test-map")
        monitoring.register_callback(TOOL_ID, monitoring.events.PY_START, self.on_start)
        monitoring.set_events(TOOL_ID, monitoring.events.PY_START)

    def stop(self) -> None:
        monitoring = sys.monitoring
        monitoring.set_events(TOOL_ID, 0)
        monitoring.register_callback(TOOL_ID, monitoring.events.PY_START, None)
        monitoring.free_tool_id(TOOL_ID)

    def on_start(self, code, _offset):
        filename = code.co_filename
        rel = self.paths.get(filename, "")
        if rel == "":
            rel = None
            normalized = os.path.normcase(filename)
            if normalized.startswith(self.prefix):
                candidate = filename[len(self.prefix):].replace(os.sep, "/")
                if candidate.startswith(TRACKED):
                    rel = candidate
            self.paths[filename] = rel
        if rel is not None and self.context is not None:
            self.hits.setdefault(self.context, set()).add((rel, qualname(code)))
        return sys.monitoring.DISABLE

    def switch(self, context: str | None) -> None:
        if context != self.context:
            self.context = context
            sys.monitoring.restart_events()

    def write(self) -> None:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
        data = {"worker": worker,
                "hits": {context: sorted(hits) for context, hits in self.hits.items()}}
        (self.out_dir / f"hits-{worker}-{os.getpid()}.json").write_text(json.dumps(data), encoding="utf-8")


_recorder: Recorder | None = None


def _test_file(nodeid: str) -> str:
    return nodeid.split("::", 1)[0].replace("\\", "/")


def pytest_configure(config):
    global _recorder
    out_dir = os.environ.get("BACKEND_TEST_MAP_DIR")
    if out_dir and _recorder is None and sys.version_info >= (3, 12):
        _recorder = Recorder(out_dir)
        _recorder.start()


def pytest_collectstart(collector):
    # Code a test module runs while it is imported belongs to that module.
    if _recorder is not None and isinstance(collector, pytest.Module):
        _recorder.switch(_test_file(collector.nodeid))


def pytest_runtest_logstart(nodeid, location):
    if _recorder is not None:
        _recorder.switch(_test_file(nodeid))


@pytest.hookimpl(hookwrapper=True)
def pytest_fixture_setup(fixturedef, request):
    if _recorder is None or fixturedef.scope not in {"session", "package"}:
        yield
        return
    previous = _recorder.context
    _recorder.switch(SHARED)
    try:
        yield
    finally:
        _recorder.switch(previous)


def pytest_unconfigure(config):
    global _recorder
    if _recorder is not None:
        _recorder.stop()
        _recorder.write()
        _recorder = None
