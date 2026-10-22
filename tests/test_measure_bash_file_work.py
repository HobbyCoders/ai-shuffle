"""The audit reports aggregate requests, never command/session text."""
import importlib.util
import pytest
import json
from pathlib import Path

spec = importlib.util.spec_from_file_location("bash_work_audit", Path(__file__).parents[1] / "scripts/measure_bash_file_work.py")
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


def event(identifier, command="cat private-path", ts=150):
    return dict(tool_use_id=identifier, input={"command": command}, ts=ts)


def test_counts_rates_exclusion_dedup_and_window():
    rows = [
        ("s1", "delegated", event("one")),
        ("s1", "delegated", json.dumps(event("one"))),
        ("s1", "delegated", event("two", "git status")),
        ("s2", "native", event("one", "grep -rn token path")),
        ("excluded", "native", event("x")),
        ("s2", "native", event("old", ts=99)),
        ("s2", "native", event("future", ts=200)),
        ("s2", "native", event("unknown", ts=0)),
    ]
    report = audit.aggregate(rows, 100, 200, "excluded")
    assert report["legs"]["delegated"]["total"] == 2
    assert report["legs"]["delegated"]["categories"]["file-read"] == {"count": 1, "rate": .5}
    assert report["legs"]["native"]["total"] == 1
    assert report["legs"]["native"]["categories"]["search"]["rate"] == 1
    assert report["unknown_timestamp_events"] == 1
    assert "private-path" not in json.dumps(report)
    assert "token path" not in json.dumps(report)
    assert "s1" not in json.dumps(report)


def test_unclassifiable_is_not_silently_counted_as_shell():
    report = audit.aggregate([("s", "unknown", dict(tool_use_id="x", input={}, ts=150))], 100, 200)
    assert report["legs"]["unknown"]["categories"]["unclassifiable"]["count"] == 1


@pytest.mark.parametrize("fail", [False, True])
def test_main_uses_read_only_snapshot_and_always_rolls_back(monkeypatch, capsys, fail):
    from app.core.config import settings
    import psycopg2
    calls = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, sql, params=None):
            calls.append((sql, params))
            if fail:
                raise RuntimeError("private exception text")

        def __iter__(self):
            return iter([])

    class Connection:
        def set_session(self, **options):
            calls.append(options)

        def cursor(self):
            return Cursor()

        def rollback(self):
            calls.append("rollback")

        def close(self):
            calls.append("close")

    monkeypatch.setattr(type(settings), "get_database_connection", lambda self: {})
    monkeypatch.setattr(psycopg2, "connect", lambda **kw: Connection())
    monkeypatch.setattr("sys.argv", ["audit", "--until", "2026-10-22T00:00:00+00:00"])
    if fail:
        with pytest.raises(RuntimeError):
            audit.main()
    else:
        audit.main()
        assert json.loads(capsys.readouterr().out)["legs"] == {}
    assert calls[0] == dict(readonly=True, isolation_level="REPEATABLE READ")
    assert calls[-2:] == ["rollback", "close"]
