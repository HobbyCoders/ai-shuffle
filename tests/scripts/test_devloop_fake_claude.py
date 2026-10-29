"""The dev loop's fake ``claude`` CLI (scripts/devloop/fake_claude).

The shim is run as the app runs it: found on PATH, probed with ``auth
status``, and driven over stream-json. Hermetic: child processes and a
loopback stand-in for the app's compaction bridge, no network.
"""
from __future__ import annotations

import asyncio
import json
import os
import queue
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from scripts.devloop import fake_claude
from scripts.devloop import seed as seed_module
from scripts.devloop.fake_claude import cli as fake_cli

TIMEOUT = 20.0


# ─── PATH: the fake is found, the real CLI never is ─────────────────────────


@pytest.fixture
def real_bin(tmp_path: Path) -> Path:
    """A directory holding a "real" claude next to another program."""
    directory = tmp_path / "real-bin"
    directory.mkdir()
    for name in ("claude", "node"):
        (directory / name).write_text("#!/bin/sh\necho real\n")
        (directory / name).chmod(0o755)
    return directory


def test_the_instance_path_finds_the_shim_and_masks_the_real_cli(tmp_path, real_bin,
                                                                 monkeypatch):
    from app.core.provider import harness_adapter as ha

    instance = tmp_path / "instance"
    instance.mkdir()
    bin_dir = fake_claude.install(instance)
    path = fake_claude.instance_path(f"{real_bin}{os.pathsep}/usr/bin", bin_dir)
    entries = path.split(os.pathsep)
    assert entries[0] == str(bin_dir)
    assert str(real_bin) not in entries and "/usr/bin" in entries
    mirror = Path(entries[1])
    assert (mirror / "node").resolve() == (real_bin / "node").resolve()
    assert not (mirror / "claude").exists()

    monkeypatch.setenv("PATH", path)
    resolved = ha._resolve_claude_cmd()[0]
    assert resolved == str(bin_dir / "claude") and fake_claude.is_fake(resolved)
    assert not fake_claude.is_fake(real_bin / "claude")
    # A lost shim reads as "not installed", never as the real CLI.
    (bin_dir / "claude").unlink()
    with pytest.raises(ha.ClaudeCliMissingError):
        ha._resolve_claude_cmd()


def test_the_instance_home_launcher_is_the_fake_and_nothing_else_is_touched(tmp_path):
    bin_dir = fake_claude.install(tmp_path)
    link = fake_claude.cover_home(tmp_path / "home", bin_dir)
    assert link == tmp_path / "home" / ".local" / "bin" / "claude"
    assert fake_claude.is_fake(link)
    assert fake_claude.cover_home(tmp_path / "home", bin_dir) == link  # idempotent
    outside = tmp_path.parent / f"{tmp_path.name}-outside-home"
    assert fake_claude.cover_home(outside, bin_dir) is None and not outside.exists()


def test_the_cli_status_probe_reads_a_fake_sign_in(tmp_path):
    from app.core import claude_cli_status

    shim = fake_claude.install(tmp_path) / "claude"
    status = claude_cli_status.probe(resolver=lambda: str(shim))
    assert status.installed and status.signed_in is True and status.error is None
    assert status.cli_version == "2.1.312"
    assert status.email == fake_claude.FAKE_EMAIL and status.email.endswith(".invalid")
    log = [json.loads(line) for line in fake_claude.log_path(tmp_path).read_text().splitlines()]
    assert {"kind": "start"}.items() <= log[0].items()
    assert all("env" not in entry for entry in log)  # names only, never values


def test_it_refuses_anything_but_status_and_print_runs(tmp_path):
    shim = fake_claude.install(tmp_path) / "claude"
    for argv in (["auth", "login"], ["update"], ["setup-token"]):
        result = subprocess.run([str(shim), *argv], capture_output=True, text=True, check=False,
                                timeout=TIMEOUT)
        assert result.returncode == 2 and "never signs in" in result.stderr


# ─── stream-json ────────────────────────────────────────────────────────────


