"""Deterministic snapshots keep failed cases out and never imply quality wins."""
from types import SimpleNamespace

from scripts.effort_wire_snapshot import Capture, main, snapshot


def test_sorting_and_failed_cases():
    capture = Capture()
    for case, passed in [("z", True), ("failed", False), ("a", True)]:
        capture.pytest_runtest_logreport(SimpleNamespace(
            when="call", passed=passed, nodeid=case,
            user_properties=[("wire", {"parameters": {"reasoning": {"effort": "high"}}})]))
    assert snapshot(capture.records) == snapshot(list(reversed(capture.records)))
    assert '"measurement": "request-parameters-only"' in snapshot(capture.records)
    assert "failed" not in snapshot(capture.records)


def test_main_includes_child_producer_suite(monkeypatch, tmp_path):
    from pathlib import Path
    import pytest

    def run(args, plugins):
        assert [Path(arg).name for arg in args[:2]] == [
            "test_effort_wire.py", "test_effort_inheritance_wire.py"]
        plugins[0].records.append({"case": "child", "source": "child-turn-resume"})
        return 0

    monkeypatch.setattr(pytest, "main", run)
    output = tmp_path / "snapshot.json"
    assert main(["--output", str(output)]) == 0
    assert "child-turn-resume" in output.read_text()


def test_failed_suite_does_not_write_snapshot(monkeypatch, tmp_path):
    import pytest

    monkeypatch.setattr(pytest, "main", lambda *a, **kw: 1)
    output = tmp_path / "snapshot.json"
    assert main(["--output", str(output)]) == 1
    assert not output.exists()
