"""Deterministic fake AI for the dev loop: free, offline, scripted by the prompt.

    python -m scripts.devloop.fake_ai --port 18990 [--scenarios DIR] [--log PATH]
        [--latency-ms N] [--tps N]

Serves ``GET /v1/models`` and streaming ``POST /v1/chat/completions``
(OpenAI chat), ``/v1/messages`` (Anthropic) and ``/v1/responses`` (OpenAI
Responses) on loopback, using the wire builders in :mod:`fake_wire`.

Reply selection is stateless and keyed off the request alone:

* The prompt is the last user message with real text (tool results and
  ``<system-reminder>`` blocks do not count).
* ``[script:NAME]`` in the prompt picks ``NAME.json`` from the scenarios
  directory; otherwise the ``default`` scenario answers. Options ride in the
  tag: ``[script:slow tps=4 latency_ms=800]``.
* A scenario is a list of ``steps``. The step played is the number of tool
  rounds (assistant tool calls) the request already carries after the prompt,
  so turn 1 can call a tool and turn 2, which sends the tool result back,
  gives the final text. Past the last step the last step repeats.
* A question-tray answer (the app queues it as a user message starting
  ``Answer to your question:``) is not a new prompt: it counts as one more
  round of the scenario that asked, and ``{answer}`` in a step's text is
  replaced by it.
* Provider safety checks: a step's ``served_model`` (or the tag option
  ``served_model=ID``) makes the reply report another model than the one
  requested, the way a provider reroute does; a step's ``refusal`` declines
  the request the way each dialect's safety check does (Anthropic
  ``stop_reason: "refusal"`` with ``stop_details``, a ``content_filter`` stop
  for chat completions, ``incomplete: content_filter`` for Responses) when the
  request asks for one of ``refuse_models`` (default ``fake-ai``), so a retry
  on any other model gets the step's text. ``refusal.partial_text`` streams
  that much of a reply first (Anthropic dialect only: the other two dialects'
  filters count as a block only before any output).

Every request is appended to a JSONL log (``--log``) for assertions.
"""
from __future__ import annotations

import argparse
import json
import re
import signal
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Self

if __package__ in (None, ""):  # ``python scripts/devloop/fake_ai.py``
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.devloop.fake_wire import (
    CUT,
    Chat,
    FakeProviderServer,
    Messages,
    Recorded,
    Reply,
    Responses,
    Sleep,
    error_reply,
)

SCENARIOS_DIR = Path(__file__).resolve().parent / "scenarios"
DEFAULT_MODEL = "fake-ai"
# A second model the dev loop lists, so a safety-check prompt on the Fake AI
# row has another model on the same account to offer.
SAFE_MODEL = "fake-ai-safe"
DEV_MODELS = (DEFAULT_MODEL, SAFE_MODEL)
_TAG = re.compile(r"\[script:([A-Za-z0-9_.-]+)((?:\s+[A-Za-z_]+=[^\s\]]+)*)\s*\]")
_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)
_TOKEN = re.compile(r"\S+\s*|\s+")
# How the app hands a question-tray answer to the agent (user_question_handler).
_ANSWER = re.compile(r"(?:^|\n)Answer to your question:\n(.*)\Z", re.DOTALL)
_REDACT = {"authorization", "x-api-key", "cookie", "proxy-authorization"}


# ─── request reading ────────────────────────────────────────────────────────


def dialect_for(path: str) -> str | None:
    clean = path.split("?", 1)[0].rstrip("/")
    for suffix, dialect in (("/chat/completions", "chat"), ("/messages", "messages"),
                            ("/responses", "responses")):
        if clean.endswith(suffix):
            return dialect
    return None


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and part.get("type") in ("text", "input_text"):
                parts.append(str(part.get("text") or ""))
        return "\n".join(parts)
    return ""