class Cli:
    """The shim as the delegated loop spawns it, in a dev-instance layout."""

    def __init__(self, root: Path, *args: str, env: dict | None = None,
                 hooks: dict | None = None) -> None:
        self.root = root
        (root / "home").mkdir(exist_ok=True)
        (root / "ws").mkdir(exist_ok=True)
        shim = fake_claude.install(root) / "claude"
        self.proc = subprocess.Popen(
            [str(shim), "-p", "--input-format", "stream-json", "--output-format",
             "stream-json", "--verbose", *args],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=root / "ws", env={"PATH": os.environ["PATH"], "HOME": str(root / "home"),
                                  **(env or {})})
        self.frames: queue.Queue = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()
        self.send({"type": "control_request", "request_id": "init", "request": {
            "subtype": "initialize", "hooks": hooks or {}}})
        self.init = self.until(lambda f: f.get("subtype") == "init")[-1]

    def _pump(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self.frames.put(json.loads(line))
        self.frames.put(None)

    def send(self, frame: dict) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write((json.dumps(frame) + "\n").encode())
        self.proc.stdin.flush()

    def user(self, text: str) -> None:
        self.send({"type": "user", "message": {"role": "user",
                                               "content": [{"type": "text", "text": text}]}})

    def until(self, predicate, answer=None) -> list[dict]:
        """Frames up to the first that matches ``predicate`` (stream events skipped).
        ``answer(frame)`` may reply to a control request on the way."""
        seen = []
        while True:
            frame = self.frames.get(timeout=TIMEOUT)
            assert frame is not None, f"the CLI exited: {self.stderr()}"
            if frame.get("type") == "stream_event":
                continue
            seen.append(frame)
            if answer is not None and frame.get("type") == "control_request":
                answer(self, frame)
            if predicate(frame):
                return seen

    def turn(self, text: str, answer=None) -> list[dict]:
        self.user(text)
        return self.until(lambda f: f.get("type") == "result", answer)

    def stderr(self) -> str:
        if self.proc.poll() is None:
            return ""
        assert self.proc.stderr is not None
        return self.proc.stderr.read().decode()

    def close(self) -> int:
        assert self.proc.stdin is not None
        self.proc.stdin.close()
        return self.proc.wait(TIMEOUT)


def _texts(frames: list[dict], kind: str = "assistant") -> list[str]:
    return [b.get("text", "") for f in frames if f.get("type") == kind
            for b in (f["message"]["content"] if isinstance(f["message"]["content"], list)
                      else [{"text": f["message"]["content"]}])
            if isinstance(b, dict) and b.get("type", "text") == "text"]


def _results(frames: list[dict]) -> list[dict]:
    return [b for f in frames if f.get("type") == "user" and isinstance(f["message"]["content"], list)
            for b in f["message"]["content"] if b.get("type") == "tool_result"]


def _reply(decision: dict):
    def answer(cli: Cli, frame: dict) -> None:
        if frame["request"]["subtype"] in ("can_use_tool", "hook_callback"):
            cli.send({"type": "control_response", "response": {
                "subtype": "success", "request_id": frame["request_id"],
                "response": decision}})
    return answer


def test_a_plain_prompt_gets_the_default_echo(tmp_path):
    cli = Cli(tmp_path)
    assert cli.init["claude_code_version"] == "2.1.312" and "Bash" in cli.init["tools"]
    frames = cli.turn("Hello there <system-reminder>hidden</system-reminder>")
    assert "You said: “Hello there”" in _texts(frames)[0]
    assert frames[-1]["subtype"] == "success" and not frames[-1]["is_error"]
    assert cli.close() == 0


def test_context_usage_reports_the_window_the_app_pins(tmp_path):
    """No spurious "context window mismatch": the pin is honoured, as by the CLI."""
    cli = Cli(tmp_path, env={"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "1000000"})
    cli.send({"type": "control_request", "request_id": "usage",
              "request": {"subtype": "get_context_usage"}})
    answer = cli.until(lambda f: (f.get("response") or {}).get("request_id") == "usage")[-1][
        "response"]["response"]
    assert answer["maxTokens"] == answer["rawMaxTokens"] == 1_000_000
    assert answer["autocompactSource"] == "env"
    cli.close()


def test_a_tool_asks_for_permission_and_a_deny_is_the_tool_error(tmp_path):
    cli = Cli(tmp_path)
    frames = cli.turn("[script:tool-bash]", _reply({"behavior": "allow"}))
    asks = [f for f in frames if f.get("type") == "control_request"]
    assert [a["request"]["tool_name"] for a in asks] == ["Bash"]
    result = _results(frames)[0]
    assert result["content"][0]["text"] == "hello from the fake claude cli"
    assert not result["is_error"]
    assert frames[-1]["num_turns"] == 2

    frames = cli.turn("[script:tool-bash]", _reply({"behavior": "deny", "message": "No."}))
    result = _results(frames)[0]
    assert result["is_error"] and result["content"][0]["text"] == "No."
    cli.close()


def test_bypass_mode_runs_without_asking_and_disallowed_tools_never_run(tmp_path):
    cli = Cli(tmp_path, "--permission-mode", "bypassPermissions",
              "--disallowed-tools", "ToolSearch,Bash")
    assert "Bash" not in cli.init["tools"] and "ToolSearch" not in cli.init["tools"]
    frames = cli.turn("[script:tool-bash]")
    assert not [f for f in frames if f.get("type") == "control_request"]
    assert not _results(frames)
    assert "--disallowed-tools removes" in _texts(frames)[0]
    cli.close()


def test_a_pre_tool_use_hook_deny_wins(tmp_path):
    hooks = {"PreToolUse": [{"matcher": None, "hookCallbackIds": ["fluid_PreToolUse"]}]}
    cli = Cli(tmp_path, hooks=hooks)
    frames = cli.turn("[script:tool-bash]", _reply({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny",
        "permissionDecisionReason": "Blocked by policy."}}))
    hooks_asked = [f["request"] for f in frames if f.get("type") == "control_request"]
    assert [h["subtype"] for h in hooks_asked] == ["hook_callback"]
    assert hooks_asked[0]["input"]["tool_name"] == "Bash"
    result = _results(frames)[0]
    assert result["is_error"] and result["content"][0]["text"] == "Blocked by policy."
    cli.close()


def test_a_long_mcp_result_is_persisted_as_a_stub(tmp_path):
    cli = Cli(tmp_path, "--permission-mode", "bypassPermissions")
    frames = cli.turn("[script:long-result]")
    text = _results(frames)[0]["content"][0]["text"]
    assert text.startswith("<persisted-output>") and len(text) < 3_000
    saved = Path(text.split("saved to: ", 1)[1].split("\n", 1)[0])
    assert saved.is_file() and len(saved.read_text()) == 60_000
    assert saved.is_relative_to(tmp_path / "home" / ".claude")
    cli.close()


def test_api_errors_and_a_crash(tmp_path):
    cli = Cli(tmp_path)
    frames = cli.turn("[script:error-500]")
    assert frames[-1]["is_error"] and frames[-1]["result"].startswith("API Error: 500")
    cli.user("[script:exit-error]")
    assert cli.proc.wait(TIMEOUT) == 1
    assert "scripted crash" in cli.stderr()


def test_resume_continues_a_known_session_and_refuses_an_unknown_one(tmp_path):
    cli = Cli(tmp_path)
    cli.turn("First turn.")
    sid = cli.init["session_id"]
    cli.close()
    again = Cli(tmp_path, "--resume", sid)
    assert again.init["session_id"] == sid
    again.close()

    shim = fake_claude.install(tmp_path) / "claude"
    refused = subprocess.run([str(shim), "-p", "--resume", "no-such-session"],
                             capture_output=True, text=True, check=False, timeout=TIMEOUT,
                             cwd=tmp_path / "ws",
                             env={"PATH": os.environ["PATH"], "HOME": str(tmp_path / "home")})
    assert refused.returncode == 1 and "No conversation found" in refused.stderr


def test_nothing_is_written_outside_the_dev_instance(tmp_path):
    """HOME outside FAKE_CLAUDE_ROOT: no transcript, and --resume cannot work."""
    outside = tmp_path / "outside-home"
    outside.mkdir()
    instance = tmp_path / "instance"
    instance.mkdir()
    cli = Cli(instance, env={"HOME": str(outside)})
    cli.turn("Hello.")
    cli.close()
    assert not any(outside.rglob("*"))


# ─── compaction ─────────────────────────────────────────────────────────────


class Bridge:
    """A stand-in for the app's mod bridge: records posts, answers ``reply(body)``."""

    def __init__(self, reply) -> None:
        self.posts: list[tuple[dict, str]] = []
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["content-length"])))
                bridge.posts.append((body, self.headers.get("authorization", "")))
                data = json.dumps(reply(body)).encode()
                self.send_response(200)
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        origin = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.env = {"FLUID_CLAUDE_MOD_URL": f"{origin}/claude-mod/usage",
                    "FLUID_CLAUDE_MOD_COMPACT_URL": f"{origin}/claude-mod/compact",
                    "FLUID_CLAUDE_MOD_TOKEN": "bridge-token"}

    def close(self) -> None:
        self.server.shutdown()


