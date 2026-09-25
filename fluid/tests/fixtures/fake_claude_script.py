#!/usr/bin/env python3
"""A scripted fake ``claude`` CLI (stream-json, 2.1.x vocabulary) for e2e tests.

``fake_claude.py`` replays fixed scenarios picked by a letter; this one plays
whatever a test writes into a JSON script, so a test states the exact frame
sequence it needs next to its assertions. Hermetic: no network, no real CLI.

Selection (environment of the spawned child):

  FAKE_CLAUDE_SCRIPT   path to the script JSON (required)
  FAKE_CLAUDE_RECORD   path of a JSONL file the fake appends to: one ``start``
                       record (argv, env, cwd), every stdin frame it reads
                       (``stdin``), and each permission answer it acted on
                       (``decision``). Tests assert on what the CLI saw.

Script shape::

  {
    "version": "2.1.280 (Claude Code)",       # ``claude --version`` text
    "spawn_exit": {"code": 1, "stderr": "."}, # exit before any frame
    "init": {"tools": [...], "model": "...", "session_id": "..."},
    "context_usage": "answer" | "error" | "mute",   # get_context_usage replies
    "controls": {"<subtype>": "error" | "mute"},    # how other control
                                              # requests (set_model, …) are
                                              # answered; default success
    "turns": [[step, ...], ...],              # one list per user frame; after
                                              # the last, a plain text turn
    "resume_turns": [[step, ...], ...]        # used instead when spawned
  }                                           # with --resume

A ``--resume <id>`` argument becomes the session id, as with the real CLI.

Steps (``op``):

  text        {"text", "msg_id"?, "usage"?}        assistant text frame
  thinking    {"text"}                             assistant thinking frame
  tool_use    {"id", "name", "input", "usage"?}    assistant tool_use frame
  stream_text {"text", "chunks"?}                  stream_event deltas, then
                                                   the whole assistant frame
  ask         {"id", "tool_use_id", "name", "input", "suggestions"?,
               "skip_if_allowed"?}                 a ``can_use_tool``
              control_request; blocks until the matching control_response
              (other control requests are answered meanwhile). With
              ``skip_if_allowed`` the CLI's own rules decide first, as the real
              CLI does: ``--allowed-tools`` (fnmatch), ``bypassPermissions``
              and, for edit tools, ``acceptEdits`` allow without asking.
  tool_result {"tool_use_id", "content", "echo_input"?}   the CLI's tool_result
              echo. A denied call reports the deny message with ``is_error``;
              ``echo_input`` reports the (possibly rewritten) input it ran.
  cancel      {"id"}                               control_cancel_request
  result      {"subtype"?, "usage"?, "cost"?, "num_turns"?, "errors"?,
               "result"?}                          the turn's result frame
  frame       {"frame"}                            any frame, verbatim
  raw         {"line"}                             a raw stdout line
  sleep       {"s"}
  serve       {"subtype", "count"?, "timeout"?}    read stdin, answering control
              requests, until ``count`` of ``subtype`` were answered
  wait_interrupt {"timeout"?}                      read stdin (answering control
              requests) until an ``interrupt`` control_request arrives
  hang        {}                                   read stdin until EOF, exit 0
  exit        {"code", "stderr"?}                  die at once
"""

from __future__ import annotations

import fnmatch
import json
import os
import select
import sys
import time

TIMEOUT = object()
CONTROL = {"type": "_control_answered"}  # read() answered a control request
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
CONTEXT_USAGE = {
    "categories": [{"name": "Messages", "tokens": 600, "color": "#E17055"}],
    "totalTokens": 12000, "maxTokens": 200000, "rawMaxTokens": 200000,
    "percentage": 6.0, "model": "claude-fake", "isAutoCompactEnabled": True,
}
DEFAULT_USAGE = {
    "input_tokens": 10, "output_tokens": 5,
    "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
}


