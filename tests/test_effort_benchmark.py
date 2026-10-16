"""Offline guards for the live benchmark; never starts a session."""
import json
from contextlib import contextmanager
from types import SimpleNamespace

from scripts.effort_benchmark import check_task, load_tasks, prepare, read_metrics, summarize


def test_task_inventory_and_broken_coding_baselines(tmp_path):
    tasks = load_tasks()
    assert 15 <= len(tasks) <= 25
    assert len({t["id"] for t in tasks}) == len(tasks)
    assert {t["kind"] for t in tasks} == {"coding", "tools", "search"}
    for task in tasks:
        path = tmp_path / task["id"]
        prepare(task, path)
        if task["check"] == "unittest":
            # Syntax errors must not masquerade as deliberately broken behaviour.
            compile(task["files"]["solution.py"], "solution.py", "exec")
            compile(task["files"]["test_solution.py"], "test_solution.py", "exec")
        assert not check_task(task, path), task["id"]
        if task["check"] == "json":
            (path / "answer.json").write_text(json.dumps(task["expected"]))
            assert check_task(task, path)
            name = next(iter(task["files"]))
            (path / name).write_text("tampered")
            assert not check_task(task, path)


def test_metrics_count_calls_once_and_preserve_missing_reasoning():
    records = [
        ("turn.usage", {"turn_id": "t", "run_id": "r", "usage": {
            "model_call_index": 1, "input_tokens": 10, "output_tokens": 7,
            "reasoning_tokens": 4, "cache_read_input_tokens": 20}}),
        ("turn.usage", {"turn_id": "t", "run_id": "r", "usage": {
            "model_call_index": 1, "input_tokens": 10, "output_tokens": 7,
            "reasoning_tokens": 4, "cache_read_input_tokens": 20}}),
        ("tool.use", {}),
        ("turn.usage", {"turn_id": "t", "run_id": "r", "usage": {
            "model_call_index": 2, "input_tokens": 11, "output_tokens": 8,
            "reasoning_tokens": 5, "cache_read_input_tokens": 21}}),
    ]
    class Response:
        def raise_for_status(self):
            pass
        def iter_lines(self):
            for event, record in records:
                yield "event: " + event
                yield "data: " + json.dumps(record)
            yield "event: caught_up"
    @contextmanager
    def stream(*a):
        yield Response()
    client = SimpleNamespace(_http=SimpleNamespace(stream=stream))
    got = read_metrics(client, "fixture")
    assert got == {"input_tokens": 21, "output_tokens": 15, "reasoning_tokens": 9,
                   "cache_read_tokens": 41, "model_calls": 2, "tool_calls": 1}
    records.clear()
    assert read_metrics(client, "fixture")["reasoning_tokens"] is None


def test_summary_reports_spread_not_only_averages():
    got = summarize([{"config": "low", "passed": True, "wall_seconds": 10},
                     {"config": "low", "passed": False, "wall_seconds": 30}])["low"]
    assert got["pass_rate"] == .5
    assert got["wall_seconds"]["mean"] == 20
    assert got["wall_seconds"]["min"] == 10
    assert got["wall_seconds"]["max"] == 30
    assert got["wall_seconds"]["stdev"] > 0
    assert got["reasoning_tokens"] is None


def test_one_run_sets_effort_and_archives_even_when_metrics_fail(monkeypatch, tmp_path):
    from scripts import effort_benchmark as bench
    frames, archived = [], []
    class Client:
        def _exchange(self, frame, done, **kw):
            frames.append(frame)
            return {"session_id": "owned-fixture"}
        def session(self, sid):
            return {"status": "archived" if archived else "active", "effort": "high",
                    "messages": [{"role": "assistant", "content": "done"}]}
        def state(self, sid):
            return {"is_streaming": False}
        def stop(self, sid):
            assert sid == "owned-fixture"
        def archive(self, sid):
            archived.append(sid)
        def close(self):
            pass
    args = SimpleNamespace(output=str(tmp_path / "result.json"), url="unused", seed=95,
                           repetitions=1, jobs=1, agent="coding", timeout=10)
    runner = bench.Benchmark(args)
    runner.model = "prov:fixture:gpt-6.1-sol"
    monkeypatch.setattr(runner, "client", Client)
    monkeypatch.setattr(bench, "check_task", lambda *a: True)
    def broken_metrics(*a):
        raise ValueError("private detail must not be recorded")
    monkeypatch.setattr(bench, "read_metrics", broken_metrics)
    runner.one((load_tasks()[0], "high", 0))
    assert frames[0]["overrides"]["effort"] == "high"
    assert archived == ["owned-fixture"]
    assert not runner.sessions
    assert json.loads(runner.journal.read_text()) == []
    assert runner.rows[0]["error"] == "ValueError"
    assert "private detail" not in (tmp_path / "result.json").read_text()