def _plugin(tmp_path: Path) -> str:
    manifest = tmp_path / "mod" / ".claude-plugin" / "plugin.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"name": "ai-shuffle"}))
    return str(tmp_path / "mod")


def _keep_last_exchange(body: dict) -> dict:
    rows = body["messages"]
    start = max(i for i, r in enumerate(rows) if r["role"] == "user" and not r.get("toolResults"))
    return {"messages": [{k: v for k, v in r.items() if k != "handle"} for r in rows[start:]]}


def test_compact_posts_to_the_bridge_then_replays_the_answer_with_fresh_uuids(tmp_path):
    bridge = Bridge(_keep_last_exchange)
    cli = Cli(tmp_path, "--permission-mode", "bypassPermissions",
              "--plugin-dir", _plugin(tmp_path), env=bridge.env)
    try:
        cli.turn("An early question.")
        cli.turn("[script:tool-bash]")
        frames = cli.turn("/compact")
    finally:
        cli.close()
        bridge.close()
    (body, auth), = bridge.posts
    assert auth == "Bearer bridge-token" and body["v"] == 1 and body["trigger"] == "manual"
    handles = [row["handle"] for row in body["messages"]]
    assert len(handles) == len(set(handles)) == 7  # 2 prompts, 2 texts, call, result, answer
    statuses = [f.get("status") for f in frames if f.get("subtype") == "status"]
    assert statuses == ["compacting", None]
    boundary = next(f for f in frames if f.get("subtype") == "compact_boundary")
    assert boundary["logical_parent_uuid"] == handles[-1]
    replayed = [f for f in frames[frames.index(boundary) + 1:] if f["type"] in ("user", "assistant")]
    assert [f["type"] for f in replayed] == ["user", "assistant", "assistant", "user", "assistant"]
    assert not {f["uuid"] for f in replayed} & set(handles)
    assert frames[-1]["type"] == "result" and frames[-1]["result"] == ""


