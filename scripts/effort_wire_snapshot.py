"""Offline producer-path request snapshots (no provider calls or credentials).

Run from the repository root with the development/test dependencies installed::

    PYTHONPATH=fluid python -m scripts.effort_wire_snapshot --output /tmp/effort-before.json
    # Make a tuning change, run again with --output /tmp/effort-after.json, then:
    diff -u /tmp/effort-before.json /tmp/effort-after.json

Runs only tests/core/test_effort_wire.py and test_effort_inheritance_wire.py.
Real session/profile option building, child dispatch, adapters, loops and
serializers produce HTTP bodies against MockTransport; the CLI route records
the real argv builder's effort flag without spawning Claude.
The JSON is sorted, contains synthetic tuning parameters only, and is written
only after ALL route assertions pass. No timings, credentials, paths or prompts.

This measures request configuration, NOT task quality, token use or latency.
It cannot justify changing a default. Use scripts/effort_benchmark.py for an
explicitly approved outcome experiment; that separate live script is not run
by this harness or CI. Vendor presets in local_settings are explicit opt-ins,
not a claim of a measured win or automatic changes to existing sessions.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


class Capture:
    def __init__(self):
        self.records = []

    def pytest_runtest_logreport(self, report):
        if report.when == "call" and report.passed:
            for key, value in report.user_properties:
                if key == "wire":
                    self.records.append({"case": report.nodeid, **value})


def snapshot(records):
    return json.dumps({"version": 1, "measurement": "request-parameters-only",
                       "requests": sorted(records, key=lambda r: r["case"])},
                      sort_keys=True, indent=2, allow_nan=False) + "\n"


def main(argv=None):
    import pytest

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    capture = Capture()
    root = Path(__file__).resolve().parents[1] / "tests" / "core"
    tests = [root / name for name in ("test_effort_wire.py", "test_effort_inheritance_wire.py")]
    status = pytest.main([*(str(test) for test in tests), "-q", "-o", "addopts="], plugins=[capture])
    if status:
        return int(status)
    if not capture.records:
        raise RuntimeError("No requests captured; refusing an empty snapshot")
    args.output.write_text(snapshot(capture.records), encoding="utf-8")
    print(f"Recorded {len(capture.records)} requests in {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
