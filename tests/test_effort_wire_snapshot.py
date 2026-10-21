"""Deterministic snapshots keep failed cases out and never imply quality wins."""
from types import SimpleNamespace

from scripts.effort_wire_snapshot import Capture, snapshot


def test_sorting_and_failed_cases():
    capture = Capture()
    for case, passed in [("z", True), ("failed", False), ("a", True)]:
        capture.pytest_runtest_logreport(SimpleNamespace(
            when="call", passed=passed, nodeid=case,
            user_properties=[("wire", {"parameters": {"reasoning": {"effort": "high"}}})]))
    assert snapshot(capture.records) == snapshot(list(reversed(capture.records)))
    assert '"measurement": "request-parameters-only"' in snapshot(capture.records)
    assert "failed" not in snapshot(capture.records)