def test_a_skipped_manual_compaction_says_so_and_no_bridge_means_the_summarizer(tmp_path):
    bridge = Bridge(lambda body: {"skip": "nothing more to compact"})
    (tmp_path / "a").mkdir()
    cli = Cli(tmp_path / "a", "--plugin-dir", _plugin(tmp_path), env=bridge.env)
    try:
        cli.turn("Hello.")
        frames = cli.turn("/compact")
    finally:
        cli.close()
        bridge.close()
    assert _texts(frames)[-1] == "Not compacted · nothing more to compact"
    assert not [f for f in frames if f.get("subtype") == "compact_boundary"]

    (tmp_path / "b").mkdir()
    cli = Cli(tmp_path / "b")
    cli.turn("Hello.")
    frames = cli.turn("[script:compact]")
    cli.close()
    boundary = next(f for f in frames if f.get("subtype") == "compact_boundary")
    assert boundary["compact_metadata"]["trigger"] == "auto"
    # As Claude Code 2.1.312: the last message before the compaction.
    assert boundary["logical_parent_uuid"]
    assert "compacted mid-turn" in _texts(frames)[-1]
    transcript, = (tmp_path / "b" / "home" / ".claude" / "projects").glob("*/*.jsonl")
    entries = [json.loads(line) for line in transcript.read_text().splitlines()]
    summary = entries[[e.get("subtype") for e in entries].index("compact_boundary") + 1]
    assert summary["isCompactSummary"] and "Hello." in summary["message"]["content"]