def conversation(dialect: str, body: Any) -> list[tuple[str, str]]:
    """The request as ``(kind, text)`` entries: ``user``, ``call`` (an
    assistant tool-call turn) or ``result`` (tool output sent back)."""
    if not isinstance(body, dict):
        return []
    entries: list[tuple[str, str]] = []
    if dialect == "responses":
        items = body.get("input")
        if isinstance(items, str):
            return [("user", items)]
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            kind = item.get("type")
            if kind == "function_call":
                if not entries or entries[-1][0] != "call":
                    entries.append(("call", ""))
            elif kind == "function_call_output":
                entries.append(("result", ""))
            elif item.get("role") == "user":
                entries.append(("user", _text_of(item.get("content"))))
        return entries
    for message in body.get("messages") or []:
        if not isinstance(message, dict):
            continue
        role, content = message.get("role"), message.get("content")
        if role == "assistant":
            calls = message.get("tool_calls") or (
                [b for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]
                if isinstance(content, list) else [])
            if calls:
                entries.append(("call", ""))
        elif role == "tool":
            entries.append(("result", ""))
        elif role == "user":
            blocks = content if isinstance(content, list) else []
            if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in blocks):
                entries.append(("result", _text_of(content)))
            else:
                entries.append(("user", _text_of(content)))
    return entries


def visible(text: str) -> str:
    return _REMINDER.sub("", text).strip()


def question_answer(text: str) -> str | None:
    """The body of a question-tray answer the app queued as a user message
    (``Answer to your question:`` then question/answer lines), else None."""
    found = _ANSWER.search(visible(text))
    return found.group(1).strip() if found else None


def prompt_and_round(entries: list[tuple[str, str]]) -> tuple[str, int]:
    """The user's prompt text and how many rounds followed it.

    A round is an assistant tool-call turn or a question-tray answer: the
    answer to a question the scenario asked continues that scenario instead
    of starting a new one."""
    def rounds(after: list[tuple[str, str]]) -> int:
        return sum(1 for kind, text in after
                   if kind == "call" or (kind == "user" and question_answer(text) is not None))

    for index in range(len(entries) - 1, -1, -1):
        kind, text = entries[index]
        if kind == "user" and visible(text) and question_answer(text) is None:
            return visible(text), rounds(entries[index + 1:])
    return "", rounds(entries)


def latest_answer(entries: list[tuple[str, str]]) -> str | None:
    for kind, text in reversed(entries):
        if kind == "user" and (answer := question_answer(text)) is not None:
            return answer
    return None


def offered_tools(body: Any) -> list[str]:
    names = []
    for tool in (body.get("tools") or []) if isinstance(body, dict) else []:
        if isinstance(tool, dict):
            name = tool.get("name") or (tool.get("function") or {}).get("name")
            if isinstance(name, str):
                names.append(name)
    return names


def parse_tag(prompt: str) -> tuple[str | None, dict[str, str]]:
    found = None
    for found in _TAG.finditer(prompt):
        pass  # the last tag wins
    if found is None:
        return None, {}
    params = dict(pair.split("=", 1) for pair in found.group(2).split())
    return found.group(1), params


CODE_MODE_TOOL = "run_python"


def python_cell(calls: list[dict]) -> str:
    """One code-mode cell: ``from tools.X import X``, then each call by keyword."""
    names = sorted({call["name"] for call in calls})
    lines = [f"from tools.{name} import {name}" for name in names]
    for number, call in enumerate(calls, 1):
        args = ", ".join(f"{key}={value!r}" for key, value in (call.get("input") or {}).items())
        lines += [f"r{number} = {call['name']}({args})", f"print(r{number})"]
    return "\n".join(lines)