def _flag(name: str) -> str | None:
    if name in sys.argv:
        i = sys.argv.index(name)
        return sys.argv[i + 1] if i + 1 < len(sys.argv) else None
    return None


class Fake:
    def __init__(self, script: dict) -> None:
        self.script = script
        self.record_path = os.environ.get("FAKE_CLAUDE_RECORD")
        self.buf = b""
        self.eof = False
        self.queued_users: list[dict] = []
        self.decisions: dict[str, dict] = {}
        self.interrupted = False
        self.served: dict[str, int] = {}
        self.mode = _flag("--permission-mode") or "default"
        allowed = _flag("--allowed-tools") or ""
        self.allowed = [p for p in allowed.split(",") if p]
        self.sid = _flag("--resume") or (script.get("init") or {}).get("session_id", "cli-sess-1")
        self.msg_counter = 0

    # ─── io ──────────────────────────────────────────────────────────────

    def record(self, kind: str, **fields) -> None:
        if not self.record_path:
            return
        with open(self.record_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"kind": kind, **fields}) + "\n")

    def emit(self, frame: dict) -> None:
        sys.stdout.write(json.dumps(frame) + "\n")
        sys.stdout.flush()

    def _readline(self, timeout: float | None):
        deadline = None if timeout is None else time.monotonic() + timeout
        while b"\n" not in self.buf:
            if self.eof:
                if self.buf:
                    line, self.buf = self.buf, b""
                    return line
                return None
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return TIMEOUT
                ready, _, _ = select.select([0], [], [], remaining)
                if not ready:
                    return TIMEOUT
            chunk = os.read(0, 1 << 16)
            if not chunk:
                self.eof = True
            self.buf += chunk
        line, _, self.buf = self.buf.partition(b"\n")
        return line

    def read(self, timeout: float | None = None):
        """The next frame. A control request is answered here and reported as
        ``CONTROL`` (so a caller waiting on its effect can look again); None
        on EOF, TIMEOUT when ``timeout`` ran out."""
        while True:
            line = self._readline(timeout)
            if line is None or line is TIMEOUT:
                return line
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue
            self.record("stdin", frame=msg)
            if msg.get("type") == "control_request":
                self.answer_control(msg)
                return CONTROL
            return msg

    def next_user(self):
        if self.queued_users:
            return self.queued_users.pop(0)
        while True:
            msg = self.read()
            if msg is None or msg.get("type") == "user":
                return msg

    def answer_control(self, msg: dict) -> None:
        rid = msg.get("request_id", "")
        req = msg.get("request") or {}
        subtype = req.get("subtype")
        self.served[subtype] = self.served.get(subtype, 0) + 1
        payload: dict = {}
        if subtype == "initialize":
            payload = {"commands": [], "output_style": "default"}
        elif subtype == "interrupt":
            self.interrupted = True
        elif subtype == "set_permission_mode":
            if (self.script.get("controls") or {}).get(subtype) is None:
                self.mode = req.get("mode") or self.mode
        elif subtype == "get_context_usage":
            how = self.script.get("context_usage", "answer")
            if how == "mute":
                return
            if how == "error":
                self.emit({"type": "control_response", "response": {
                    "subtype": "error", "request_id": rid, "error": "unavailable",
                }})
                return
            payload = CONTEXT_USAGE
        how = (self.script.get("controls") or {}).get(subtype)
        if how == "mute":
            return
        if how == "error":
            self.emit({"type": "control_response", "response": {
                "subtype": "error", "request_id": rid, "error": f"{subtype} refused",
            }})
            return
        self.emit({"type": "control_response", "response": {
            "subtype": "success", "request_id": rid, "response": payload,
        }})

    # ─── frames ──────────────────────────────────────────────────────────

    def assistant(self, content: list, step: dict, stop_reason: str) -> None:
        self.msg_counter += 1
        self.emit({"type": "assistant", "session_id": self.sid, "message": {
            "id": step.get("msg_id") or f"msg_{self.msg_counter}",
            "model": step.get("model", "claude-fake"),
            "role": "assistant", "content": content,
            "usage": step.get("usage", DEFAULT_USAGE),
            "stop_reason": stop_reason,
        }})

    def permitted_without_asking(self, name: str) -> bool:
        if self.mode == "bypassPermissions":
            return True
        if self.mode == "acceptEdits" and name in EDIT_TOOLS:
            return True
        return any(fnmatch.fnmatchcase(name, pattern) for pattern in self.allowed)

    def ask(self, step: dict) -> None:
        tid = step.get("tool_use_id", "")
        if step.get("skip_if_allowed") and self.permitted_without_asking(step["name"]):
            self.decisions[tid] = {"behavior": "allow", "updatedInput": step.get("input", {}),
                                   "by": "cli_rules"}
            self.record("decision", tool_use_id=tid, response=self.decisions[tid])
            return
        self.emit({"type": "control_request", "request_id": step["id"], "request": {
            "subtype": "can_use_tool", "tool_name": step["name"],
            "input": step.get("input", {}), "tool_use_id": tid,
            "permission_suggestions": step.get("suggestions", []),
        }})
        while True:
            msg = self.read()
            if msg is None:
                sys.exit(0)
            if msg.get("type") == "user":
                self.queued_users.append(msg)
                continue
            response = msg.get("response") or {}
            if msg.get("type") == "control_response" and response.get("request_id") == step["id"]:
                self.decisions[tid] = response.get("response") or {}
                self.record("decision", tool_use_id=tid, response=self.decisions[tid])
                return

    def tool_result(self, step: dict) -> None:
        tid = step["tool_use_id"]
        decision = self.decisions.get(tid, {"behavior": "allow"})
        if decision.get("behavior") == "deny":
            text, is_error = decision.get("message") or "denied", True
        elif step.get("echo_input"):
            text, is_error = json.dumps(decision.get("updatedInput")), False
        else:
            text, is_error = step.get("content", "ok"), bool(step.get("is_error"))
        self.emit({"type": "user", "session_id": self.sid, "message": {"role": "user", "content": [{
            "type": "tool_result", "tool_use_id": tid,
            "content": [{"type": "text", "text": text}], "is_error": is_error,
        }]}})

    def result(self, step: dict) -> None:
        subtype = step.get("subtype", "success")
        frame = {
            "type": "result", "subtype": subtype, "duration_ms": 12, "duration_api_ms": 9,
            "is_error": step.get("is_error", subtype != "success"),
            "num_turns": step.get("num_turns", 1), "session_id": self.sid,
            "total_cost_usd": step.get("cost", 0.001),
            "usage": step.get("usage", DEFAULT_USAGE),
        }
        for key in ("errors", "result"):
            if key in step:
                frame[key] = step[key]
        self.emit(frame)

    def stream_text(self, step: dict) -> None:
        text, chunks = step["text"], max(1, int(step.get("chunks", 3)))
        size = max(1, -(-len(text) // chunks))

        def ev(event: dict) -> None:
            self.emit({"type": "stream_event", "uuid": "u", "session_id": self.sid, "event": event})

        ev({"type": "message_start", "message": {"usage": {"input_tokens": 9}}})
        ev({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})
        for i in range(0, len(text), size):
            ev({"type": "content_block_delta", "index": 0,
                "delta": {"type": "text_delta", "text": text[i:i + size]}})
        ev({"type": "content_block_stop", "index": 0})
        self.assistant([{"type": "text", "text": text}], step, "end_turn")

    def serve(self, step: dict) -> None:
        subtype, count = step["subtype"], int(step.get("count", 1))
        deadline = time.monotonic() + float(step.get("timeout", 10))
        while self.served.get(subtype, 0) < count:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            msg = self.read(remaining)
            if msg is None:
                sys.exit(0)
            if msg is not TIMEOUT and msg.get("type") == "user":
                self.queued_users.append(msg)

    def wait_interrupt(self, step: dict) -> None:
        deadline = time.monotonic() + float(step.get("timeout", 30))
        while not self.interrupted:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            msg = self.read(remaining)
            if msg is None:
                sys.exit(0)
            if msg is not TIMEOUT and msg.get("type") == "user":
                self.queued_users.append(msg)

    # ─── driver ──────────────────────────────────────────────────────────

    def run_step(self, step: dict) -> None:
        op = step["op"]
        if op == "text":
            self.assistant([{"type": "text", "text": step["text"]}], step, "end_turn")
        elif op == "thinking":
            self.assistant([{"type": "thinking", "thinking": step["text"], "signature": "sig"}],
                           step, "end_turn")
        elif op == "tool_use":
            self.assistant([{"type": "tool_use", "id": step["id"], "name": step["name"],
                             "input": step.get("input", {})}], step, "tool_use")
        elif op == "stream_text":
            self.stream_text(step)
        elif op == "ask":
            self.ask(step)
        elif op == "tool_result":
            self.tool_result(step)
        elif op == "cancel":
            self.emit({"type": "control_cancel_request", "request_id": step["id"]})
        elif op == "result":
            self.result(step)
        elif op == "frame":
            frame = dict(step["frame"])
            frame.setdefault("session_id", self.sid)
            self.emit(frame)
        elif op == "raw":
            sys.stdout.write(step["line"] + "\n")
            sys.stdout.flush()
        elif op == "sleep":
            time.sleep(float(step["s"]))
        elif op == "serve":
            self.serve(step)
        elif op == "wait_interrupt":
            self.wait_interrupt(step)
        elif op == "hang":
            while self.read() is not None:
                pass
            sys.exit(0)
        elif op == "exit":
            if step.get("stderr"):
                sys.stderr.write(step["stderr"] + "\n")
                sys.stderr.flush()
            sys.stdout.flush()
            os._exit(int(step.get("code", 1)))
        else:
            raise SystemExit(f"fake_claude_script: unknown op {op!r}")

    def main(self) -> int:
        spawn_exit = self.script.get("spawn_exit")
        if spawn_exit:
            sys.stderr.write(spawn_exit.get("stderr", "") + "\n")
            return int(spawn_exit.get("code", 1))
        init = self.script.get("init") or {}
        self.emit({
            "type": "system", "subtype": "init", "session_id": self.sid,
            "model": init.get("model", "claude-fake"),
            "tools": init.get("tools", ["Bash", "Read", "Edit", "mcp__fluid_bundled__Recall"]),
            "mcp_servers": init.get("mcp_servers", [{"name": "fluid_bundled", "status": "connected"}]),
            "permissionMode": self.mode, "cwd": os.getcwd(),
        })
        turns = self.script.get("turns") or []
        if _flag("--resume") is not None and "resume_turns" in self.script:
            turns = self.script["resume_turns"]
        index = 0
        while True:
            msg = self.next_user()
            if msg is None:
                return 0
            self.interrupted = False
            steps = turns[index] if index < len(turns) else [
                {"op": "text", "text": "Hello from the scripted fake."}, {"op": "result"},
            ]
            index += 1
            for step in steps:
                self.run_step(step)


def main() -> int:
    if "--version" in sys.argv:
        script = _load_script()
        print(script.get("version", "2.1.280 (Claude Code)"))
        return 0
    script = _load_script()
    fake = Fake(script)
    fake.record("start", argv=sys.argv[1:], env=dict(os.environ), cwd=os.getcwd())
    return fake.main()


def _load_script() -> dict:
    path = os.environ.get("FAKE_CLAUDE_SCRIPT")
    if not path:
        return {}
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


if __name__ == "__main__":
    sys.exit(main())
