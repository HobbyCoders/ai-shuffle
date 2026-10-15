"""The live smoke runner and release gate, against a fake worker API.

No real worker, model or token is involved: ``FakeWorker`` stands in for the
worker's HTTP API, and the client's own wire behaviour is checked against
``httpx.MockTransport`` and a local WebSocket stub.
"""
import json
import threading
from urllib.parse import urlsplit

import httpx
import pytest

from scripts.live_smoke import __main__ as cli
from scripts.live_smoke import gate, results, scenarios
from scripts.live_smoke.client import (
    AuthRefused,
    ClientError,
    HttpWorkerClient,
    Unreachable,
)
from scripts.live_smoke.runner import Runner, SetupError, pick_model, tiny_png

REV = "a810cf58f0e1d2c3b4a5968778695a4b3c2d1e0f"
PEER_REV = "b" * 40


class FakeWorker:
    """In-memory stand-in for the endpoints the runner calls."""

    def __init__(self, *, peer_online=True, peer=True, answer=None, tokens=100, default_agent="coding"):
        self.peer_online, self.has_peer = peer_online, peer
        self.prefs = {"composer.profile": default_agent} if default_agent is not None else {}
        self.answer = answer or (lambda prompt: ("OK", []))
        self.tokens = tokens
        self.sessions = {}
        self.sent, self.archived, self.stopped, self.compacted = [], [], [], []
        self.raise_on_send = None
        self.calls = 0
        self.compact_note = None

    def deploy_status(self):
        workers = [{"label": "ServerWorker", "device_id": "dev_self", "is_self": True, "online": True,
                    "revision": REV, "short": REV[:7], "app_version": "1.0"}]
        if self.has_peer:
            workers.append({"label": "DesktopWorker", "device_id": "dev_peer", "is_self": False,
                            "online": self.peer_online, "revision": PEER_REV, "short": PEER_REV[:7]})
        return {"workers": workers}

    def installs(self):
        rows = [{"id": "dev_self", "device_name": "ServerWorker", "online": True, "approved": True, "is_self": True}]
        if self.has_peer:
            rows.append({"id": "dev_peer", "device_name": "DesktopWorker", "online": self.peer_online,
                         "approved": True, "is_self": False})
        return {"installs": rows, "self_instance_id": "dev_self"}

    def models(self):
        return {"models": [{"value": "opus", "label": "Opus"}, {"value": "haiku", "label": "Haiku"}],
                "providers": [], "claude": {"enabled": True}}

    def projects(self):
        return [{"id": "proj1", "name": "ai-shuffle"}]

    def agents(self):
        return [{"id": "coding", "is_builtin": False}, {"id": "general-purpose", "is_builtin": True},
                {"id": "frontend-designer", "is_builtin": True}]

    def preference(self, key):
        return self.prefs.get(key)

    def send(self, prompt, *, session_id=None, target_instance_id=None, **kw):
        if self.raise_on_send and self.raise_on_send(prompt):
            raise self.raise_on_send.exc
        self.sent.append({"prompt": prompt, "session_id": session_id, "target": target_instance_id, **kw})
        if session_id is None:
            session_id = f"s{len(self.sessions) + 1}"
            self.sessions[session_id] = {"messages": [], "queue": [], "tokens": 0,
                                         "remote": target_instance_id == "dev_peer"}
        s = self.sessions[session_id]
        s["messages"].append({"id": len(s["messages"]), "role": "user", "type": None, "content": prompt})
        s["queue"].append(prompt)
        return session_id

    def _advance(self, s):
        if s["queue"] and s.get("ticked"):
            prompt = s["queue"].pop(0)
            text, tools = self.answer(prompt)
            for tool in tools:
                s["messages"].append({"id": len(s["messages"]), "role": "assistant", "type": "tool_use",
                                      "content": "", "toolName": tool})
            s["messages"].append({"id": len(s["messages"]), "role": "assistant", "type": "text", "content": text})
            s["tokens"] += self.tokens
            s["ticked"] = False
        elif s["queue"]:
            s["ticked"] = True

    def session(self, sid):
        self.calls += 1
        s = self.sessions[sid]
        self._advance(s)
        busy = bool(s["queue"])
        return {"id": sid, "messages": [dict(m) for m in s["messages"]],
                "total_tokens_in": s["tokens"], "total_tokens_out": 0, "cache_read_tokens": 50,
                "is_remote": s["remote"], "device_name": "DesktopWorker" if s["remote"] else "ServerWorker",
                "instance_id": "dev_peer" if s["remote"] else "dev_self",
                "remote_runtime_state": ("working" if busy else "idle") if s["remote"] else None,
                "_busy": busy}

    def state(self, sid):
        return {"is_streaming": bool(self.sessions[sid]["queue"])}

    def compact(self, sid):
        self.compacted.append(sid)
        s = self.sessions[sid]
        if self.compact_note is not None:
            # Native leg: the outcome is a system.note in the event log only.
            s.setdefault("notes", []).append(
                {"type": "system.note", "subtype": "compact_failed", "data": {"reason": self.compact_note}})
            return {}
        s["messages"].append({"id": len(s["messages"]), "role": "system", "type": "system",
                              "content": "", "systemSubtype": "compact_boundary"})
        return {}

    def system_notes(self, sid):
        return list(self.sessions[sid].get("notes", []))

    def stop(self, sid):
        self.stopped.append(sid)
        self.sessions[sid]["queue"].clear()

    def archive(self, sid):
        self.archived.append(sid)