def summary(prompt: str, limit: int = 160) -> str:
    text = " ".join(_TAG.sub("", prompt).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ─── scenarios ──────────────────────────────────────────────────────────────


class ScenarioError(ValueError):
    pass


def validate_scenario(name: str, data: Any) -> dict:
    if not isinstance(data, dict) or not isinstance(data.get("steps"), list) or not data["steps"]:
        raise ScenarioError(f"{name}: needs a non-empty 'steps' list")
    for number, step in enumerate(data["steps"], 1):
        if not isinstance(step, dict):
            raise ScenarioError(f"{name} step {number}: must be an object")
        kinds = [key for key in ("text", "tool_calls", "error", "refusal") if key in step]
        if not kinds or ("error" in step and len(kinds) > 1):
            raise ScenarioError(f"{name} step {number}: needs text, tool_calls (with optional "
                                "text), refusal or error")
        if "refusal" in step:
            _validate_refusal(f"{name} step {number}", step["refusal"])
        if "served_model" in step and not isinstance(step["served_model"], str):
            raise ScenarioError(f"{name} step {number}: served_model must be a string")
        for call in step.get("tool_calls") or []:
            if not isinstance(call, dict) or not isinstance(call.get("name"), str):
                raise ScenarioError(f"{name} step {number}: each tool call needs a name")
        if "error" in step and not isinstance(step["error"].get("status"), int):
            raise ScenarioError(f"{name} step {number}: error needs an integer status")
    for key in ("tps", "latency_ms"):
        if key in data and not isinstance(data[key], (int, float)):
            raise ScenarioError(f"{name}: {key} must be a number")
    return data


def _validate_refusal(where: str, refusal: Any) -> None:
    if not isinstance(refusal, dict):
        raise ScenarioError(f"{where}: refusal must be an object")
    for key in ("category", "explanation", "recommended_model", "partial_text"):
        if key in refusal and refusal[key] is not None and not isinstance(refusal[key], str):
            raise ScenarioError(f"{where}: refusal.{key} must be a string")
    models = refusal.get("refuse_models", [DEFAULT_MODEL])
    if not isinstance(models, list) or not all(isinstance(m, str) for m in models):
        raise ScenarioError(f"{where}: refusal.refuse_models must be a list of model ids")


def refuses(refusal: dict, model: Any) -> bool:
    """Does this step's safety check decline a request for ``model``?

    ``refuse_models`` lists the ids it declines (``"*"`` declines every
    model); a request for any other model is answered normally, which is
    what a retry on the offered model sends."""
    models = refusal.get("refuse_models", [DEFAULT_MODEL])
    return "*" in models or (model or DEFAULT_MODEL) in models


class Scenarios:
    """``*.json`` in one directory, reloaded when a file changes."""

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        self._lock = threading.Lock()
        self._stamp: tuple = ()
        self._loaded: dict[str, dict] = {}
        self.load()

    def load(self) -> dict[str, dict]:
        with self._lock:
            files = sorted(self.directory.glob("*.json"))
            stamp = tuple((f.name, f.stat().st_mtime_ns) for f in files)
            if stamp != self._stamp:
                loaded = {}
                for path in files:
                    try:
                        data = json.loads(path.read_text(encoding="utf-8"))
                    except ValueError as exc:
                        raise ScenarioError(f"{path.name}: {exc}") from exc
                    loaded[path.stem] = validate_scenario(path.stem, data)
                if "default" not in loaded:
                    raise ScenarioError(f"{self.directory}: a default.json scenario is required")
                self._loaded, self._stamp = loaded, stamp
            return self._loaded


# ─── replies ────────────────────────────────────────────────────────────────


@dataclass
class Plan:
    """What one request gets, before it is framed for a dialect."""
    text: str = ""
    calls: list[dict] = field(default_factory=list)
    error: dict | None = None
    cut_after: int | None = None
    tps: float = 0.0
    latency_ms: float = 0.0
    served_model: str | None = None  # the model the reply says answered
    refusal: dict | None = None  # the safety check declined this request


def _tokens(text: str) -> list[str]:
    return _TOKEN.findall(text)


def _streamed(text: str, plan: Plan, frame: Callable[[str], bytes]) -> tuple[list[Any], bool]:
    """Text as delta frames at ``plan.tps``; True when the stream was cut."""
    tokens = _tokens(text)
    if plan.tps <= 0:  # still several deltas, so the UI streams
        size = max(1, len(tokens) // 12)
        pieces = ["".join(tokens[i:i + size]) for i in range(0, len(tokens), size)]
    else:
        pieces = tokens
    frames: list[Any] = []
    sent = 0
    for piece in pieces:
        if plan.tps > 0 and frames:
            frames.append(Sleep(1 / plan.tps))
        frames.append(frame(piece))
        sent += len(piece)
        if plan.cut_after is not None and sent >= plan.cut_after:
            frames.append(CUT)
            return frames, True
    return frames, False


def _usage(body: Any, text: str) -> tuple[int, int]:
    return max(1, len(json.dumps(body)) // 4), max(1, len(_tokens(text)))


def _call_args(call: dict) -> str:
    return json.dumps(call.get("input") or {})


def _stop_details(refusal: dict) -> dict:
    """Anthropic's ``stop_details`` for a refusal (``recommended_model`` only
    when the scenario names one: the API sends it only to a request that asked
    for ``fallbacks``, which the app never does)."""
    details: dict[str, Any] = {"type": "refusal", "category": refusal.get("category"),
                               "explanation": refusal.get("explanation")}
    if refusal.get("recommended_model"):
        details["recommended_model"] = refusal["recommended_model"]
    return details


def frame_chat(plan: Plan, body: Any, stream: bool) -> Reply:
    model = plan.served_model or body.get("model") or DEFAULT_MODEL
    prompt_tokens, completion_tokens = _usage(body, plan.text)
    if plan.refusal is not None:
        # A content filter that stopped the reply before any output.
        if not stream:
            return Reply(content_type="application/json", frames=[json.dumps({
                "id": "chatcmpl-" + uuid.uuid4().hex[:12], "object": "chat.completion",
                "model": model, "choices": [{"index": 0, "finish_reason": "content_filter",
                                             "message": {"role": "assistant", "content": None}}],
                "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": 0,
                          "total_tokens": prompt_tokens}})])
        return Reply(frames=[Chat.chunk({"role": "assistant", "content": ""}, model=model),
                             Chat.chunk({}, "content_filter", model=model),
                             Chat.usage(prompt_tokens, 0, model=model), Chat.DONE],
                     framing="close")
    calls = [{"id": c["id"], "name": c["name"], "args": _call_args(c)} for c in plan.calls]
    finish = "tool_calls" if calls else "stop"
    if not stream:
        if plan.cut_after is not None:
            return Reply(frames=[CUT], framing="chunked", content_type="application/json")
        message: dict[str, Any] = {"role": "assistant", "content": plan.text or None}
        if calls:
            message["tool_calls"] = [{"id": c["id"], "type": "function", "function": {
                "name": c["name"], "arguments": c["args"]}} for c in calls]
        return Reply(content_type="application/json", frames=[json.dumps({
            "id": "chatcmpl-" + uuid.uuid4().hex[:12], "object": "chat.completion",
            "model": model, "choices": [{"index": 0, "message": message,
                                         "finish_reason": finish}],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                      "total_tokens": prompt_tokens + completion_tokens}})])
    frames: list[Any] = [Chat.chunk({"role": "assistant", "content": ""}, model=model)]
    text_frames, cut = _streamed(plan.text, plan,
                                 lambda t: Chat.chunk({"content": t}, model=model))
    frames += text_frames
    if not cut:
        if calls:
            frames += Chat.tool_calls(calls, finish=None, usage=False, done=False,
                                      model=model)[1:]
        frames += [Chat.chunk({}, finish, model=model),
                   Chat.usage(prompt_tokens, completion_tokens, model=model), Chat.DONE]
    return Reply(frames=frames, framing="chunked" if cut else "close")


def frame_messages(plan: Plan, body: Any, stream: bool) -> Reply:
    model = plan.served_model or body.get("model") or DEFAULT_MODEL
    input_tokens, output_tokens = _usage(body, plan.text)
    stop = "tool_use" if plan.calls else "end_turn"
    if plan.refusal is not None:
        details = _stop_details(plan.refusal)
        if not stream:
            return Reply(content_type="application/json", frames=[json.dumps({
                "id": "msg_" + uuid.uuid4().hex[:12], "type": "message", "role": "assistant",
                "model": model, "content": [], "stop_reason": "refusal",
                "stop_sequence": None, "stop_details": details,
                "usage": {"input_tokens": input_tokens, "output_tokens": 1}})])
        frames = [Messages.start(input_tokens, model=model)]
        partial = plan.refusal.get("partial_text")
        if partial:
            # The classifier fired after part of the reply had streamed.
            frames += Messages.text_block(0, partial)
        return Reply(frames=[
            *frames,
            Messages.ev("message_delta", delta={"stop_reason": "refusal", "stop_sequence": None,
                                                "stop_details": details},
                        usage={"output_tokens": 1}),
            Messages.ev("message_stop"),
        ])
    if not stream:
        if plan.cut_after is not None:
            return Reply(frames=[CUT], framing="chunked", content_type="application/json")
        content: list[dict] = [{"type": "text", "text": plan.text}] if plan.text else []
        content += [{"type": "tool_use", "id": c["id"], "name": c["name"],
                     "input": c.get("input") or {}} for c in plan.calls]
        return Reply(content_type="application/json", frames=[json.dumps({
            "id": "msg_" + uuid.uuid4().hex[:12], "type": "message", "role": "assistant",
            "model": model, "content": content, "stop_reason": stop, "stop_sequence": None,
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens}})])
    frames: list[Any] = [Messages.start(input_tokens, model=model)]
    index = 0
    if plan.text:
        frames.append(Messages.ev("content_block_start", index=0,
                                  content_block={"type": "text", "text": ""}))
        text_frames, cut = _streamed(plan.text, plan, lambda t: Messages.ev(
            "content_block_delta", index=0, delta={"type": "text_delta", "text": t}))
        frames += text_frames
        if cut:
            return Reply(frames=frames, framing="chunked")
        frames.append(Messages.ev("content_block_stop", index=0))
        index = 1
    for call in plan.calls:
        frames += Messages.tool_block(index, call["id"], call["name"], _call_args(call))
        index += 1
    return Reply(frames=frames + Messages.end(stop, output_tokens))


