"""The dev loop's fake ``claude``: what the shim from :func:`fake_claude.install` runs.

    claude --version                 the version text (a current 2.1.x)
    claude auth --help | status      signed in as an obviously fake account
    claude -p --input-format stream-json --output-format stream-json ...
                                     plays turns picked by the prompt

It speaks the stream-json protocol the app's ``DelegatedLoop`` drives, on top
of the scripted fake the fluid tests use (``fluid/tests/fixtures/
fake_claude_script.py``), and adds what a dev instance needs from a real CLI:

* ``[script:NAME]`` in the prompt plays a scenario: this package's
  ``scenarios/NAME.json`` first, then the fake AI's
  (``scripts/devloop/scenarios``), in the same format. No tag: ``default``.
* The hooks the app registers at ``initialize`` are called the way the CLI
  calls them (UserPromptSubmit, PreToolUse, PostToolUse, PreCompact,
  SessionStart on compaction, Stop), and a PreToolUse deny is obeyed.
* Tools: a PreToolUse allow, the CLI's own rules (``--allowed-tools``, the
  permission mode, read-only tools) or a ``can_use_tool`` answer from the app
  decide; ``--disallowed-tools`` removes a tool from the model entirely. The
  stdio MCP servers in ``--mcp-config`` (the app's bundled bridge) are really
  connected and listed in the init frame, and a scenario call to one of their
  tools without a scripted ``result`` really runs there. Nothing else runs: a
  built-in tool's output is the scenario's ``result`` (or ``result_chars``
  characters of filler). An MCP result over 50,000 characters is saved to a
  file and replaced by a ``<persisted-output>`` stub, as Claude Code 2.1 does.
* ``/compact`` (manual) and a step with ``"compact": true`` (automatic, mid
  turn) compact the way Claude Code 2.1.312 with AI Shuffle's mod does, frame
  for frame and in its order, as the replay fixtures in ``compaction/`` list
  it (derived from the real CLI: docs/internal/claude-code-compaction-
  protocol.md): post the conversation to the app's compaction route, then
  SessionStart, the status, the boundary, and the answer replayed as ordinary
  frames with fresh uuids (#679). Without the route, or when the app falls
  back, Claude Code's summarizer stands in (a fake summary that keeps the
  newest rows); a manual compaction the app skips is left as it is.
* A transcript per session under the CLI config dir (``<config>/projects/
  <slug>/<id>.jsonl``), so ``--resume`` works across app restarts and an
  unknown id is refused with exit 1, as the real CLI does. It is written only
  inside the dev instance (``FAKE_CLAUDE_ROOT``); anywhere else nothing is
  kept.

No model, no credential, no network: the only socket it opens is the app's
loopback compaction bridge. Every run is logged to ``FAKE_CLAUDE_LOG``.
"""
from __future__ import annotations

import fnmatch
import http.client
import importlib.util
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.devloop import fake_ai
from scripts.devloop.fake_claude import COMPACTION_DIR, FAKE_EMAIL, SCENARIOS_DIR

FIXTURE = ROOT / "fluid" / "tests" / "fixtures" / "fake_claude_script.py"
VERSION = "2.1.312 (Claude Code)"
MODEL = "claude-sonnet-4-5"
BUILTIN_TOOLS = ("Task", "Bash", "Glob", "Grep", "ExitPlanMode", "Read", "Edit", "Write",
                 "NotebookEdit", "WebFetch", "TodoWrite", "WebSearch", "BashOutput", "KillShell",
                 "ToolSearch")
#: What Claude Code runs without asking in the default mode.
READ_ONLY = {"Read", "Glob", "Grep", "TodoWrite", "ToolSearch", "BashOutput"}
#: Claude Code 2.1 saves an MCP tool result longer than this to a file.
PERSIST_OVER = 50_000
PREVIEW_CHARS = 2_000
HOOK_TIMEOUT = 120.0
MCP_START_TIMEOUT = 20.0
MCP_CALL_TIMEOUT = 300.0
BRIDGE_TIMEOUT = 60.0
_COMPACT = re.compile(r"^/compact(?:\s+(.*))?$", re.DOTALL)


def _load_fixture() -> Any:
    spec = importlib.util.spec_from_file_location("fake_claude_script", FIXTURE)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


script = _load_fixture()
_flag = script._flag


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _new_uuid() -> str:
    return str(uuid.uuid4())


def log(kind: str, **fields: Any) -> None:
    """One JSON line in ``FAKE_CLAUDE_LOG`` (never values of the environment)."""
    path = os.environ.get("FAKE_CLAUDE_LOG")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(json.dumps({"ts": time.time(), "pid": os.getpid(), "kind": kind,
                                     **fields}, default=str) + "\n")
    except OSError:
        pass


def _inside_root(path: Path) -> bool:
    root = os.environ.get("FAKE_CLAUDE_ROOT")
    if not root:
        return False
    try:
        path.resolve().relative_to(Path(root).resolve())
        return True
    except ValueError:
        return False


# ─── scenarios ──────────────────────────────────────────────────────────────


def scenario_names() -> list[str]:
    return sorted({p.stem for d in (SCENARIOS_DIR, fake_ai.SCENARIOS_DIR)
                   for p in d.glob("*.json")})


def load_scenario(name: str) -> dict | None:
    """``NAME.json`` from this package, else from the fake AI's scenarios."""
    for directory in (SCENARIOS_DIR, fake_ai.SCENARIOS_DIR):
        path = directory / f"{name}.json"
        if path.is_file():
            return fake_ai.validate_scenario(name, json.loads(path.read_text(encoding="utf-8")))
    return None