def _data(*rows, cap=600000):
    return {"version": 1, "defaults": {"token_cap": cap, "within_s": 60, "project": "ai-shuffle",
                                       "permission_mode": "bypassPermissions"},
            "suites": [{"name": "t", "rows": list(rows)}]}


def _script(rid, prompt="reply OK", **step):
    return {"id": rid, "check": rid, "kind": "script",
            "steps": [{"op": "start", "as": "a", "prompt": prompt, "expect_reply": "^OK$", **step}]}


def _run(worker, data, tmp_path, **kw):
    runner = Runner(worker, data, results_base=tmp_path, poll_s=0, run_id="r1", **kw)
    return runner.run()


# -- runner ------------------------------------------------------------------


def test_pass_writes_result_file_and_archives(tmp_path):
    worker = FakeWorker()
    data = _data(_script("t.1"), {"id": "t.2", "check": "Stop", "kind": "browser"},
                 {"id": "t.3", "check": "Quit", "kind": "owner"})
    result, path = _run(worker, data, tmp_path)

    assert path == tmp_path / REV / "ServerWorker.json"
    on_disk = json.loads(path.read_text())
    assert on_disk == result
    assert result["outcome"] == "pass"
    assert result["revision"] == REV and result["worker"] == "ServerWorker"
    assert result["peer"] == {"label": "DesktopWorker", "revision": PEER_REV, "online": True}
    assert result["model"] == {"model": "haiku", "why": "cheapest by name ('haiku')"}
    rows = {r["id"]: r for r in result["rows"]}
    assert rows["t.1"]["status"] == "pass"
    assert rows["t.1"]["evidence"]["sessions"] == {"a": "s1"}
    assert rows["t.1"]["evidence"]["message_index"] == 1
    assert rows["t.2"] == {"id": "t.2", "kind": "browser", "check": "Stop", "status": "not_run",
                           "reason": "needs browser", "evidence": {}}
    assert rows["t.3"]["reason"] == "owner step"
    assert result["tokens"] == {"input": 100, "output": 0, "cache_write": 0, "cache_read": 50, "counted": 100}
    assert result["sessions"] == [{"session_id": "s1", "row": "t.1", "archived": True}]
    assert worker.archived == ["s1"]
    assert worker.sent[0]["model"] == "haiku" and worker.sent[0]["permission_mode"] == "bypassPermissions"
    text = path.read_text().lower()
    assert "authorization" not in text and "password" not in text and "token\":" not in text


def test_failing_reply_is_a_fail_with_evidence(tmp_path):
    worker = FakeWorker(answer=lambda p: ("NOPE", []))
    result, _ = _run(worker, _data(_script("t.1")), tmp_path)
    row = result["rows"][0]
    assert row["status"] == "fail"
    assert "does not match" in row["reason"]
    assert row["evidence"]["sessions"] == {"a": "s1"} and row["evidence"]["message_index"] == 1
    assert result["outcome"] == "fail"
    assert worker.archived == ["s1"]


def test_missing_tool_and_user_count_checks(tmp_path):
    worker = FakeWorker(answer=lambda p: ("OK", ["mcp__ai-tools__editImage"]))
    ok = _script("t.1", expect_tool=["editImage"], user_messages=1)
    bad = _script("t.2", expect_tool=["Swarm"])
    result, _ = _run(worker, _data(ok, bad), tmp_path)
    assert result["rows"][0]["status"] == "pass"
    assert result["rows"][1]["status"] == "fail" and "Swarm" in result["rows"][1]["reason"]


def test_token_cap_stops_the_run(tmp_path):
    worker = FakeWorker(tokens=400)
    data = _data(_script("t.1"), _script("t.2"), _script("t.3"), cap=600)
    result, _ = _run(worker, data, tmp_path)
    status = [(r["id"], r["status"], r["reason"]) for r in result["rows"]]
    assert status[0] == ("t.1", "pass", None)
    assert status[1][1] == "fail" and status[1][2].startswith("token cap exceeded: 800 > 600")
    assert status[2] == ("t.3", "not_run", "token cap reached")
    assert result["cap_exceeded"] is True and result["outcome"] == "fail"
    assert "s2" in worker.stopped
    assert sorted(worker.archived) == ["s1", "s2"]
    assert len(worker.sent) == 2
    verdict = gate.evaluate(tmp_path, REV, ["ServerWorker"], data["suites"][0]["rows"])
    assert verdict["verdict"] == "block"


def test_token_cap_flag_overrides_default():
    runner = Runner(FakeWorker(), _data(_script("t.1")), results_base="/nonexistent", token_cap=5)
    assert runner.cap == 5
    assert Runner(FakeWorker(), _data(_script("t.1")), results_base="/x").cap == 600000


@pytest.mark.parametrize("peer_kwargs, reason", [
    ({"peer_online": False}, "worker offline: DesktopWorker"),
    ({"peer": False}, "worker offline: no other worker is paired"),
])
def test_offline_peer_rows_are_not_run(tmp_path, peer_kwargs, reason):
    worker = FakeWorker(**peer_kwargs)
    remote = _script("t.1", on="peer", expect_owner="peer")
    flagged = {"id": "t.2", "check": "ids", "kind": "script", "two_worker": True, "steps": [{"op": "workers"}]}
    local = _script("t.3")
    result, _ = _run(worker, _data(remote, flagged, local), tmp_path)
    rows = {r["id"]: r for r in result["rows"]}
    assert rows["t.1"]["status"] == "not_run" and rows["t.1"]["reason"] == reason
    assert rows["t.2"]["status"] == "not_run" and rows["t.2"]["reason"] == reason
    assert rows["t.3"]["status"] == "pass"
    assert all(s["target"] is None for s in worker.sent)
    assert result["outcome"] == "incomplete"