def frame_responses(plan: Plan, body: Any, stream: bool) -> Reply:
    model = plan.served_model or body.get("model") or DEFAULT_MODEL
    input_tokens, output_tokens = _usage(body, plan.text)
    message_id = "msg_" + uuid.uuid4().hex[:12]
    if plan.refusal is not None:
        # The content filter ended the response before any output.
        response = {"id": "resp_" + uuid.uuid4().hex[:12], "object": "response",
                    "status": "incomplete", "model": model, "output": [],
                    "incomplete_details": {"reason": "content_filter"},
                    "usage": {"input_tokens": input_tokens, "output_tokens": 0,
                              "total_tokens": input_tokens,
                              "input_tokens_details": {"cached_tokens": 0}}}
        if not stream:
            return Reply(content_type="application/json", frames=[json.dumps(response)])
        return Reply(frames=[Responses.created(),
                             Responses.ev("response.incomplete", response=response)])
    items: list[dict] = []
    if plan.text:
        items.append({"type": "message", "id": message_id, "role": "assistant",
                      "status": "completed", "content": [
                          {"type": "output_text", "text": plan.text, "annotations": []}]})
    items += [{"type": "function_call", "id": f"fc_{c['id']}", "call_id": c["id"],
               "name": c["name"], "arguments": _call_args(c), "status": "completed"}
              for c in plan.calls]
    usage = {"input_tokens": input_tokens, "output_tokens": output_tokens}
    if not stream:
        if plan.cut_after is not None:
            return Reply(frames=[CUT], framing="chunked", content_type="application/json")
        return Reply(content_type="application/json", frames=[json.dumps({
            "id": "resp_" + uuid.uuid4().hex[:12], "object": "response", "status": "completed",
            "model": model, "output": items,
            "usage": {**usage, "total_tokens": input_tokens + output_tokens,
                      "input_tokens_details": {"cached_tokens": 0}}})])
    frames: list[Any] = [Responses.created()]
    output_index = 0
    if plan.text:
        item = {"type": "message", "id": message_id, "role": "assistant",
                "status": "in_progress", "content": []}
        frames.append(Responses.ev("response.output_item.added", output_index=0, item=item))
        text_frames, cut = _streamed(plan.text, plan, lambda t: Responses.ev(
            "response.output_text.delta", item_id=message_id, output_index=0,
            content_index=0, delta=t))
        frames += text_frames
        if cut:
            return Reply(frames=frames, framing="chunked")
        frames.append(Responses.ev("response.output_item.done", output_index=0, item=items[0]))
        output_index = 1
    for offset, call in enumerate(plan.calls):
        frames += Responses.function_call(f"fc_{call['id']}", call["id"], call["name"],
                                          _call_args(call), output_index=output_index + offset)
    return Reply(frames=frames + [Responses.completed(input_tokens, output_tokens,
                                                      output=items, model=model)])