COMPACTION_HOOKS = {event: [{"matcher": None, "hookCallbackIds": [f"fluid_{event}"]}]
                    for event in ("PreCompact", "SessionStart")}


def _printed(frames: list[dict]) -> list[tuple]:
    """What the CLI printed (and asked) around a compaction, in the
    fixtures' vocabulary."""
    shape = []
    for f in frames:
        kind = f.get("type")
        if kind == "control_request" and f["request"].get("subtype") == "hook_callback":
            shape.append(("hook", f["request"]["input"]["hook_event_name"]))
        elif kind == "system" and f.get("subtype") in ("status", "compact_boundary"):
            shape.append((kind, f["subtype"], f.get("status"), f.get("compact_result")))
        elif kind in ("user", "assistant", "result"):
            shape.append((kind,))
    return shape


def _listed(fixture: dict, replayed: int) -> list[tuple]:
    """The same for what the fixture lists (``replayed``: rows of the answer)."""
    shape = []
    for step in fixture["sequence"]:
        if "hook" in step:
            shape.append(("hook", step["hook"]))
        elif "frame" in step:
            frame = step["frame"]
            if frame["type"] == "system":
                shape.append(("system", frame["subtype"], frame.get("status"),
                              frame.get("compact_result")))
            else:
                shape.append((frame["type"],))
        elif step.get("replay") == "answer":
            shape += [("replayed",)] * replayed
        elif "result" in step:
            shape.append(("result",))
    return shape


@pytest.mark.parametrize("trigger, outcome", [
    ("auto", "fluid"), ("manual", "fluid"), ("auto", "summarizer"), ("manual", "summarizer"),
    ("manual", "skip"),
])
def test_the_fake_prints_each_compaction_as_its_replay_fixture_lists_it(tmp_path, trigger,
                                                                        outcome):
    """The fixtures are the ground truth (derived from Claude Code 2.1.312);
    the fake must print exactly their sequence."""
    answer = {"fluid": _keep_last_exchange, "skip": lambda body: {"skip": "nothing left"},
              "summarizer": lambda body: {"fallback": "nothing left"}}[outcome]
    bridge = Bridge(answer)
    cli = Cli(tmp_path, "--permission-mode", "bypassPermissions", "--plugin-dir",
              _plugin(tmp_path), env=bridge.env, hooks=COMPACTION_HOOKS)
    try:
        cli.turn("An early question.")
        cli.turn("[script:tool-bash]")
        frames = cli.turn("/compact" if trigger == "manual" else "[script:compact]",
                          _reply({}))
    finally:
        cli.close()
        bridge.close()
    replayed = len(answer(bridge.posts[0][0])["messages"]) if outcome == "fluid" else 0
    expected = _listed(fake_cli.load_compaction(trigger, outcome), replayed)
    printed = _printed(frames)[:len(expected)]
    boundary = next((i for i, f in enumerate(frames) if f.get("subtype") == "compact_boundary"),
                    None)
    if replayed:
        # The replay: the answer's rows as user/assistant frames, right after the boundary.
        at = expected.index(("replayed",))
        assert printed[at:at + replayed] == [(f["type"],) for f in
                                             frames[boundary + 1:boundary + 1 + replayed]]
        printed[at:at + replayed] = [("replayed",)] * replayed
    assert printed == expected
    if boundary is not None:
        metadata = frames[boundary]["compact_metadata"]
        assert metadata["trigger"] == trigger
        assert metadata["pre_tokens"] > 0 and metadata["post_tokens"] > 0
        assert {"cumulative_dropped_tokens", "duration_ms"} <= metadata.keys()
        assert ("preserved_messages" in metadata) == (outcome == "summarizer")
        assert frames[boundary]["logical_parent_uuid"]
    tail = [f["type"] for f in frames[len(expected):]]
    if trigger == "auto":  # the turn goes on: a model call answers
        assert tail == ["assistant", "result"]
    else:  # a typed /compact makes no model call; a skip printed its own result
        assert tail == ([] if outcome == "skip" else ["result"])
        assert frames[-1]["num_turns"] == 0