def test_peer_session_is_targeted_and_ownership_checked(tmp_path):
    worker = FakeWorker()
    result, _ = _run(worker, _data(_script("t.1", on="peer", expect_owner="peer")), tmp_path)
    assert result["rows"][0]["status"] == "pass"
    assert worker.sent[0]["target"] == "dev_peer"


def test_interrupted_run_still_archives_and_writes(tmp_path):
    worker = FakeWorker()

    class Stop:
        exc = KeyboardInterrupt()

        def __call__(self, prompt):
            return prompt == "second"
    worker.raise_on_send = Stop()
    data = _data(_script("t.1"), _script("t.2", prompt="second"), _script("t.3"))
    result, path = _run(worker, data, tmp_path)
    assert result["outcome"] == "interrupted"
    assert [r["status"] for r in result["rows"]] == ["pass", "not_run", "not_run"]
    assert [r["reason"] for r in result["rows"][1:]] == ["interrupted", "interrupted"]
    assert worker.archived == ["s1"]
    assert json.loads(path.read_text())["outcome"] == "interrupted"


def test_interrupt_while_polling_archives_the_running_session(tmp_path):
    worker = FakeWorker()
    real_session = worker.session

    def session(sid):
        if worker.calls >= 1 and not worker.archived:
            worker.calls += 1
            if worker.calls == 2:
                raise KeyboardInterrupt
        return real_session(sid)
    worker.session = session
    result, _ = _run(worker, _data(_script("t.1")), tmp_path)
    assert result["outcome"] == "interrupted"
    assert worker.archived == ["s1"]


def test_unreachable_worker_writes_nothing(tmp_path):
    class Down(FakeWorker):
        def deploy_status(self):
            raise Unreachable("http://127.0.0.1:8000 unreachable: ConnectError")
    with pytest.raises(SetupError, match="unreachable"):
        _run(Down(), _data(_script("t.1")), tmp_path)
    assert not any(tmp_path.iterdir())


def test_partial_run_writes_partial_file(tmp_path):
    worker = FakeWorker()
    result, path = _run(worker, _data(_script("t.1"), _script("t.2")), tmp_path, only=["t.2"])
    assert path.name == "ServerWorker-partial-r1.json" and result["partial"] is True
    assert [r["id"] for r in result["rows"]] == ["t.2"]
    with pytest.raises(SetupError, match="unknown row ids"):
        _run(FakeWorker(), _data(_script("t.1")), tmp_path, only=["nope"])


def test_message_as_turn_ends_and_compact(tmp_path):
    def answer(prompt):
        return ("ONE" if "ONE" in prompt else "TWO" if "TWO" in prompt else "OK"), []
    worker = FakeWorker(answer=answer)
    turn_end = {"id": "t.1", "check": "x", "kind": "script", "steps": [
        {"op": "start", "as": "a", "wait": False, "prompt": "reply ONE"},
        {"op": "message", "session": "a", "when": "reply_seen", "prompt": "reply TWO",
         "user_messages": 2, "replies": {"\\bONE\\b": 1, "\\bTWO\\b": 1}}]}
    compact = {"id": "t.2", "check": "c", "kind": "script", "steps": [
        {"op": "start", "as": "a", "prompt": "reply OK", "expect_reply": "OK"},
        {"op": "compact", "session": "a"},
        {"op": "message", "session": "a", "prompt": "reply TWO", "expect_reply": "TWO"}]}
    result, _ = _run(worker, _data(turn_end, compact), tmp_path)
    assert [r["status"] for r in result["rows"]] == ["pass", "pass"], result["rows"]
    assert worker.compacted == ["s2"]
    assert result["rows"][1]["evidence"]["compact"] == "boundary recorded"


def _compact_row():
    return {"id": "t.1", "check": "c", "kind": "script", "steps": [
        {"op": "start", "as": "a", "prompt": "reply OK", "expect_reply": "OK"},
        {"op": "compact", "session": "a", "within_s": 0},
        {"op": "message", "session": "a", "prompt": "reply OK", "expect_reply": "OK"}]}


def test_native_compact_with_nothing_to_compact_passes(tmp_path):
    # A native chat this short has nothing to reclaim; the engine says so in
    # a compact_failed note that only the event log carries.
    worker = FakeWorker()
    worker.compact_note = ("nothing could be compacted: the context is already as small "
                           "as compaction makes it without touching the most recent work")
    result, _ = _run(worker, _data(_compact_row()), tmp_path)
    row = result["rows"][0]
    assert row["status"] == "pass", row
    assert row["evidence"]["compact"].startswith("native engine: nothing could be compacted")


def test_native_compact_failure_fails_the_row(tmp_path):
    worker = FakeWorker()
    worker.compact_note = "compaction failed: RuntimeError"
    result, _ = _run(worker, _data(_compact_row()), tmp_path)
    row = result["rows"][0]
    assert row["status"] == "fail"
    assert row["reason"] == "compact_failed recorded: compaction failed: RuntimeError"


