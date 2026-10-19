"""Offline tests for the bench's trace parsers, report metrics and task grading."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from scripts.harness_h2h import report as R
from scripts.harness_h2h import tasks as T
from scripts.harness_h2h import trace as TR


def _jsonl(path: Path, rows) -> Path:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return path


def test_claude_stream_pairs_tool_use_with_result(tmp_path):
    stream = _jsonl(tmp_path / "stream.jsonl", [
        {"t": 10.0, "line": json.dumps({"type": "system", "subtype": "init"})},
        {"t": 12.0, "line": json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "a", "name": "Bash", "input": {"command": "pytest"}}]}})},
        {"t": 15.0, "line": json.dumps({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "a", "content": "ok", "is_error": True}]}})},
        {"t": 16.0, "line": json.dumps({"type": "result", "result": "fixed", "num_turns": 2})},
        {"t": 16.1, "line": "not json"},
    ])
    calls = _jsonl(tmp_path / "calls.jsonl", [
        {"t_start": 10.5, "t_first": 11.0, "t_end": 12.0, "input": 100, "cached": 0, "output": 5,
         "cache_key": "cc:s:abcd"},
        {"t_start": 15.1, "t_first": 15.5, "t_end": 16.0, "input": 10, "cached": 90, "output": 5,
         "cache_key": "cc:s:abcd:small"},
    ])
    tr = TR.from_claude_stream(stream, calls)
    assert tr["final_text"] == "fixed"
    assert tr["tools"] == [{"id": "a", "name": "Bash", "t_start": 12.0, "t_end": 15.0, "is_error": True,
                            "input": '{"command": "pytest"}', "subagent": False, "result_chars": 2}]
    assert [c["role"] for c in tr["calls"]] == ["main", "small"]
    assert tr["parse_errors"] == 1


def test_app_events_rebuild_calls_from_usage_boundaries():
    ev = [
        {"event_type": "turn.started", "data": {"created_at": "2026-10-19T00:00:00+00:00"}},
        {"event_type": "turn.usage", "data": {"created_at": "2026-10-19T00:00:03+00:00",
                                               "usage": {"input_tokens": 30000, "output_tokens": 40,
                                                         "cache_read_tokens": 0}}},
        {"event_type": "tool.use", "data": {"created_at": "2026-10-19T00:00:03+00:00",
                                             "tool_use_id": "t1", "name": "Read", "input": {"p": 1}}},
        {"event_type": "tool.result", "data": {"created_at": "2026-10-19T00:00:04+00:00",
                                                "tool_use_id": "t1", "content_preview": "x"}},
        {"event_type": "assistant.text_delta", "data": {"created_at": "2026-10-19T00:00:06+00:00",
                                                         "text": "done"}},
        {"event_type": "turn.usage", "data": {"created_at": "2026-10-19T00:00:07+00:00",
                                               "usage": {"input_tokens": 300, "output_tokens": 4,
                                                         "cache_read_tokens": 29000}}},
        {"event_type": "turn.completed", "data": {"created_at": "2026-10-19T00:00:07+00:00"}},
    ]
    tr = TR.from_app_events(ev, {})
    c1, c2 = tr["calls"]
    assert c1["t_end"] - c1["t_start"] == pytest.approx(3.0)
    assert c2["t_start"] - c1["t_start"] == pytest.approx(4.0)  # starts at the tool result
    assert (c2["input"], c2["cached"]) == (300, 29000)
    assert tr["tools"][0]["t_end"] - tr["tools"][0]["t_start"] == pytest.approx(1.0)
    assert tr["final_text"] == "done"


def test_app_abort_and_delegation_are_surfaced():
    ev = [
        {"event_type": "turn.started", "data": {"created_at": "2026-10-19T00:00:00+00:00"}},
        {"event_type": "subagent.started", "data": {"worker_session_id": "w1", "agent_name": "A",
                                                     "description": "d"}},
        {"event_type": "turn.aborted", "data": {"created_at": "2026-10-19T00:00:03+00:00",
                                                 "reason": "error", "message": "needs to be reconnected"}},
        {"event_type": "turn.aborted", "data": {"reason": "user"}},
    ]
    tr = TR.from_app_events(ev, {})
    assert tr["aborts"] == ["needs to be reconnected"]
    assert tr["subagents"] == [{"worker_session_id": "w1", "agent_name": "A", "description": "d"}]


def test_matrix_redoes_harness_failures(tmp_path):
    from scripts.harness_h2h import runner
    ok, bad = tmp_path / "ok.json", tmp_path / "bad.json"
    ok.write_text(json.dumps({"grade": {"passed": False}}))
    bad.write_text(json.dumps({"harness_error": "app turn aborted: x"}))
    assert not runner._needs_redo(ok)
    assert runner._needs_redo(bad)
    assert runner._needs_redo(tmp_path / "missing.json")


def test_contamination_scans_past_the_stored_preview(tmp_path):
    long_cmd = "echo " + "x" * 600 + " && gh pr view 2343"
    stream = _jsonl(tmp_path / "stream.jsonl", [
        {"t": 1.0, "line": json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "a", "name": "Bash", "input": {"command": long_cmd}}]}})},
    ])
    tr = TR.from_claude_stream(stream, tmp_path / "none.jsonl")
    assert len(tr["tools"][0]["input"]) == 400 and "gh pr" not in tr["tools"][0]["input"]
    assert len(tr["contamination"]) == 1 and "gh pr view 2343" in tr["contamination"][0]
    app = TR.from_app_events([{"event_type": "tool.use", "data": {
        "tool_use_id": "t", "name": "Bash", "input": {"command": long_cmd}}}], {})
    assert len(app["contamination"]) == 1


def test_workspace_guard_refuses_real_folders(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    out = tmp_path / "out"
    for bad in (T.REPO_ROOT, T.REPO_ROOT / "scripts", Path("/"), Path.home(), repo, tmp_path, out / "ws"):
        with pytest.raises(T.UnsafeWorkspace):
            T.check_workspace(bad, (repo, out))
    T.check_workspace(tmp_path / "ws", (repo, out))
    # A non-empty folder the bench did not create is never emptied.
    record = out / T.OWNER_RECORD
    stranger = tmp_path / "stranger"
    stranger.mkdir()
    (stranger / "keep.txt").write_text("mine")
    with pytest.raises(T.UnsafeWorkspace):
        T.clear_dir(stranger, owner_record=record)
    assert (stranger / "keep.txt").exists()
    ws = tmp_path / "ws"
    T.clear_dir(ws, owner_record=record)
    (ws / "junk").write_text("x")
    # An agent wiping every untracked file (git clean -fdx) leaves the proof intact.
    T.clear_dir(ws, owner_record=record)
    assert list(ws.iterdir()) == []
    assert json.loads(record.read_text())["workspaces"] == [str(ws.resolve())]
    with pytest.raises(T.UnsafeWorkspace):  # the record may not sit in the workspace
        T.clear_dir(ws, owner_record=ws / "own.json")


def test_contamination_flags_routes_to_the_real_fix():
    tools = [{"name": "Bash", "input": "gh pr view 2343"},
             {"name": "Bash", "input": "cat /workspace/ai-shuffle/app/serve.py"},
             {"name": "Bash", "input": "cat /workspace/harness-h2h-ws/app/serve.py"},
             {"name": "Read", "input": "app/core/terminals.py"}]
    hits = TR.contamination(tools)
    assert len(hits) == 2 and "gh pr view" in hits[0]


def test_run_metrics_splits_wall_time():
    r = {"task": "x", "arm": "fluid", "trial": 0, "t_start": 0.0, "t_end": 20.0, "wall_s": 20.0,
         "grade": {"passed": True, "score": 1.0},
         "trace": {"calls": [
             {"t_start": 1.0, "t_first": 2.0, "t_end": 5.0, "input": 1000, "cached": 0, "output": 10,
              "role": "main", "exact": True},
             {"t_start": 8.0, "t_first": 9.0, "t_end": 12.0, "input": 100, "cached": 900, "output": 10,
              "role": "main", "exact": True}],
             "tools": [{"t_start": 5.0, "t_end": 8.0, "is_error": True, "name": "Bash"},
                       {"t_start": 5.5, "t_end": 6.0, "name": "Read"}]}}
    m = R.run_metrics(r)
    assert (m["model_s"], m["tool_s"], m["overhead_s"]) == (8.0, 3.0, 9.0)
    assert m["ttft_s"] == 1.0
    assert m["first_prompt_tokens"] == 1000 and m["prompt_tokens"] == 2000
    assert m["cache_hit"] == 0.45
    assert m["tool_errors"] == 1 and m["tool_names"] == {"Bash": 1, "Read": 1}


def test_aggregate_pairs_against_claude_code():
    def run(task, arm, wall, passed=True):
        return {"task": task, "arm": arm, "passed": passed, "score": 1.0 if passed else 0.0,
                "wall_s": wall, "timed_out": False, "harness_error": None, "contamination": [],
                "est_cost": 0.1, **{k: 1 for k in R.AGG_KEYS if k not in ("wall_s", "est_cost")}}
    metrics = [run("a", "cc", 100), run("a", "fluid", 200), run("b", "cc", 10), run("b", "fluid", 5, False)]
    s = R.aggregate(metrics)
    assert s["arms"]["fluid"]["passed"] == 1 and s["arms"]["cc"]["pass_rate"] == 1.0
    assert s["paired_vs_cc"]["fluid"]["geo_mean_wall_ratio_vs_cc"] == pytest.approx(1.0)
    html = R.render_html(s, [R.run_metrics({"task": "a", "arm": "cc", "trial": 0, "wall_s": 1,
                                            "t_start": 0, "t_end": 1, "trace": {}})], {"model": "m"})
    assert "Claude Code" in html and "<svg" in html


def _repo(tmp_path: Path) -> tuple[str, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin"}
    run = lambda *a: subprocess.run(a, cwd=repo, env=env, check=True, capture_output=True)  # noqa: E731
    run("git", "init", "-q", "-b", "main")
    (repo / "lib.py").write_text("def add(a, b):\n    return a - b\n")
    (repo / "test_lib.py").write_text("def test_placeholder():\n    assert True\n")
    run("git", "add", "-A")
    run("git", "commit", "-qm", "base")
    base = run("git", "rev-parse", "HEAD").stdout.decode().strip()
    (repo / "lib.py").write_text("def add(a, b):\n    return a + b\n")
    (repo / "test_lib.py").write_text("from lib import add\n\ndef test_add():\n    assert add(2, 2) == 4\n")
    run("git", "commit", "-qam", "fix")
    fix = run("git", "rev-parse", "HEAD").stdout.decode().strip()
    return str(repo), base, fix


def test_snapshot_has_no_history_and_grading_uses_hidden_tests(tmp_path):
    import sys
    repo, base, fix = _repo(tmp_path)
    task = T.Task(id="add", title="t", kind="bugfix", repo=repo, base=base, fix=fix, prompt="p",
                  hidden_tests=["test_lib.py"],
                  test_cmd=f"{sys.executable} -m pytest {{tests}} -q -p no:cacheprovider")
    ws = tmp_path / "ws"
    T.snapshot(repo, base, ws, owner_record=tmp_path / T.OWNER_RECORD)
    log = subprocess.run(["git", "log", "--oneline"], cwd=ws, capture_output=True, text=True).stdout
    assert len(log.strip().splitlines()) == 1
    assert fix not in subprocess.run(["git", "rev-list", "--all"], cwd=ws, capture_output=True,
                                     text=True).stdout
    assert T.grade(task, ws)["passed"] is False
    T.snapshot(repo, base, ws, owner_record=tmp_path / T.OWNER_RECORD)
    (ws / "lib.py").write_text("def add(a, b):\n    return a + b\n")
    (ws / "test_lib.py").write_text("def test_agent_cheat():\n    assert True\n")
    graded = T.grade(task, ws)
    assert graded["passed"] is True
    assert graded["diff"]["files"] == ["lib.py", "test_lib.py"]
    assert T.validate(task, tmp_path)["ok"] is True


def test_question_grading(tmp_path):
    task = T.Task(id="q", title="q", kind="question", repo="", base="", prompt="p",
                  answer_checks=["FOO_ENV", r"\b90\b"], answer_forbid=["nonsense"])
    (tmp_path / ".git").mkdir()
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "ANSWER.md").write_text("Set FOO_ENV; the budget is 90 s.")
    g = T.grade(task, tmp_path)
    assert g["passed"] and g["score"] == 1.0


def test_shipped_tasks_load_and_name_their_graders():
    tasks = T.load_tasks()
    assert len({t.id for t in tasks}) == len(tasks)
    for t in tasks:
        if t.kind == "bugfix":
            assert t.fix and t.hidden_tests and "{tests}" in t.test_cmd
        else:
            assert t.answer_checks
