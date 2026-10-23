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
                   "cache_read_tokens": 41, "model_calls": 2, "tool_calls": 1,
                   "input_tokens_estimated": False, "output_tokens_estimated": False,
                   "reasoning_tokens_complete": True,
                   "partial_usage_reasons": {},
                   "model_call_outcomes": [
                       {"model_call_index": i, "partial_usage_reason": None, "estimated": False}
                       for i in (1, 2)]}
    records.clear()
    for i, reason in enumerate(("stop", "argument_guard", "dropped_stream", "provider_missing_usage", "unknown")):
        raw = {"model_call_index": i, "input_tokens_estimated": True}
        if reason != "unknown":
            raw["partial_usage_reason"] = reason
        records.append(("turn.usage", {"usage": raw}))
    assert read_metrics(client, "fixture")["partial_usage_reasons"] == {
        reason: 1 for reason in ("stop", "argument_guard", "dropped_stream", "provider_missing_usage", "unknown")}
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
    assert runner.rows[0]["metrics_error"] == "ValueError"
    assert not runner.rows[0].get("cleanup_failed")
    assert "private detail" not in (tmp_path / "result.json").read_text()


def test_cluster_bootstrap_is_reproducible_and_rejects_duplicate_runs():
    import pytest
    from scripts.effort_benchmark_analysis import analyze, paired_interval
    rows = [{"task": task, "kind": "coding", "config": config,
             "repetition": rep, "passed": True, "wall_seconds": seconds}
            for task in ("a", "b") for rep in range(3)
            for config, seconds in (("default", 20), ("high", 10))]
    result = paired_interval(rows, "wall_seconds")
    assert result["pairs"] == 6
    assert result["task_clusters"] == 2
    assert result["mean_difference"] == -10
    assert result["ci95"] == [-10, -10]
    assert paired_interval(list(reversed(rows)), "wall_seconds") == result
    with pytest.raises(ValueError, match="duplicate"):
        analyze({"rows": rows + rows[:1]})


def test_committed_evidence_has_all_repetitions_and_matches_task_fixture():
    import hashlib
    from pathlib import Path
    from scripts.effort_benchmark import TASK_FILE
    from scripts.effort_benchmark_analysis import analyze
    data = json.loads((Path(__file__).resolve().parents[1] / "docs" / "benchmarks" /
                       "effort-gpt-6.1-sol-2026-10-16.json").read_text())
    assert data["task_sha256"] == hashlib.sha256(TASK_FILE.read_bytes()).hexdigest()
    expected = {(task["id"], config, rep) for task in load_tasks()
                for config in ("default", "low", "medium", "high", "xhigh") for rep in range(3)}
    assert len(data["rows"]) == 300
    assert {(r["task"], r["config"], r["repetition"]) for r in data["rows"]} == expected
    assert data["cleanup"]["sweep_sessions_archived_and_read_back"] == 300
    computed = analyze(data)
    assert data["summary"] == computed["summary"]
    assert data["high_vs_default"] == computed["high_vs_default"]
    assert all(r["observed_effort"] == r["config"] for r in data["rows"]
               if r["config"] != "default")
    assert all("reasoning_tokens_complete" in r for r in data["rows"])


def test_preflight_selects_the_requested_route_and_refuses_a_missing_one(tmp_path):
    import pytest
    from scripts import effort_benchmark as bench
    catalog = {"models": [{"value": "opus", "effort_levels": ["low", "high"], "default_effort": "high"}],
               "providers": [
                   {"id": "sub", "provider_type": "chatgpt", "models": [
                       {"id": "gpt-6.1-sol", "effort_levels": ["low", "high"], "default_effort": "low"}]},
                   {"id": "key", "provider_type": "openai", "models": [
                       {"id": "gpt-6.1-sol", "effort_levels": ["low", "medium", "high"], "default_effort": "medium"}]}]}
    class Client:
        def deploy_status(self):
            return {"workers": [{"is_self": True, "revision": "fixture"}]}
        def models(self):
            return catalog
        def agents(self):
            return [{"id": "coding"}]
        def close(self):
            pass
    def runner(**kw):
        args = SimpleNamespace(output=str(tmp_path / "r.json"), url="unused", agent="coding",
                               configs=["default", "high"], **kw)
        r = bench.Benchmark(args)
        r.client = Client
        return r
    legacy = runner()
    legacy.preflight()
    assert legacy.model == "prov:sub:gpt-6.1-sol"
    key = runner(model="gpt-6.1-sol", provider="openai")
    key.preflight()
    assert key.model == "prov:key:gpt-6.1-sol"
    assert key.capability == {"effort_levels": ["low", "medium", "high"], "default_effort": "medium"}
    claude = runner(model="opus", provider="claude")
    claude.preflight()
    assert claude.model == "opus"
    with pytest.raises(ValueError, match="no configured openrouter provider"):
        runner(model="gpt-6.1-sol", provider="openrouter").preflight()