def test_code_mode_subagent_counts_as_task(tmp_path):
    # Native code mode launches Task inside run_python; the history stores a
    # "subagent" card, not a Task tool_use.
    worker = FakeWorker(answer=lambda p: ("OK", ["run_python"]))
    real = worker._advance

    def advance(s):
        before = len(s["messages"])
        real(s)
        if len(s["messages"]) > before:
            s["messages"].insert(-1, {"id": 99, "role": "assistant", "type": "subagent", "content": ""})
    worker._advance = advance
    result, _ = _run(worker, _data(_script("t.1", expect_tool=["Task"])), tmp_path)
    assert result["rows"][0]["status"] == "pass", result["rows"][0]


def _two_message_row(rid="t.1"):
    return {"id": rid, "check": "x", "kind": "script", "steps": [
        {"op": "start", "as": "a", "prompt": "reply OK", "expect_reply": "OK"},
        {"op": "message", "session": "a", "prompt": "reply OK again", "expect_reply": "OK"},
        {"op": "message", "session": "a", "when": "reply_seen", "prompt": "reply OK once more"}]}


def test_default_agent_is_sent_on_start_and_every_message(tmp_path):
    # The web UI starts a chat with the account's default agent and sends the
    # session's agent on every later message; the worker refuses a query
    # that carries none ("That agent no longer exists" / "Profile not found").
    worker = FakeWorker()
    result, _ = _run(worker, _data(_two_message_row()), tmp_path)
    assert result["rows"][0]["status"] == "pass", result["rows"]
    assert [(s["session_id"], s.get("profile")) for s in worker.sent] == [
        (None, "coding"), ("s1", "coding"), ("s1", "coding")]
    assert result["agent"] == {"agent": "coding", "why": "account default"}


def test_agent_flag_is_sent_on_start_and_every_message(tmp_path):
    worker = FakeWorker()
    result, _ = _run(worker, _data(_two_message_row()), tmp_path, agent="general-purpose")
    assert result["rows"][0]["status"] == "pass", result["rows"]
    assert [s.get("profile") for s in worker.sent] == ["general-purpose"] * 3
    assert result["agent"] == {"agent": "general-purpose", "why": "given"}


def test_stale_default_agent_falls_back_like_the_ui(tmp_path):
    worker = FakeWorker(default_agent="deleted-agent")
    result, _ = _run(worker, _data(_script("t.1")), tmp_path)
    assert worker.sent[0]["profile"] == "general-purpose"  # first built-in, as validateSelectedProfile
    assert result["agent"] == {"agent": "general-purpose", "why": "account default 'deleted-agent' no longer exists"}


@pytest.mark.parametrize("kwargs, agent, msg", [
    ({"default_agent": None}, None, r"no default agent: pick an agent .* --agent ID \(available: coding, general-purpose, frontend-designer\)"),
    ({"default_agent": ""}, None, "no default agent"),
    ({}, "nope", r"agent 'nope' does not exist on this worker \(--agent; available: coding"),
])
def test_missing_agent_stops_before_any_session(tmp_path, kwargs, agent, msg):
    worker = FakeWorker(**kwargs)
    with pytest.raises(SetupError, match=msg):
        _run(worker, _data(_script("t.1")), tmp_path, agent=agent)
    assert worker.sent == [] and worker.sessions == {}
    assert not any(tmp_path.iterdir())


def test_no_agents_on_the_worker_is_a_setup_error(tmp_path):
    worker = FakeWorker()
    worker.agents = lambda: []
    with pytest.raises(SetupError, match="no agents are configured"):
        _run(worker, _data(_script("t.1")), tmp_path)
    assert worker.sent == []


def test_runs_without_script_rows_need_no_agent(tmp_path):
    worker = FakeWorker(default_agent=None)
    result, _ = _run(worker, _data({"id": "t.1", "check": "Stop", "kind": "browser"}), tmp_path)
    assert result["agent"] is None and result["rows"][0]["reason"] == "needs browser"


def test_placeholders_and_image(tmp_path):
    worker = FakeWorker()
    row = {"id": "t.1", "check": "x", "kind": "script", "steps": [
        {"op": "start", "as": "a", "prompt": "run {run} on {peer}"},
        {"op": "start", "as": "b", "prompt": "message {session:a} with {image}"}]}
    _run(worker, _data(row), tmp_path)
    assert worker.sent[0]["prompt"] == "run r1 on DesktopWorker"
    image = tmp_path / REV / "assets" / "live-smoke-r1.png"
    assert worker.sent[1]["prompt"] == f"message s1 with {image}"
    assert image.read_bytes().startswith(b"\x89PNG")


def test_pick_model_prefers_cheap_usable_models():
    models = {"models": [{"value": "opus", "label": "Opus"}], "claude": {"enabled": False},
              "providers": [{"id": "oa", "usable": True, "models": [{"id": "gpt-5", "label": "GPT-5"},
                                                                    {"id": "gpt-5-mini", "label": "mini"}]},
                            {"id": "x", "usable": False, "models": [{"id": "nano", "label": "nano"}]}]}
    assert pick_model(models)[0] == "prov:oa:gpt-5-mini"
    assert pick_model({"models": [{"value": "opus"}]}) == (None, "worker default (no model name looked cheap)")