def filler(chars: int) -> str:
    """``chars`` characters of numbered lines (deterministic)."""
    lines, size, number = [], 0, 0
    while size < chars:
        number += 1
        line = f"{number:06d} fake output line for the long tool result scenario"
        lines.append(line)
        size += len(line) + 1
    return "\n".join(lines)[:chars]


# ─── compaction fixtures (what the real CLI prints, in order) ───────────────

_PLACEHOLDER = re.compile(r"\$([a-z_]+)")
#: Bound afresh at every use.
_FRESH = {"uuid": _new_uuid, "now": _now,
          "message_id": lambda: "msg_fake_" + uuid.uuid4().hex[:20]}


def load_compaction(trigger: str, outcome: str) -> dict:
    """``compaction/<trigger>-<outcome>.json``: one compaction as Claude Code
    prints it (``outcome``: ``fluid``, ``summarizer`` or ``skip``)."""
    return json.loads((COMPACTION_DIR / f"{trigger}-{outcome}.json").read_text(encoding="utf-8"))


def split_at_bridge(fixture: dict) -> tuple[list[dict], list[dict]]:
    """The steps before the mod's compaction hook runs, and the rest."""
    steps = fixture["sequence"]
    for at, step in enumerate(steps):
        if step.get("bridge"):
            return steps[:at], steps[at + 1:]
    return [], steps


def bind(value: Any, ctx: dict[str, Any]) -> Any:
    """``value`` with its ``$name`` placeholders filled from ``ctx``: a string
    that is one placeholder takes the value itself (a number, a list, null),
    others take its text. A kept-messages field is left out when nothing was
    kept, as the CLI leaves it out when the summary covers everything."""
    if isinstance(value, dict):
        out = {k: bind(v, ctx) for k, v in value.items()}
        if not ctx.get("preserved_uuids"):
            for key in [k for k in out if k.lower().replace("_", "").startswith("preserved")]:
                del out[key]
        return out
    if isinstance(value, list):
        return [bind(v, ctx) for v in value]
    if not isinstance(value, str) or "$" not in value:
        return value

    def lookup(name: str) -> Any:
        return _FRESH[name]() if name in _FRESH else ctx.get(name)

    whole = _PLACEHOLDER.fullmatch(value)
    if whole:
        return lookup(whole.group(1))
    return _PLACEHOLDER.sub(lambda m: str(lookup(m.group(1)) or ""), value)


# ─── MCP (stdio servers from --mcp-config) ──────────────────────────────────


