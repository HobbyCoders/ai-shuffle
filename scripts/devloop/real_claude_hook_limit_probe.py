#!/usr/bin/env python3
"""Ask the real ``claude`` CLI what happens to hook context around its size limit.

The compaction fixes rest on behaviour only the real CLI can confirm: how much of
a hook answer reaches the model, and in what order a compaction it summarizes
itself runs. This drives the CLI the way DelegatedLoop does, over stream-json with
hook callbacks registered at ``initialize``, runs a real ``/compact``, and asks a
cheap model for a token placed only at the very end of the context.

It uses the machine's existing Claude Code login and changes nothing: no settings
files are read (``--setting-sources ""``), no session is kept, the scratch folder
is temporary and no app is involved. It spends a little real model usage, so it is
never run by CI.

    python scripts/devloop/real_claude_hook_limit_probe.py

Prints one JSON line per case. ``model_saw_tail`` true means the whole answer
reached the model; ``order`` is what the CLI did, in the order this script read it.
Each size runs with plain ASCII filler and with emoji filler: the CLI's check is
JavaScript's ``length``, in UTF-16 units, so emoji reach it with about half as many
Python characters. ``python_chars`` and ``utf16_units`` say both measures of a case.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
from typing import Any

TURN_SECONDS = 180
TAIL = "TAILTOKEN="


def _units(text: str) -> int:
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def _context(size: int, token: str, filler: str = "ascii") -> str:
    """Exactly ``size`` UTF-16 units, with the token only at the very end."""
    tail = f" {TAIL}{token}"
    room = size - len(tail)
    if filler == "emoji":  # three units each: the pair and a space
        body = "\U0001F600 " * (room // 3) + "x" * (room % 3)
    else:
        body = ("filler text " * (room // 12 + 1))[:room]
    return body + tail


class _Cli:
    def __init__(self, claude: str, model: str, cwd: str, hooks: tuple[str, ...]) -> None:
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("CLAUDE") and k != "ANTHROPIC_API_KEY"}
        env["DISABLE_AUTOUPDATER"] = "1"
        self.proc = subprocess.Popen(
            [claude, "-p", "--input-format", "stream-json", "--output-format", "stream-json",
             "--verbose", "--model", model, "--setting-sources", "", "--allowedTools", "Bash",
             "--no-session-persistence"],
            cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True,
        )
        self.frames: queue.Queue[dict[str, Any] | None] = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()
        self.send({"type": "control_request", "request_id": "init", "request": {
            "subtype": "initialize",
            "hooks": {event: [{"matcher": None, "hookCallbackIds": [event]}] for event in hooks},
        }})

    def _read(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            try:
                self.frames.put(json.loads(line))
            except ValueError:
                continue
        self.frames.put(None)

    def send(self, frame: dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(frame) + "\n")
        self.proc.stdin.flush()

    def say(self, text: str) -> None:
        self.send({"type": "user", "message": {"role": "user", "content": text}})

    def close(self) -> None:
        self.proc.kill()
        self.proc.wait()


def _turn(cli: _Cli, event: str, context: str, order: list[str]) -> str:
    """Read one turn to its result, answering hooks. ``event``'s answer carries
    ``context``: for SessionStart, only a compaction's."""
    deadline = time.monotonic() + TURN_SECONDS
    while time.monotonic() < deadline:
        try:
            frame = cli.frames.get(timeout=max(0.1, deadline - time.monotonic()))
        except queue.Empty:
            break
        if frame is None:
            return "(the CLI exited)"
        kind, subtype = frame.get("type"), frame.get("subtype")
        if kind == "control_request":
            request = frame.get("request") or {}
            answer: dict[str, Any] = {}
            if request.get("subtype") == "hook_callback":
                data = request.get("input") or {}
                name = data.get("hook_event_name", "")
                order.append(f"hook:{name}" + (f"({data['source']})" if data.get("source") else ""))
                if name == event and (name != "SessionStart" or data.get("source") == "compact"):
                    answer = {"hookSpecificOutput": {
                        "hookEventName": name, "additionalContext": context,
                    }}
            cli.send({"type": "control_response", "response": {
                "subtype": "success", "request_id": frame.get("request_id"), "response": answer,
            }})
        elif kind == "system" and subtype == "status":
            order.append(f"status:{frame.get('status')}:{frame.get('compact_result', '')}")
        elif kind == "system" and subtype == "compact_boundary":
            order.append("compact_boundary")
        elif kind == "result":
            return str(frame.get("result") or frame.get("subtype"))
    return "(no result in time)"


def probe(claude: str, model: str, event: str, size: int, filler: str = "ascii") -> dict[str, Any]:
    token = secrets.token_hex(6)
    context = _context(size, token, filler)
    cwd = tempfile.mkdtemp(prefix="hook-limit-probe-")
    order: list[str] = []
    ask = (f"Reply with one line only: the value after the literal text '{TAIL}' if your "
           "context shows it (do not read any file or run anything to find it), or NONE.")
    cli = _Cli(claude, model, cwd, ("PreCompact", "SessionStart", "PostToolUse"))
    try:
        if event == "SessionStart":
            cli.say("Reply with the single word: ready.")
            _turn(cli, event, context, order)
            cli.say("/compact")
            _turn(cli, event, context, order)
            cli.say(f"Do not use any tool. {ask}")
        else:
            cli.say(f"First run the Bash command `true`. Then: {ask}")
        reply = _turn(cli, event, context, order)
    finally:
        cli.close()
        shutil.rmtree(cwd, ignore_errors=True)
    return {"event": event, "filler": filler, "utf16_units": _units(context),
            "python_chars": len(context), "model_saw_tail": token in reply,
            "reply_is_none": reply.strip() == "NONE", "order": order}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--claude", default=shutil.which("claude") or "claude")
    parser.add_argument("--model", default="haiku")
    parser.add_argument("--sizes", default="9000,10000,10001", help="in UTF-16 units")
    parser.add_argument("--fillers", default="ascii,emoji")
    args = parser.parse_args()
    version = subprocess.run([args.claude, "--version"], capture_output=True, text=True, timeout=30)
    print(json.dumps({"cli": version.stdout.strip()}), flush=True)
    for event in ("SessionStart", "PostToolUse"):
        for filler in args.fillers.split(","):
            for size in (int(s) for s in args.sizes.split(",")):
                print(json.dumps(probe(args.claude, args.model, event, size, filler)), flush=True)


if __name__ == "__main__":
    main()