def test_tiny_png_is_valid(tmp_path):
    data = tiny_png(tmp_path / "x.png").read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n" and data.endswith(b"IEND\xaeB`\x82")


# -- scenarios ---------------------------------------------------------------


def test_real_scenarios_validate_and_markdown_is_generated():
    data = scenarios.load()
    rows = scenarios.rows(data)
    assert data["defaults"]["token_cap"] == 600000
    ids = {r["id"] for r in rows}
    assert {"two-worker-smoke.9", "harness-basics.3"} <= ids
    kinds = {r["kind"] for r in rows}
    assert kinds == {"script", "browser", "owner"}
    assert scenarios.SCENARIOS_MD.read_text(encoding="utf-8") == scenarios.render_markdown(data), (
        "scenarios.md is stale: run python -m scripts.live_smoke render")


@pytest.mark.parametrize("row, msg", [
    ({"id": "x", "check": "x", "kind": "robot"}, "kind"),
    ({"id": "x", "check": "x", "kind": "script", "steps": [{"op": "jump"}]}, "unknown op"),
    ({"id": "x", "check": "x", "kind": "script", "steps": [{"op": "message", "session": "a", "prompt": "p"}]},
     "no earlier step"),
    ({"id": "x", "check": "x", "kind": "browser", "steps": [{"op": "workers"}]}, "only script"),
    ({"id": "x", "check": "x", "kind": "script", "steps": [{"op": "start", "prompt": "p", "typo": 1}]},
     "does not take"),
])
def test_validate_rejects_bad_rows(row, msg):
    with pytest.raises(scenarios.ScenarioError, match=msg):
        scenarios.validate(_data(row))


# -- gate --------------------------------------------------------------------

ROWS = [
    {"id": "s.1", "check": "a", "kind": "script"},
    {"id": "s.2", "check": "b", "kind": "browser"},
    {"id": "s.3", "check": "c", "kind": "owner"},
    {"id": "s.4", "check": "d", "kind": "browser", "optional": True},
]


def _write(tmp_path, worker="ServerWorker", revision=REV, **rows):
    statuses = {"s.1": "pass", "s.2": "pass", "s.3": "not_run", "s.4": "not_run", **rows}
    kinds = {r["id"]: r["kind"] for r in ROWS}
    data = {"revision": revision, "worker": worker, "outcome": "pass", "cap_exceeded": False,
            "rows": [{"id": k, "kind": kinds[k], "status": v, "reason": None if v == "pass" else "needs browser"}
                     for k, v in statuses.items() if v is not None]}
    return results.write(results.result_path(tmp_path, REV, worker), data)


def test_gate_pass(tmp_path):
    _write(tmp_path)
    _write(tmp_path, worker="DesktopWorker")
    report = gate.evaluate(tmp_path, REV, ["ServerWorker", "DesktopWorker"], ROWS)
    assert report["verdict"] == "pass" and report["blockers"] == []
    assert report["owner_todo"] == ["ServerWorker: s.3 (c) needs the owner", "DesktopWorker: s.3 (c) needs the owner"]
    assert report["release_notes_lines"] == [f"Live smoke passed on ServerWorker, DesktopWorker at {REV[:9]}."]


@pytest.mark.parametrize("rows, expect", [
    ({"s.1": "fail"}, "ServerWorker: s.1 failed"),
    ({"s.1": "not_run"}, "ServerWorker: s.1 not run"),
    ({"s.2": "not_run"}, "s.2 not recorded"),
    ({"s.2": None}, "s.2 missing from the result"),
])
def test_gate_blocks_on_rows(tmp_path, rows, expect):
    _write(tmp_path, **rows)
    report = gate.evaluate(tmp_path, REV, ["ServerWorker"], ROWS)
    assert report["verdict"] == "block"
    assert any(expect in b for b in report["blockers"]), report["blockers"]
    assert report["release_notes_lines"] == []


def test_gate_blocks_on_missing_file_and_wrong_revision(tmp_path):
    _write(tmp_path)
    report = gate.evaluate(tmp_path, REV, ["ServerWorker", "DesktopWorker"], ROWS)
    assert report["verdict"] == "block"
    assert any(b.startswith("no smoke result for DesktopWorker") for b in report["blockers"])

    _write(tmp_path, worker="DesktopWorker", revision="c" * 40)
    report = gate.evaluate(tmp_path, REV, ["DesktopWorker"], ROWS)
    assert report["blockers"] == [f"DesktopWorker's result is for revision {'c' * 9}, not {REV[:9]}"]

    empty = gate.evaluate(tmp_path, "d" * 40, None, ROWS)
    assert empty["verdict"] == "block" and "no smoke result for revision" in empty["blockers"][0]


def test_gate_blocks_interrupted_and_over_cap(tmp_path):
    path = _write(tmp_path)
    data = json.loads(path.read_text())
    data.update(outcome="interrupted", cap_exceeded=True, token_cap=600000)
    results.write(path, data)
    blockers = gate.evaluate(tmp_path, REV, ["ServerWorker"], ROWS)["blockers"]
    assert "ServerWorker's run was interrupted" in blockers
    assert "ServerWorker's run hit the token cap (600000)" in blockers


def test_gate_skip_is_recorded(tmp_path):
    report = gate.evaluate(tmp_path, REV, ["ServerWorker"], ROWS, skip_reason="DesktopWorker is off for the week")
    assert report["verdict"] == "skipped" and report["blockers"] == []
    assert report["would_block"]
    assert report["release_notes_lines"] == ["Live smoke skipped (--skip-smoke): DesktopWorker is off for the week"]


def test_gate_discovers_workers_and_ignores_partials(tmp_path):
    _write(tmp_path)
    results.write(results.result_path(tmp_path, REV, "ServerWorker", partial="r9"), {"revision": REV, "rows": []})
    assert gate.discover_workers(tmp_path, REV) == ["ServerWorker"]


# -- record / check / import CLI ---------------------------------------------


def test_record_then_check_cli(tmp_path, capsys):
    _write(tmp_path, **{"s.2": "not_run"})
    base = ["--results-dir", str(tmp_path)]
    scen_file = tmp_path / "scen.json"
    scen_file.write_text(json.dumps({"version": 1, "defaults": {}, "suites": [{"name": "s", "rows": [
        {**r, **({"steps": [{"op": "workers"}]} if r["kind"] == "script" else {})} for r in ROWS]}]}))
    check = ["check", REV, "--worker", "ServerWorker", "--scenarios", str(scen_file), *base]
    assert cli.main(check) == 1
    assert cli.main(["record", REV, "--worker", "ServerWorker", "--row", "s.1", "--status", "pass",
                     "--evidence", "x", *base]) == 1  # script rows are run, not recorded
    assert cli.main(["record", REV, "--worker", "ServerWorker", "--row", "s.2", "--status", "pass",
                     "--evidence", "snapshot /tmp/s2.png", "--by", "owner", *base]) == 0
    row = next(r for r in results.read(results.result_path(tmp_path, REV, "ServerWorker"))["rows"] if r["id"] == "s.2")
    assert row["status"] == "pass" and row["evidence"] == {"note": "snapshot /tmp/s2.png"}
    assert row["recorded"]["by"] == "owner"
    assert cli.main(check) == 0
    capsys.readouterr()
    assert cli.main([*check, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["verdict"] == "pass"
    assert cli.main(["check", "e" * 40, "--skip-smoke", "owner away", "--scenarios", str(scen_file), *base]) == 0


def test_record_needs_evidence_and_a_run(tmp_path):
    with pytest.raises(FileNotFoundError):
        results.record(results.result_path(tmp_path, REV, "ServerWorker"), "s.2", "pass", "x")
    path = _write(tmp_path)
    with pytest.raises(ValueError, match="evidence"):
        results.record(path, "s.2", "pass", "  ")
    with pytest.raises(KeyError):
        results.record(path, "nope", "pass", "x")


def test_import_copies_another_workers_file(tmp_path):
    src = tmp_path / "from-desktopworker.json"
    src.write_text(json.dumps({"revision": REV, "worker": "DesktopWorker", "rows": []}))
    dest_dir = tmp_path / "smoke"
    assert cli.main(["import", str(src), "--results-dir", str(dest_dir)]) == 0
    assert results.read(dest_dir / REV / "DesktopWorker.json")["worker"] == "DesktopWorker"
    src.write_text(json.dumps({"revision": REV, "worker": "DesktopWorker", "rows": [], "partial": True}))
    assert cli.main(["import", str(src), "--results-dir", str(dest_dir)]) == 1


def test_result_path_rejects_non_revisions(tmp_path):
    with pytest.raises(ValueError):
        results.result_path(tmp_path, "../etc", "ServerWorker")
    assert results.result_path(tmp_path, REV.upper(), "Gaming PC/x").name == "Gaming_PC_x.json"


def test_smoke_dir_resolution(monkeypatch, tmp_path):
    monkeypatch.setenv("AI_SHUFFLE_SMOKE_DIR", str(tmp_path / "a"))
    assert results.smoke_dir() == tmp_path / "a"
    assert results.smoke_dir(str(tmp_path / "b")) == tmp_path / "b"
    monkeypatch.delenv("AI_SHUFFLE_SMOKE_DIR")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    assert results.smoke_dir() == tmp_path / "data" / "smoke"


# -- the real client's wire behaviour ----------------------------------------


def test_http_client_sends_origin_and_no_credentials():
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.path.endswith("/projects"):
            return httpx.Response(401, json={"detail": "no"})
        return httpx.Response(200, json={"workers": []})
    client = HttpWorkerClient("http://127.0.0.1:8000", transport=httpx.MockTransport(handler))
    assert client.deploy_status() == {"workers": []}
    client.archive("s1")
    with pytest.raises(AuthRefused, match="passwordless local access"):
        client.projects()
    for req in seen:
        assert req.headers["origin"] == "http://127.0.0.1:8000"
        assert "authorization" not in req.headers and "cookie" not in req.headers
    assert seen[1].method == "PATCH" and json.loads(seen[1].content) == {"is_active": False, "archived": True}


def test_http_client_maps_errors():
    def boom(request):
        raise httpx.ConnectError("refused")
    with pytest.raises(Unreachable):
        HttpWorkerClient(transport=httpx.MockTransport(boom)).models()
    client = HttpWorkerClient(transport=httpx.MockTransport(lambda r: httpx.Response(409, text="busy")))
    with pytest.raises(ClientError, match="409"):
        client.compact("s1")


def test_http_client_reads_system_notes_up_to_caught_up():
    note = {"type": "system.note", "subtype": "compact_failed", "data": {"reason": "nothing"}}
    body = (
        'id: 1\nevent: turn.started\ndata: {"type": "turn.started"}\n\n'
        f"id: 2\nevent: system.note\ndata: {json.dumps(note)}\n\n"
        'event: caught_up\ndata: {"seq": 2}\n\n'
        'id: 3\nevent: system.note\ndata: {"subtype": "after"}\n\n'
    )
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})
    client = HttpWorkerClient(transport=httpx.MockTransport(handler))
    assert client.system_notes("s1") == [note]
    assert seen[0].url.path == "/api/v1/sessions/s1/events"


def _real_routes():
    from app.main import app

    def walk(routes):
        for route in routes:
            if hasattr(route, "effective_candidates"):  # FastAPI's included routers
                yield from walk(route.effective_candidates())
                yield from route.effective_low_priority_routes()
            else:
                yield route
    return list(walk(app.routes))


def _routed(routes, kind, method, path):
    from starlette.routing import Match

    scope = {"type": kind, "path": path, "method": method, "root_path": ""}
    return any(route.matches(scope)[0] == Match.FULL for route in routes)


def test_client_calls_exist_on_the_real_worker():
    """Every path the client calls is a route of the real app (a fake client cannot catch a 404/403)."""
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path))
        return httpx.Response(200, json={})
    client = HttpWorkerClient("http://127.0.0.1:8000", transport=httpx.MockTransport(handler))
    client.deploy_status()
    client.installs()
    client.models()
    client.projects()
    client.agents()
    client.preference("composer.profile")
    client.session("s1")
    client.state("s1")
    client.compact("s1")
    client.archive("s1")
    ws_path = urlsplit(client._ws_url()).path

    assert seen[0] == ("GET", "/api/v1/settings/application-updates/deploy-status")
    assert ("GET", "/api/v1/agents") in seen and ("GET", "/api/v1/preferences/composer.profile") in seen
    assert ws_path == "/api/v1/ws/chat"
    routes = _real_routes()
    assert not _routed(routes, "http", "GET", "/api/v1/application-updates/deploy-status")  # the old 404
    assert not _routed(routes, "websocket", "GET", "/ws/chat")  # the old 403
    missing = [f"{m} {p}" for m, p in seen if not _routed(routes, "http", m, p)]
    assert missing == []
    assert _routed(routes, "websocket", "GET", ws_path)


@pytest.fixture()
def ws_stub():
    from websockets.sync.server import serve

    frames, origins, paths = [], [], []

    def handler(ws):
        origins.append(ws.request.headers.get("Origin"))
        paths.append(ws.request.path)
        assert ws.request.headers.get("Authorization") is None
        for raw in ws:
            frame = json.loads(raw)
            frames.append(frame)
            if frame.get("prompt") == "refuse":
                ws.send(json.dumps({"type": "error", "message": "Session not found"}))
            elif frame.get("type") == "query" and not frame.get("session_id"):
                ws.send(json.dumps({"type": "ping"}))
                ws.send(json.dumps({"type": "session_created", "session_id": "new1",
                                    "request_id": frame["request_id"]}))

    server = serve(handler, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.socket.getsockname()[1]
    yield f"http://127.0.0.1:{port}", frames, origins, paths
    server.shutdown()


def test_ws_send_starts_session_with_origin(ws_stub):
    url, frames, origins, paths = ws_stub
    client = HttpWorkerClient(url, ack_timeout=5)
    client.linger_s = 0.2
    sid = client.send("hello", model="haiku", permission_mode="bypassPermissions", target_instance_id="dev_peer")
    assert sid == "new1"
    query = frames[0]
    assert query["type"] == "query" and query["prompt"] == "hello"
    assert query["overrides"] == {"model": "haiku", "permission_mode": "bypassPermissions"}
    assert query["target_instance_id"] == "dev_peer"
    assert {"type": "pong"} in frames
    assert origins == [url]
    assert paths == ["/api/v1/ws/chat"]
    with pytest.raises(ClientError, match="Session not found"):
        client.send("refuse", session_id="old")
    assert frames[-1]["session_id"] == "old"


def test_ws_follow_up_carries_the_agent(ws_stub):
    url, frames, _origins, _paths = ws_stub
    client = HttpWorkerClient(url, ack_timeout=5)
    client.linger_s = 0.2
    assert client.send("again", session_id="new1", model="sonnet", profile="coding") == "new1"
    assert frames[0]["session_id"] == "new1" and frames[0]["profile"] == "coding"

# Harness facts must not be inferred from the model's wording or tool choices.
class HarnessWorker(FakeWorker):
    def __init__(self, *, receipt="consumed", tools=("Swarm", "MessageSession"), **kw):
        super().__init__(answer=lambda _p: ("Arbitrary prose, no tool calls.", []), **kw)
        self.receipt = receipt
        self.tools = tools
        self.deliveries = []

    def tool_surface(self):
        return {"groups": [{"id": "harness", "available": True,
                            "tools": [{"name": t} for t in self.tools]}], "errors": []}

    def peer_message(self, sender_id, session_id, message, request_id):
        self.deliveries.append((sender_id, session_id, message, request_id))
        return {"session_id": session_id, "turn_id": "peer-turn-1", "delivery_status": self.receipt}


def _harness_rows(*ids):
    return _data(*(r for r in scenarios.rows(scenarios.load()) if r["id"] in ids))


def test_harness_rows_ignore_model_wording_and_tool_choice(tmp_path):
    worker = HarnessWorker()
    result, _ = _run(worker, _harness_rows("two-worker-smoke.2", "two-worker-smoke.9", "harness-basics.3"), tmp_path)
    assert all(r["status"] == "pass" for r in result["rows"])
    assert worker.deliveries == [("s3", "s2", "Live smoke r1: delivery probe; no reply required.", "r1-9")]
    evidence = result["rows"][1]["evidence"]["peer_delivery"]
    assert evidence["turn_id"] == "peer-turn-1" and evidence["delivery_status"] == "consumed"
    assert len(worker.archived) == 3


@pytest.mark.parametrize("receipt", ["queued", "begun", "stopped", "unknown"])
def test_peer_delivery_requires_consumed_receipt(tmp_path, receipt):
    data = _harness_rows("two-worker-smoke.9")
    data["suites"][0]["rows"][0]["steps"][-1]["within_s"] = 0
    result, _ = _run(HarnessWorker(receipt=receipt), data, tmp_path)
    assert result["rows"][0]["status"] == "fail"
    assert result["rows"][0]["evidence"]["peer_delivery"]["delivery_status"] == receipt


def test_catalog_requires_actual_available_tool_not_description(tmp_path):
    class BrokenCatalog(HarnessWorker):
        def tool_surface(self):
            return {"groups": [{"available": False, "tools": [{"name": "Swarm"}]},
                               {"available": True, "tools": [{"name": "Read", "description": "Swarm"}]}]}
    result, _ = _run(BrokenCatalog(), _harness_rows("harness-basics.3"), tmp_path)
    assert result["rows"][0]["status"] == "fail"
    assert "Swarm" in result["rows"][0]["reason"]


def test_harness_client_wire_contract():
    seen = []
    def handle(req):
        seen.append((req.method, req.url.path, json.loads(req.content) if req.content else None))
        return httpx.Response(200, json={"groups": []} if req.method == "GET" else {
            "session_id": "receiver", "turn_id": "turn", "delivery_status": "consumed"})
    client = HttpWorkerClient(transport=httpx.MockTransport(handle))
    try:
        assert client.tool_surface() == {"groups": []}
        assert client.peer_message("sender", "receiver", "probe", "stable-id")["delivery_status"] == "consumed"
    finally:
        client.close()
    assert seen == [("GET", "/api/v1/tools/surface", None),
                    ("POST", "/api/v1/tools/sessions/sender/peer-message",
                     {"session_id": "receiver", "message": "probe", "request_id": "stable-id"})]


@pytest.mark.parametrize("failure", ["missing_reply", "wrong_owner"])
def test_remote_start_checks_transcript_and_owner_not_wording(tmp_path, failure):
    class BrokenPeer(HarnessWorker):
        def session(self, sid):
            snapshot = super().session(sid)
            if failure == "missing_reply":
                snapshot["messages"] = [m for m in snapshot["messages"] if m["role"] == "user"]
            else:
                snapshot.update(is_remote=False, device_name="ServerWorker", instance_id="dev_self")
            return snapshot
    data = _harness_rows("two-worker-smoke.2")
    data["suites"][0]["rows"][0]["steps"][0]["within_s"] = 1
    result, _ = _run(BrokenPeer(), data, tmp_path)
    assert result["rows"][0]["status"] == "fail"


def test_catalog_only_row_needs_no_agent_or_model_turn(tmp_path):
    worker = HarnessWorker(default_agent=None, tools=("mcp__builtins__Swarm",))
    result, _ = _run(worker, _harness_rows("harness-basics.3"), tmp_path)
    assert result["rows"][0]["status"] == "pass"
    assert worker.sent == []


@pytest.mark.parametrize("bad_receipt", [
    {"session_id": "wrong", "turn_id": "turn", "delivery_status": "consumed"},
    {"session_id": "s1", "delivery_status": "consumed"},
    {"session_id": "s1", "turn_id": "changed", "delivery_status": "consumed"},
])
def test_peer_delivery_rejects_unrelated_or_unstable_receipts(tmp_path, bad_receipt):
    class BadReceipt(HarnessWorker):
        def peer_message(self, *args):
            self.deliveries.append(args)
            if len(self.deliveries) == 1:
                return {"session_id": "s1", "turn_id": "original", "delivery_status": "queued"}
            return bad_receipt
    worker = BadReceipt()
    result, _ = _run(worker, _harness_rows("two-worker-smoke.9"), tmp_path)
    assert result["rows"][0]["status"] == "fail"
    assert worker.deliveries[0] == worker.deliveries[1]


@pytest.mark.parametrize("missing_revision", [False, True])
def test_presence_row_records_and_requires_peer_revision(tmp_path, missing_revision):
    class PresenceWorker(HarnessWorker):
        def deploy_status(self):
            status = super().deploy_status()
            if missing_revision:
                status["workers"][1]["revision"] = None
            return status
    result, _ = _run(PresenceWorker(), _harness_rows("two-worker-smoke.1"), tmp_path)
    row = result["rows"][0]
    assert row["status"] == ("fail" if missing_revision else "pass")
    assert row["evidence"]["workers"][1]["revision"] == (None if missing_revision else PEER_REV)


@pytest.mark.parametrize("step", [
    {"op": "tool_catalog", "tools": []},
    {"op": "peer_delivery", "sender": "missing", "session": "a", "message": "probe", "request_id": "r"},
    {"op": "peer_delivery", "sender": "a", "session": "a", "request_id": "r"},
])
def test_new_harness_steps_validate_required_inputs(step):
    row = _script("t.1")
    row["steps"].append(step)
    with pytest.raises(scenarios.ScenarioError):
        scenarios.validate(_data(row))