def test_a_summarized_conversation_resumes_with_its_kept_rows(tmp_path):
    """--resume after Claude Code's summarizer: the summary, then the kept
    rows (looked up by uuid), then what came after; nothing is printed again."""
    cli = Cli(tmp_path, "--permission-mode", "bypassPermissions")
    cli.turn("An early question.")
    cli.turn("[script:tool-bash]")
    frames = cli.turn("/compact")
    sid = cli.init["session_id"]
    cli.close()
    boundary = next(f for f in frames if f.get("subtype") == "compact_boundary")
    kept = boundary["compact_metadata"]["preserved_messages"]["uuids"]
    assert kept and boundary["compact_metadata"]["preserved_messages"]["anchor_uuid"]
    again = Cli(tmp_path, "--resume", sid)
    resumed = again.turn("After the restart.")
    again.close()
    assert [f["type"] for f in resumed if f["type"] != "control_response"] == [
        "assistant", "result"]
    log = [json.loads(line) for line in fake_claude.log_path(tmp_path).read_text().splitlines()]
    init = [entry for entry in log if entry["kind"] == "init"][-1]
    assert init["resumed"] and init["rows"] == 1 + len(kept)


# ─── the delegated loop drives it ───────────────────────────────────────────


async def test_the_delegated_loop_runs_a_tool_turn_through_the_shim(tmp_path):
    from fluid.engine.delegated import DelegatedLoop
    from fluid.events import (
        PermissionRequest,
        RespondPermission,
        ToolResult,
        TurnComplete,
        UserTurn,
    )
    from fluid.store.base import MemoryStore

    (tmp_path / "home").mkdir()
    shim = fake_claude.install(tmp_path) / "claude"
    store = MemoryStore()
    loop = DelegatedLoop(store=store, model="sonnet", claude_cmd=[str(shim)],
                         env={"HOME": str(tmp_path / "home")}, cwd=str(tmp_path))
    sid = (await store.create_session()).session_id
    loop._session_id = sid
    seen: list = []

    async def watch() -> None:
        async for event in loop.bus.subscribe(sid, since_seq=loop.bus.last_seq(sid)):
            seen.append(event)
            if isinstance(event, PermissionRequest):
                await loop.submit(RespondPermission(request_id=event.request_id,
                                                    decision="allow"))
            if isinstance(event, TurnComplete):
                return

    try:
        watcher = asyncio.create_task(watch())
        await asyncio.sleep(0)
        await loop.submit(UserTurn(content="[script:tool-bash]"))
        await asyncio.wait_for(watcher, TIMEOUT)
        await asyncio.wait_for(loop.wait_turn(), TIMEOUT)
    finally:
        await loop.aclose()
    results = [e for e in seen if isinstance(e, ToolResult)]
    assert [r.is_error for r in results] == [False]
    assert loop.cli_session_id and loop.cli_version == "2.1.312 (Claude Code)"


# ─── seed ───────────────────────────────────────────────────────────────────


def test_the_claude_code_agent_is_seeded_only_beside_the_fake(tmp_path, real_bin, monkeypatch):
    from app.core.provider import harness_adapter as ha

    monkeypatch.setattr(ha, "_resolve_claude_cmd", lambda **_: [str(real_bin / "claude")])
    result = asyncio.run(seed_module.seed_claude_cli())
    assert "is not the dev loop's fake" in result["skipped"]

    shim = fake_claude.install(tmp_path) / "claude"
    monkeypatch.setattr(ha, "_resolve_claude_cmd", lambda **_: [str(shim)])
    assert seed_module._fake_claude_cli() == str(shim)