_FRAMERS = {"chat": frame_chat, "messages": frame_messages, "responses": frame_responses}


def _number(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class FakeAI:
    """The responder plus its request log. ``server`` is the live listener."""

    def __init__(self, scenarios_dir: Path = SCENARIOS_DIR, *, log_path: Path | None = None,
                 latency_ms: float = 0.0, tps: float = 0.0, host: str = "127.0.0.1",
                 port: int = 0, models: tuple[str, ...] = (DEFAULT_MODEL,)) -> None:
        self.scenarios = Scenarios(scenarios_dir)
        self.log_path = Path(log_path) if log_path else None
        self.latency_ms = latency_ms
        self.tps = tps
        self._log_lock = threading.Lock()
        self.server = FakeProviderServer(responder=self.respond, models=models, host=host,
                                         port=port, on_reply=self._log_get)

    @property
    def url(self) -> str:
        return self.server.url

    def close(self) -> None:
        self.server.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # The log is the assertion surface for journey tests: one JSON line per
    # request, credentials redacted.
    def _log(self, recorded: Recorded, **entry: Any) -> None:
        if self.log_path is None:
            return
        headers = {k: ("<redacted>" if k.lower() in _REDACT else v)
                   for k, v in recorded.headers.items()}
        line = json.dumps({"ts": time.time(), "method": recorded.method, "path": recorded.path,
                           **entry, "headers": headers, "body": recorded.body},
                          default=str)
        with self._log_lock, self.log_path.open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")

    def _log_get(self, recorded: Recorded, reply: Reply) -> None:
        if recorded.method == "GET":
            self._log(recorded, reply="models" if reply.status == 200 else "error",
                      status=reply.status)

    def plan(self, recorded: Recorded) -> tuple[Plan, dict]:
        """Choose the reply for a request; also returns the log facts."""
        dialect = dialect_for(recorded.path)
        body = recorded.body if isinstance(recorded.body, dict) else {}
        entries = conversation(dialect or "", body)
        prompt, round_ = prompt_and_round(entries)
        name, params = parse_tag(prompt)
        scenarios = self.scenarios.load()
        facts = {"dialect": dialect, "stream": bool(body.get("stream")),
                 "model": body.get("model"), "scenario": name or "default", "params": params,
                 "round": round_, "prompt": prompt[:500], "tools": offered_tools(body)}
        scenario = scenarios.get(name or "default")
        if scenario is None:
            return Plan(text=f"[fake-ai] Unknown scenario '{name}'. Available: "
                             f"{', '.join(sorted(scenarios))}."), facts
        steps = scenario["steps"]
        step = steps[min(round_, len(steps) - 1)]
        plan = Plan(
            tps=_number(params.get("tps"), _number(scenario.get("tps"), self.tps)),
            latency_ms=_number(params.get("latency_ms"),
                               _number(scenario.get("latency_ms"), self.latency_ms)),
            cut_after=step.get("cut_after_chars"),
            served_model=step.get("served_model") or params.get("served_model"),
        )
        if "error" in step:
            plan.error = step["error"]
            return plan, facts
        if step.get("refusal") is not None and refuses(step["refusal"], body.get("model")):
            plan.refusal = step["refusal"]
            return plan, facts
        text = str(step.get("text") or "")
        text = text.replace("{summary}", summary(prompt) or "(empty)")
        text = text.replace("{scenarios}", ", ".join(sorted(scenarios)))
        text = text.replace("{round}", str(round_ + 1))
        text = text.replace("{answer}", latest_answer(entries) or "(no answer yet)")
        plan.text = text
        wanted = step.get("tool_calls") or []
        if wanted:
            offered = {tool.lower(): tool for tool in facts["tools"]}
            missing = [c["name"] for c in wanted if c["name"].lower() not in offered]
            if not offered:
                # Side calls (titles, suggestions) offer no tools: answer in text.
                plan.text = text or f"[fake-ai] {name}: tool step without tools"
            elif not missing:
                plan.calls = [{"id": "call_" + uuid.uuid4().hex[:20],
                               "name": offered[c["name"].lower()],
                               "input": c.get("input") or {}} for c in wanted]
            elif CODE_MODE_TOOL in offered:
                # The native engine's default surface: work tools are called
                # from one Python cell, the way a real model does it.
                facts["code_mode"] = True
                plan.calls = [{"id": "call_" + uuid.uuid4().hex[:20],
                               "name": offered[CODE_MODE_TOOL],
                               "input": {"code": python_cell(wanted)}}]
            else:
                plan.text = (f"[fake-ai] Scenario '{name}' wants tool(s) {', '.join(missing)} "
                             f"but this request offers: {', '.join(facts['tools'])}.")
        return plan, facts

    def respond(self, recorded: Recorded) -> Reply:
        dialect = dialect_for(recorded.path)
        if dialect is None:
            reply = error_reply(404, f"fake-ai: no endpoint {recorded.path}")
            self._log(recorded, reply="error", status=404)
            return reply
        try:
            plan, facts = self.plan(recorded)
        except ScenarioError as exc:
            reply = error_reply(500, f"fake-ai scenario error: {exc}")
            self._log(recorded, dialect=dialect, reply="error", status=500)
            return reply
        if plan.error is not None:
            error = plan.error
            retry_after = error.get("retry_after")
            reply = error_reply(int(error["status"]), str(error.get("message") or "error"),
                                retry_after=None if retry_after is None else str(retry_after))
        else:
            body = recorded.body if isinstance(recorded.body, dict) else {}
            reply = _FRAMERS[dialect](plan, body, facts["stream"])
        reply.delay = plan.latency_ms / 1000
        kind = ("error" if plan.error else "refusal" if plan.refusal is not None
                else "cut" if plan.cut_after is not None
                else "tool_calls" if plan.calls else "text")
        self._log(recorded, **facts, reply=kind, status=reply.status,
                  tool_calls=[{"name": c["name"], "input": c["input"]} for c in plan.calls])
        return reply


def default_log_path() -> Path:
    directory = Path(tempfile.mkdtemp(prefix="ai-shuffle-fake-ai-"))
    return directory / "requests.jsonl"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=0, help="Listen port (0 picks a free one)")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default loopback)")
    parser.add_argument("--scenarios", type=Path, default=SCENARIOS_DIR)
    parser.add_argument("--log", type=Path, help="JSONL request log (default: a temp file)")
    parser.add_argument("--latency-ms", type=float, default=0.0,
                        help="Delay before every reply's first byte")
    parser.add_argument("--tps", type=float, default=0.0,
                        help="Streamed tokens per second (0 = as fast as possible)")
    args = parser.parse_args(argv)
    if args.port in (8000, 8001):
        parser.error("8000/8001 belong to the live worker")
    log_path = args.log or default_log_path()
    stopped = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stopped.set())
    with FakeAI(args.scenarios, log_path=log_path, latency_ms=args.latency_ms, tps=args.tps,
                host=args.host, port=args.port) as fake:
        print(f"Fake AI listening on {fake.url} (scenarios: "
              f"{', '.join(sorted(fake.scenarios.load()))}); request log: {log_path}", flush=True)
        stopped.wait()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