class McpServer:
    """One stdio MCP server, as Claude Code connects it: initialize, list, call."""

    def __init__(self, name: str, entry: dict) -> None:
        self.name, self.entry = name, entry
        self.tools: list[str] = []
        self.status, self.error = "pending", None
        self.proc: subprocess.Popen | None = None
        self._next = 0
        self._replies: dict[int, dict] = {}
        self._lock = threading.Condition()
        self.list_changed = False

    def start(self) -> None:
        try:
            if self.entry.get("type", "stdio") != "stdio":
                raise RuntimeError("the dev loop's fake CLI connects stdio servers only")
            env = {**os.environ, **{k: str(v) for k, v in (self.entry.get("env") or {}).items()}}
            self.proc = subprocess.Popen(
                [self.entry["command"], *self.entry.get("args", [])], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env,
                cwd=self.entry.get("cwd") or os.getcwd())
            threading.Thread(target=self._read, daemon=True).start()
            self.request("initialize", {
                "protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "claude-code", "version": VERSION.split()[0]}},
                MCP_START_TIMEOUT)
            self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            self.refresh()
            self.status = "connected"
        except Exception as exc:  # noqa: BLE001 — the init frame reports it
            self.status, self.error = "failed", f"{type(exc).__name__}: {exc}"
            self.close()

    def refresh(self) -> None:
        tools, cursor = [], None
        while True:
            result = self.request("tools/list", {"cursor": cursor} if cursor else {},
                                  MCP_START_TIMEOUT)
            tools += [t["name"] for t in result.get("tools") or [] if isinstance(t, dict)]
            cursor = result.get("nextCursor")
            if not cursor:
                break
        self.tools, self.list_changed = tools, False

    def _send(self, message: dict) -> None:
        assert self.proc is not None and self.proc.stdin is not None
        self.proc.stdin.write((json.dumps(message) + "\n").encode())
        self.proc.stdin.flush()

    def _read(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        for line in self.proc.stdout:
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if not isinstance(message, dict):
                continue
            if "id" in message and "method" in message:  # a server request (ping, roots)
                self._send({"jsonrpc": "2.0", "id": message["id"], "result": {}})
            elif message.get("method") == "notifications/tools/list_changed":
                self.list_changed = True
            elif "id" in message:
                with self._lock:
                    self._replies[message["id"]] = message
                    self._lock.notify_all()
        with self._lock:
            self._lock.notify_all()

    def request(self, method: str, params: dict, timeout: float) -> dict:
        with self._lock:
            self._next += 1
            number = self._next
        self._send({"jsonrpc": "2.0", "id": number, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        with self._lock:
            while number not in self._replies:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or (self.proc is not None and self.proc.poll() is not None):
                    raise TimeoutError(f"{self.name}: no answer to {method}")
                self._lock.wait(min(remaining, 0.5))
            reply = self._replies.pop(number)
        if "error" in reply:
            raise RuntimeError(f"{self.name}: {method}: {reply['error']}")
        return reply.get("result") or {}

    def call(self, tool: str, arguments: dict) -> tuple[str, bool]:
        result = self.request("tools/call", {"name": tool, "arguments": arguments},
                              MCP_CALL_TIMEOUT)
        parts = [str(block.get("text", "")) if block.get("type") == "text"
                 else f"[{block.get('type')} block]"
                 for block in result.get("content") or [] if isinstance(block, dict)]
        return "\n".join(parts), bool(result.get("isError"))

    def close(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()


def connect_mcp(path: str | None) -> dict[str, McpServer]:
    if not path:
        return {}
    try:
        config = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        try:  # --mcp-config also takes the JSON itself
            config = json.loads(path)
        except ValueError:
            return {}
    servers = {name: McpServer(name, entry)
               for name, entry in (config.get("mcpServers") or {}).items()
               if isinstance(entry, dict)}
    threads = [threading.Thread(target=s.start, daemon=True) for s in servers.values()]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(MCP_START_TIMEOUT + 5)
    return servers


# ─── the CLI ────────────────────────────────────────────────────────────────


class BridgeError(Exception):
    pass


class DevClaude(script.Fake):
    """The fixture's protocol plumbing, playing dev-loop scenarios."""

    def __init__(self) -> None:
        super().__init__({})
        self.record_path = None  # the fixture's recorder; this CLI logs to FAKE_CLAUDE_LOG
        self.model = self.model or MODEL
        self.resume = _flag("--resume")
        self.sid = self.resume or _new_uuid()
        self.disallowed = [p for p in (_flag("--disallowed-tools") or "").split(",") if p]
        self.hooks: dict[str, list[str]] = {}
        self.rows: list[dict] = []  # the conversation as Claude Code holds it
        self.last_uuid: str | None = None
        self.message_id: str | None = None
        self.counter = 0
        self.model_calls = 0
        self.dropped_tokens = 0  # what compaction removed so far (cumulative_dropped_tokens)
        self.mcp: dict[str, McpServer] = {}
        self.transcript = self._transcript_path()
        self.mod_loaded = False

    # ── transcript ──
    def _config_dir(self) -> Path:
        return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")

    def _transcript_path(self) -> Path | None:
        if any(c in self.sid for c in "/\\:") or ".." in self.sid:
            return None
        directory = (self._config_dir() / "projects"
                     / re.sub(r"[^A-Za-z0-9]", "-", os.getcwd()))
        return directory / f"{self.sid}.jsonl" if _inside_root(directory) else None

    def _write(self, entry: dict) -> None:
        if self.transcript is None:
            return
        self.transcript.parent.mkdir(parents=True, exist_ok=True)
        with self.transcript.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"sessionId": self.sid, "cwd": os.getcwd(),
                                     "timestamp": _now(), **entry}) + "\n")

    def _restore(self) -> None:
        """The rows after the last compaction, from the transcript, as the CLI
        loads them: the summary, then the kept rows (``preservedMessages``,
        looked up by uuid), then what came after."""
        assert self.transcript is not None
        rows: list[dict] = []
        by_uuid: dict[str, dict] = {}
        kept: list[dict] = []
        for line in self.transcript.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if entry.get("type") == "system" and entry.get("subtype") == "compact_boundary":
                rows = []
                preserved = (entry.get("compactMetadata") or {}).get("preservedMessages") or {}
                kept = [by_uuid[u] for u in preserved.get("uuids") or [] if u in by_uuid]
                self.last_uuid = entry.get("uuid") or self.last_uuid
                self.dropped_tokens = int((entry.get("compactMetadata") or {}).get(
                    "cumulativeDroppedTokens") or self.dropped_tokens)
            elif entry.get("type") == "user" and entry.get("isCompactSummary"):
                content = (entry.get("message") or {}).get("content")
                rows = [{"role": "user", "text": content if isinstance(content, str) else "",
                         "toolUses": [], "handle": entry.get("uuid")}, *kept]
                self.last_uuid = (kept[-1]["handle"] if kept else entry.get("uuid"))
            elif isinstance(entry.get("fakeRow"), dict):
                rows.append(entry["fakeRow"])
                by_uuid[entry.get("uuid")] = entry["fakeRow"]
                self.last_uuid = entry.get("uuid") or self.last_uuid
        self.rows = rows

    def _add(self, kind: str, message: dict, row: dict, **extra: Any) -> str:
        """Keep a frame's row (handle = its uuid) and write its transcript entry."""
        handle = _new_uuid()
        row = {**row, "handle": handle}
        self.rows.append(row)
        self._write({"type": kind, "uuid": handle, "parentUuid": self.last_uuid,
                     "message": message, "fakeRow": row, **extra})
        self.last_uuid = handle
        return handle

    # ── protocol ──
    def answer_control(self, msg: dict) -> None:
        request = msg.get("request") or {}
        subtype = request.get("subtype")
        if subtype == "initialize":
            self.hooks = {event: [cid for matcher in matchers or [] if isinstance(matcher, dict)
                                  for cid in matcher.get("hookCallbackIds") or []]
                          for event, matchers in (request.get("hooks") or {}).items()}
        elif subtype == "get_context_usage":
            tokens = self.context_tokens()
            # The window the app pins (CLAUDE_CODE_AUTO_COMPACT_WINDOW), as the CLI honours it.
            pinned = os.environ.get("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "")
            window = int(pinned) if pinned.isdigit() and int(pinned) > 0 else 200_000
            self.emit({"type": "control_response", "response": {
                "subtype": "success", "request_id": msg.get("request_id", ""), "response": {
                    **script.CONTEXT_USAGE, "totalTokens": tokens, "model": self.model,
                    "maxTokens": window, "rawMaxTokens": window,
                    "autocompactSource": "env" if pinned else "default",
                    "percentage": round(tokens * 100 / window, 1)}}})
            return
        log("control", subtype=subtype)
        super().answer_control(msg)

    def context_tokens(self) -> int:
        return 12_000 + len(json.dumps(self.rows)) // 4

    def tools(self) -> list[str]:
        names = [n for n in BUILTIN_TOOLS if not self.removed(n)]
        for server in self.mcp.values():
            names += [f"mcp__{server.name}__{t}" for t in server.tools
                      if not self.removed(f"mcp__{server.name}__{t}")]
        return names

    def removed(self, name: str) -> bool:
        return any(fnmatch.fnmatchcase(name, p) for p in self.disallowed)

    def init_frame(self) -> dict:
        # Delay only the fake process, before its first init/session-id frame.
        delay_ms = max(0.0, fake_ai._number(os.environ.get("FAKE_CLAUDE_STARTUP_MS"), 0.0))
        if delay_ms:
            log("startup_delay", milliseconds=delay_ms)
            time.sleep(delay_ms / 1000)
        frame = super().init_frame()
        frame.update(
            model=self.model, tools=self.tools(), claude_code_version=VERSION.split()[0],
            mcp_servers=[{"name": s.name, "status": s.status} for s in self.mcp.values()],
            apiKeySource="none", output_style="default", slash_commands=["compact", "context"])
        return frame

    def main(self) -> int:
        if self.resume is not None:
            if self.transcript is None or not self.transcript.is_file():
                sys.stderr.write(f"No conversation found with session ID: {self.resume}\n")
                log("resume_refused", session_id=self.resume)
                return 1
            self._restore()
        plugin_dir = _flag("--plugin-dir")
        self.mod_loaded = bool(plugin_dir) and os.path.isfile(
            os.path.join(plugin_dir, ".claude-plugin", "plugin.json"))
        self.mcp = connect_mcp(_flag("--mcp-config"))
        log("init", session_id=self.sid, resumed=self.resume is not None, rows=len(self.rows),
            mcp={s.name: {"status": s.status, "error": s.error, "tools": s.tools}
                 for s in self.mcp.values()},
            mod=self.mod_loaded)
        try:
            return super().main()
        finally:
            for server in self.mcp.values():
                server.close()

    def steps_for(self, msg: dict, index: int, turns: list) -> list:
        return [{"op": "dev_turn", "msg": msg}]

    def run_step(self, step: dict) -> None:
        if step.get("op") != "dev_turn":
            return super().run_step(step)
        self.counter += 1
        self.play(step["msg"])

    # ── io helpers ──
    def poll(self, seconds: float = 0.01) -> None:
        """Read stdin for ``seconds``: control requests are answered (an
        interrupt sets ``interrupted``), user frames wait for the next turn."""
        deadline = time.monotonic() + seconds
        while not self.interrupted:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            msg = self.read(remaining)
            if msg is None:  # stdin closed: the turn still finishes, as with the CLI
                time.sleep(remaining)
                return
            if msg is script.TIMEOUT:
                return
            if isinstance(msg, dict) and msg.get("type") == "user":
                self.queued_users.append(msg)

    def quit(self, code: int, stderr: str = "") -> None:
        if stderr:
            sys.stderr.write(stderr + "\n")
            sys.stderr.flush()
        sys.stdout.flush()
        for server in self.mcp.values():
            server.close()
        os._exit(code)

    def await_answer(self, request_id: str, timeout: float) -> dict | None:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            msg = self.read(remaining)
            if msg is None or msg is script.TIMEOUT:  # nobody left to answer
                return None
            if msg is script.CONTROL:
                continue
            if msg.get("type") == "user":
                self.queued_users.append(msg)
                continue
            response = msg.get("response") or {}
            if msg.get("type") == "control_response" and response.get("request_id") == request_id:
                return (response.get("response") or {}) if response.get(
                    "subtype") == "success" else None

    def hook(self, event: str, data: dict, tool_use_id: str | None = None) -> list[dict]:
        answers = []
        for callback in self.hooks.get(event, []):
            self.counter += 1
            request_id = f"fake_hook_{self.counter}"
            request = {"subtype": "hook_callback", "callback_id": callback, "input": {
                "session_id": self.sid, "transcript_path": str(self.transcript or ""),
                "cwd": os.getcwd(), "permission_mode": self.mode, "hook_event_name": event,
                **data}}
            if tool_use_id:
                request["tool_use_id"] = tool_use_id
            self.emit({"type": "control_request", "request_id": request_id, "request": request})
            answer = self.await_answer(request_id, HOOK_TIMEOUT)
            log("hook", event=event, answered=answer is not None)
            if answer is not None:
                answers.append(answer)
        return answers

    def stream(self, event: dict) -> None:
        self.emit({"type": "stream_event", "uuid": _new_uuid(), "session_id": self.sid,
                   "parent_tool_use_id": None, "event": event})

    def usage(self, output: int = 1) -> dict:
        return {"input_tokens": 4, "output_tokens": output,
                "cache_read_input_tokens": self.context_tokens(),
                "cache_creation_input_tokens": 0}

    def start_message(self) -> None:
        self.model_calls += 1
        self.message_id = "msg_fake_" + uuid.uuid4().hex[:20]
        self.block = 0
        self.stream({"type": "message_start", "message": {
            "id": self.message_id, "type": "message", "role": "assistant", "model": self.model,
            "content": [], "stop_reason": None, "usage": self.usage()}})

    def end_message(self, stop_reason: str, output: int) -> None:
        self.stream({"type": "message_delta", "delta": {"stop_reason": stop_reason},
                     "usage": {"output_tokens": output}})
        self.stream({"type": "message_stop"})

    def assistant_frame(self, block: dict, row: dict, stop_reason: str | None) -> None:
        message = {"id": self.message_id, "type": "message", "role": "assistant",
                   "model": self.model, "content": [block], "stop_reason": stop_reason,
                   "usage": self.usage()}
        handle = self._add("assistant", message, row)
        self.emit({"type": "assistant", "uuid": handle, "session_id": self.sid,
                   "parent_tool_use_id": None, "message": message})

    def say(self, text: str, tps: float, cut_after: int | None = None) -> bool:
        """Stream ``text`` as one block; True when it was cut or interrupted."""
        index, self.block = self.block, self.block + 1
        self.stream({"type": "content_block_start", "index": index,
                     "content_block": {"type": "text", "text": ""}})
        tokens = fake_ai._tokens(text)
        size = 1 if tps > 0 else max(1, len(tokens) // 12)
        sent = ""
        for start in range(0, len(tokens), size):
            piece = "".join(tokens[start:start + size])
            self.stream({"type": "content_block_delta", "index": index,
                         "delta": {"type": "text_delta", "text": piece}})
            sent += piece
            if cut_after is not None and len(sent) >= cut_after:
                return True
            self.poll(1 / tps if tps > 0 else 0.01)
            if self.interrupted:
                return True
        self.stream({"type": "content_block_stop", "index": index})
        self.assistant_frame({"type": "text", "text": text},
                             {"role": "assistant", "text": text, "toolUses": []}, None)
        return False

    def call_frame(self, call: dict) -> None:
        index, self.block = self.block, self.block + 1
        block = {"type": "tool_use", "id": call["id"], "name": call["name"],
                 "input": call["input"]}
        self.stream({"type": "content_block_start", "index": index,
                     "content_block": {**block, "input": {}}})
        self.stream({"type": "content_block_delta", "index": index, "delta": {
            "type": "input_json_delta", "partial_json": json.dumps(call["input"])}})
        self.stream({"type": "content_block_stop", "index": index})
        self.assistant_frame(block, {"role": "assistant", "text": "", "toolUses": [
            {"tool_use_id": call["id"], "tool": call["name"], "input": call["input"]}]},
            "tool_use")

    def result_frame(self, text: str, *, subtype: str = "success", error: bool = False,
                     num_turns: int | None = None) -> None:
        self.emit({"type": "result", "subtype": subtype, "is_error": error,
                   "duration_ms": 40, "duration_api_ms": 25,
                   "num_turns": self.model_calls if num_turns is None else num_turns,
                   "result": text, "session_id": self.sid, "total_cost_usd": 0.0,
                   "usage": self.usage(len(fake_ai._tokens(text)) or 1),
                   "permission_denials": []})

    # ── a turn ──
    def play(self, msg: dict) -> None:
        self.model_calls = 0
        content = (msg.get("message") or {}).get("content")
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        blocks = [b for b in content or [] if isinstance(b, dict)]
        texts = [str(b.get("text") or "") for b in blocks if b.get("type") == "text"]
        command = _COMPACT.match(texts[-1].strip()) if texts else None
        log("turn", session_id=self.sid, prompt=fake_ai.visible("\n".join(texts))[:300],
            compact=bool(command))
        if command:
            if not self.compact("manual", command.group(1)):  # a skip printed its own result
                self.result_frame("", num_turns=0)
            return
        full = "\n".join(texts)
        stored = [b if b.get("type") == "text" else {"type": "text", "text": f"[{b.get('type')}]"}
                  for b in blocks]
        self._add("user", {"role": "user", "content": stored},
                  {"role": "user", "text": full, "toolUses": []})
        self.hook("UserPromptSubmit", {"prompt": full})
        prompt = fake_ai.visible(full)
        for _ in range(4):  # a Stop hook that blocks continues the turn (3 times at most)
            ended = self.scenario(prompt)
            if ended or self.interrupted:
                return
            blocked = [a for a in self.hook("Stop", {"stop_hook_active": False})
                       if a.get("decision") == "block"]
            if not blocked:
                break
            prompt = str(blocked[0].get("reason") or "")
            self._add("user", {"role": "user", "content": prompt},
                      {"role": "user", "text": prompt, "toolUses": []})
        self.result_frame(self.last_text)

    def scenario(self, prompt: str) -> bool:
        """Play the prompt's scenario. True when it already ended the turn."""
        self.last_text = ""
        name, params = fake_ai.parse_tag(prompt)
        try:
            data = load_scenario(name or "default")
        except (fake_ai.ScenarioError, ValueError) as exc:
            data = {"steps": [{"text": f"[fake claude] scenario error: {exc}"}]}
        if data is None:
            data = {"steps": [{"text": f"[fake claude] Unknown scenario '{name}'. Available: "
                                       f"{', '.join(scenario_names())}."}]}
        tps = fake_ai._number(params.get("tps"), fake_ai._number(data.get("tps"), 0.0))
        latency = fake_ai._number(params.get("latency_ms"),
                                  fake_ai._number(data.get("latency_ms"), 0.0))
        for step in data["steps"]:
            if self.interrupted:
                break
            if step.get("compact"):
                self.compact("auto", None)
            self.poll(latency / 1000 if latency else 0.01)
            self.start_message()
            if "error" in step:
                error = step["error"]
                self.end_message("end_turn", 1)
                self.result_frame(f"API Error: {error.get('status')} "
                                  f"{json.dumps({'type': 'error', 'error': {'message': error.get('message')}})}",
                                  error=True)
                return True
            text = (str(step.get("text") or "")
                    .replace("{summary}", fake_ai.summary(prompt) or "(empty)")
                    .replace("{scenarios}", ", ".join(scenario_names()))
                    .replace("{round}", str(self.model_calls)))
            calls = [{"id": "toolu_fake_" + uuid.uuid4().hex[:20], "name": c["name"],
                      "input": c.get("input") or {}, "spec": c}
                     for c in step.get("tool_calls") or []]
            removed = [c["name"] for c in calls if self.removed(c["name"])]
            if removed:
                text = (f"[fake claude] Scenario '{name}' wants {', '.join(removed)}, which "
                        f"--disallowed-tools removes from this session.")
                calls = []
            if text:
                if self.say(text, tps, step.get("cut_after_chars")):
                    if self.interrupted:
                        break
                    self.result_frame("API Error: Connection error.", error=True)
                    return True
                self.last_text = text
            for call in calls:
                self.call_frame(call)
            self.end_message("tool_use" if calls else "end_turn", len(fake_ai._tokens(text)) or 1)
            for call in calls:
                if self.interrupted:
                    break
                self.run_tool(call)
            if step.get("exit"):
                failure = step["exit"]
                log("exit", code=failure.get("code", 1))
                self.quit(int(failure.get("code", 1)), str(failure.get("stderr") or ""))
        if self.interrupted:
            self.result_frame("", subtype="error_during_execution", error=True)
            return True
        return False

    # ── tools ──
    def run_tool(self, call: dict) -> None:
        name, tool_input, tid = call["name"], call["input"], call["id"]
        decision, reason = None, None
        for answer in self.hook("PreToolUse", {"tool_name": name, "tool_input": tool_input,
                                               "tool_use_id": tid}, tid):
            spec = answer.get("hookSpecificOutput") or {}
            said = spec.get("permissionDecision")
            if said == "deny" or answer.get("decision") == "block" or answer.get("continue") is False:
                decision = "deny"
                reason = (spec.get("permissionDecisionReason") or answer.get("reason")
                          or answer.get("stopReason") or "Blocked by a PreToolUse hook")
                break
            if said in ("allow", "ask") and decision != "ask":
                decision = said
            if isinstance(spec.get("updatedInput"), dict):
                tool_input = spec["updatedInput"]
        if decision != "deny" and decision != "allow" and not (
                name in READ_ONLY or self.permitted_without_asking(name)):
            self.counter += 1
            self.ask({"id": f"fake_perm_{self.counter}", "tool_use_id": tid, "name": name,
                      "input": tool_input})
            answer = self.decisions.get(tid) or {}
            if answer.get("behavior") == "deny":
                decision, reason = "deny", answer.get("message") or "The user denied this tool."
            elif isinstance(answer.get("updatedInput"), dict):
                tool_input = answer["updatedInput"]
        if decision == "deny":
            text, is_error = str(reason), True
        else:
            text, is_error = self.execute(call, tool_input)
            self.hook("PostToolUse", {"tool_name": name, "tool_input": tool_input,
                                      "tool_response": text, "tool_use_id": tid}, tid)
        log("tool", name=name, denied=decision == "deny", chars=len(text), is_error=is_error)
        message = {"role": "user", "content": [{
            "type": "tool_result", "tool_use_id": tid,
            "content": [{"type": "text", "text": text}], "is_error": is_error}]}
        handle = self._add("user", message, {"role": "user", "text": "", "toolUses": [],
                                             "toolResults": [{"tool_use_id": tid, "text": text,
                                                              "isError": is_error}]})
        self.emit({"type": "user", "uuid": handle, "session_id": self.sid,
                   "parent_tool_use_id": None, "message": message})

    def execute(self, call: dict, tool_input: dict) -> tuple[str, bool]:
        spec, name = call["spec"], call["name"]
        if "result" in spec:
            text, is_error = str(spec["result"]), bool(spec.get("is_error"))
        elif "result_chars" in spec:
            text, is_error = filler(int(spec["result_chars"])), False
        elif name.startswith("mcp__") and self._server_for(name):
            server, tool = self._server_for(name)
            try:
                if server.list_changed:
                    server.refresh()
                text, is_error = server.call(tool, tool_input)
            except Exception as exc:  # noqa: BLE001 — reported as the tool's error
                text, is_error = f"MCP error: {exc}", True
        else:
            text, is_error = (f"(fake claude CLI: {name} was not run; dev-loop scenarios "
                              "script tool output with \"result\")"), False
        if name.startswith("mcp__") and len(text) > PERSIST_OVER:
            text = self.persisted(call["id"], text)
        return text, is_error

    def _server_for(self, name: str) -> tuple[McpServer, str] | None:
        for server in self.mcp.values():
            prefix = f"mcp__{server.name}__"
            if server.status == "connected" and name.startswith(prefix):
                tool = name[len(prefix):]
                if tool in server.tools:
                    return server, tool
        return None

    def persisted(self, tool_use_id: str, text: str) -> str:
        """Claude Code 2.1's stand-in for an MCP result over 50,000 characters."""
        where = "(not saved: outside the dev instance)"
        if self.transcript is not None:
            path = self.transcript.parent / self.sid / "tool-results" / f"{tool_use_id}.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            where = str(path)
        return (f"<persisted-output>\nOutput too large ({len(text) / 1024:.1f}KB). Full output "
                f"saved to: {where}\n\nPreview (first 2KB):\n{text[:PREVIEW_CHARS]}\n...\n"
                "</persisted-output>")

    # ── compaction ──
    def compact(self, trigger: str, instructions: str | None) -> bool:
        """Compact the conversation the way Claude Code 2.1.312 does, frame by
        frame as ``compaction/<trigger>-<outcome>.json`` lists it (the replay
        fixtures, derived from the real CLI; see
        docs/internal/claude-code-compaction-protocol.md). True when it ended
        the turn (a skipped ``/compact`` prints its own result)."""
        started = time.monotonic()
        before = self.context_tokens()
        ctx: dict[str, Any] = {"sid": self.sid, "logical_parent": self.last_uuid,
                               "pre_tokens": before, "instructions": instructions,
                               "boundary_uuid": _new_uuid(), "summary_uuid": _new_uuid()}
        outcome, answer = "summarizer", None
        if trigger == "manual" and not self.rows:
            outcome, ctx["reason"] = "skip", "Not enough messages to compact."
        prelude, _ = split_at_bridge(load_compaction(trigger, "summarizer"))
        # What the CLI prints before the mod's hook runs is the same for every
        # outcome of a trigger (a test checks it): the typed command's
        # "compacting" status; nothing for an automatic compaction.
        self.play_compaction(prelude, ctx)
        if outcome != "skip":
            try:
                answer = self.ask_bridge(trigger)
            except BridgeError as exc:
                if trigger == "manual":  # the mod leaves a manual one as it is
                    outcome = "skip"
                    ctx["reason"] = (f"Fluid could not compact the conversation ({exc}); it is "
                                     "left as it is")
            if outcome != "skip" and answer is not None:
                if isinstance(answer.get("messages"), list):
                    outcome, ctx["answer"] = "fluid", answer["messages"]
                elif "skip" in answer and trigger == "manual":
                    outcome, ctx["reason"] = "skip", str(answer["skip"])
                # else: a fallback or an automatic skip: the summarizer
        _, steps = split_at_bridge(load_compaction(trigger, outcome))
        ctx["started"] = started
        self.play_compaction(steps, ctx)
        log("compact", trigger=trigger, outcome=outcome, rows=len(self.rows))
        return outcome == "skip"

    def play_compaction(self, steps: list[dict], ctx: dict[str, Any]) -> None:
        """Run fixture steps: frames and transcript entries (placeholders bound
        from ``ctx``), hooks, the conversation's replacement, the replay."""
        for step in steps:
            if "hook" in step:
                self.hook(step["hook"], bind(step.get("input") or {}, ctx))
            elif "frame" in step:
                self.emit(bind(step["frame"], ctx))
            elif "transcript" in step:
                self._write(bind(step["transcript"], ctx))
            elif "apply" in step:
                self.apply(ctx["answer"], ctx)
            elif "summarize" in step:
                self.summarize(ctx, **step["summarize"])
            elif step.get("replay") == "answer":
                for entry in ctx["replay_entries"]:
                    self._write(entry)
                for frame in ctx["replay"]:
                    self.emit(frame)
            elif "result" in step:
                text = bind(step["result"].get("text", ""), ctx)
                self.result_frame(text, num_turns=step["result"].get("num_turns"))

    def ask_bridge(self, trigger: str) -> dict | None:
        """The app's answer from the mod's compaction route; None when not mounted."""
        url = os.environ.get("FLUID_CLAUDE_MOD_URL") or ""
        route = os.environ.get("FLUID_CLAUDE_MOD_COMPACT_URL") or ""
        token = os.environ.get("FLUID_CLAUDE_MOD_TOKEN") or ""
        unix = os.environ.get("FLUID_CLAUDE_MOD_SOCKET")
        pinned = re.match(r"^http://(127\.0\.0\.1:\d{1,5}|localhost)/claude-mod/usage$", url)
        if not (self.mod_loaded and pinned and route
                and route == url[: -len("/claude-mod/usage")] + "/claude-mod/compact"):
            return None
        rows = [{k: v for k, v in row.items() if k in ("role", "text", "toolUses",
                                                        "toolResults", "handle")}
                for row in self.rows]
        body = json.dumps({"v": 1, "trigger": trigger, "messages": rows})
        if unix:
            connection: http.client.HTTPConnection = _UnixConnection(unix, BRIDGE_TIMEOUT)
        else:
            connection = http.client.HTTPConnection(pinned.group(1), timeout=BRIDGE_TIMEOUT)
        try:
            connection.request("POST", "/claude-mod/compact", body=body, headers={
                "content-type": "application/json", "authorization": f"Bearer {token}"})
            response = connection.getresponse()
            payload = response.read()
        except OSError as exc:
            raise BridgeError("bridge transport error") from exc
        finally:
            connection.close()
        if response.status != 200:
            raise BridgeError(f"bridge HTTP {response.status}")
        try:
            answer = json.loads(payload)
        except ValueError as exc:
            raise BridgeError("invalid bridge response") from exc
        if not isinstance(answer, dict):
            raise BridgeError("invalid bridge response")
        return answer

    def _compacted(self, ctx: dict[str, Any]) -> None:
        """The counts the boundary reports, once the conversation is replaced."""
        after = self.context_tokens()
        self.dropped_tokens += max(0, ctx["pre_tokens"] - after)
        ctx.update(post_tokens=after, dropped_tokens=self.dropped_tokens,
                   duration_ms=max(1, round((time.monotonic() - ctx["started"]) * 1000)))

    def apply(self, rows: list[dict], ctx: dict[str, Any]) -> None:
        """Fluid's answer replaces the conversation. Claude Code then prints
        every row of it as an ordinary frame with a fresh uuid (the replay,
        #679): kept in ``ctx`` for the fixture's ``replay`` step, after the
        boundary."""
        kept, frames, entries = [], [], []
        parent = ctx["boundary_uuid"]
        for row in rows:
            row = {k: v for k, v in row.items() if k != "handle"}
            if row["role"] == "assistant":
                content = ([{"type": "text", "text": row["text"]}] if row["text"] else []) + [
                    {"type": "tool_use", "id": u["tool_use_id"], "name": u["tool"],
                     "input": u["input"]} for u in row["toolUses"]]
                message = {"id": "msg_fake_" + uuid.uuid4().hex[:20], "role": "assistant",
                           "model": self.model, "content": content}
            else:
                results = row.get("toolResults") or []
                content = ([{"type": "tool_result", "tool_use_id": r["tool_use_id"],
                             "content": [{"type": "text", "text": r["text"]}],
                             "is_error": r.get("isError", False)} for r in results]
                           if results else row["text"])
                message = {"role": "user", "content": content}
            handle = _new_uuid()
            row = {**row, "handle": handle}
            kept.append(row)
            entries.append({"type": row["role"], "uuid": handle, "parentUuid": parent,
                            "message": message, "fakeRow": row})
            frames.append({"type": row["role"], "message": message, "uuid": handle,
                           "timestamp": _now(), "session_id": self.sid,
                           "parent_tool_use_id": None})
            parent = handle
        self.rows, self.last_uuid = kept, parent
        self._compacted(ctx)
        ctx.update(replay=frames, replay_entries=entries)

    def summarize(self, ctx: dict[str, Any], *, keep: int, min_chars: int,
                  max_chars: int) -> None:
        """Claude Code's own summarizer, faked: a summary row, then the newest
        ``keep`` rows kept as they are (``preserved_messages``; never a tool
        result without its call). The kept rows are not printed again. The
        summary is a quarter of what it folds, within the real range."""
        start = max(1, len(self.rows) - keep)
        while start < len(self.rows) and self.rows[start].get("toolResults"):
            start += 1  # a result must stay beside its call: keep neither
        kept = self.rows[start:] if keep else []
        folded = self.rows[: len(self.rows) - len(kept)]
        asks = [r["text"] for r in folded if r["role"] == "user" and r["text"]]
        head = ("This session is being continued from a previous conversation that ran out "
                f"of context. The fake Claude CLI summarized {len(folded)} messages. The "
                "user's requests were:\n" + "\n".join(
                    f"- {fake_ai.summary(fake_ai.visible(a), 120)}" for a in asks[-5:]))
        size = min(max_chars, max(min_chars, len(json.dumps(folded)) // 4))
        summary = head + "\n\n" + filler(max(0, size - len(head) - 2))
        handle = ctx["summary_uuid"]
        self.rows = [{"role": "user", "text": summary, "toolUses": [], "handle": handle}, *kept]
        self.last_uuid = kept[-1]["handle"] if kept else handle
        uuids = [r["handle"] for r in kept]
        ctx.update(summary=summary, preserved_uuids=uuids,
                   preserved_head=uuids[0] if uuids else None,
                   preserved_tail=uuids[-1] if uuids else None, replay_entries=[])
        self._compacted(ctx)


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._path)
        self.sock = sock


AUTH_HELP = """Usage: claude auth [options] [command]

Manage authentication (fake: the dev loop's Claude Code CLI)

Commands:
  status [options]  Show authentication status
"""


def auth_status() -> dict:
    return {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty",
            "email": FAKE_EMAIL, "orgName": "Dev loop fake Claude CLI (no account)",
            "subscriptionType": "fake", "configDirectory": None}


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    log("start", argv=[a if len(a) <= 300 else a[:300] + "…" for a in args],
        env_keys=sorted(os.environ), cwd=os.getcwd())
    if "--version" in args or "-v" in args:
        print(VERSION)
        return 0
    if args[:1] == ["auth"]:
        if args[1:2] == ["status"]:
            print(json.dumps(auth_status(), indent=2))
            return 0
        if args[1:2] in ([], ["--help"], ["-h"]):
            print(AUTH_HELP)
            return 0
    if "-p" not in args and "--print" not in args:
        sys.stderr.write("The dev loop's fake claude only answers --version, auth status and "
                         "-p stream-json runs; it never signs in, updates or calls a model.\n")
        return 2
    return DevClaude().main()


if __name__ == "__main__":
    raise SystemExit(main())
