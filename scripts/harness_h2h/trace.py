"""Normalise each arm's raw logs into one trace shape.

    {"calls": [{"t_start", "t_end", "t_first", "input", "cached", "output",
                "reasoning", "role", "exact"}],
     "tools": [{"id", "name", "t_start", "t_end", "is_error", "input"}],
     "final_text": str, "self_reported": {...}}

``calls`` come from the metered client log when there is one (Claude Code and
bare fluid: exact start, first token and end). The app arm has no access to
its client, so its calls are rebuilt from the event log: a call ends at its
``Usage`` event and starts at the previous boundary (the turn start, the
previous call's end, or the last tool result before it). ``exact`` says which.
``input`` excludes ``cached`` everywhere.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from scripts.harness_h2h.metered import read_jsonl

_PREVIEW = 400


def _preview(value: Any) -> str:
    try:
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(value)
    return text[:_PREVIEW]


def _metered_calls(path: Path) -> list[dict[str, Any]]:
    calls = []
    for r in read_jsonl(path):
        key = str(r.get("cache_key") or "")
        role = "learner" if key.startswith("learner:") else ("small" if key.endswith(":small") else "main")
        calls.append({
            "t_start": r.get("t_start"), "t_end": r.get("t_end"), "t_first": r.get("t_first"),
            "input": r.get("input", 0), "cached": r.get("cached", 0), "output": r.get("output", 0),
            "reasoning": r.get("reasoning", 0), "role": role, "effort": r.get("effort"),
            "error": r.get("error"), "retries": r.get("retries", 0), "exact": True,
            "n_tools": r.get("n_tools"), "n_messages": r.get("n_messages"),
        })
    return calls


# ─── Claude Code (stream-json) ──────────────────────────────────────────────


def from_claude_stream(stream_path: Path, calls_path: Path) -> dict[str, Any]:
    tools: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    final_text = ""
    result: dict[str, Any] = {}
    texts: list[str] = []
    parse_errors = 0
    for row in read_jsonl(stream_path):
        t, line = row.get("t"), row.get("line", "")
        try:
            ev = json.loads(line)
        except ValueError:
            parse_errors += 1
            continue
        etype = ev.get("type")
        msg = ev.get("message") or {}
        if etype == "assistant":
            for block in msg.get("content") or ():
                if block.get("type") == "tool_use":
                    tools[block["id"]] = {"id": block["id"], "name": block.get("name"),
                                          "t_start": t, "t_end": None, "is_error": False,
                                          "input": _preview(block.get("input")),
                                          "subagent": bool(ev.get("parent_tool_use_id"))}
                    order.append(block["id"])
                elif block.get("type") == "text" and not ev.get("parent_tool_use_id"):
                    texts.append(block.get("text", ""))
        elif etype == "user":
            for block in msg.get("content") or () if isinstance(msg.get("content"), list) else ():
                if block.get("type") == "tool_result" and block.get("tool_use_id") in tools:
                    tool = tools[block["tool_use_id"]]
                    tool["t_end"] = t
                    tool["is_error"] = bool(block.get("is_error"))
                    content = block.get("content")
                    tool["result_chars"] = len(_preview_full(content))
        elif etype == "result":
            result = {k: ev.get(k) for k in ("subtype", "is_error", "duration_ms", "duration_api_ms",
                                              "num_turns", "total_cost_usd", "usage")}
            final_text = ev.get("result") or ""
    return {"calls": _metered_calls(calls_path), "tools": [tools[i] for i in order],
            "final_text": final_text or (texts[-1] if texts else ""),
            "self_reported": result, "parse_errors": parse_errors}


def _preview_full(content: Any) -> str:
    if isinstance(content, str):
        return content
    try:
        return json.dumps(content, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(content)


# ─── fluid event logs (bare store and app) ──────────────────────────────────

_DONE_RE = re.compile(r"\[done\] stop=(\S+) calls=(\d+)(?: cost=\$([\d.]+))?")


def _event_kind(ev: dict[str, Any]) -> str:
    """The event class name, from a store row (``_cls``) or an app SSE row."""
    if ev.get("_cls"):
        return str(ev["_cls"])
    name = str(ev.get("event_type") or ev.get("type") or "")
    return _APP_EVENT_NAMES.get(name, name)


# App SSE event types → fluid event class names. Filled in from the app's
# event log; unknown names pass through unchanged.
_APP_EVENT_NAMES = {
    "tool.use": "ToolUse", "tool_use": "ToolUse",
    "turn.usage": "Usage", "turn.completed": "TurnComplete", "turn.aborted": "TurnComplete",
    "turn.error": "ErrorEvent", "assistant.text_delta": "TextDelta",
    "assistant.thinking_delta": "ThinkingDelta", "tool.args_delta": "ToolArgsDelta",
    "turn.provider_retry": "ProviderRetry", "system.compaction_boundary": "ContextCompaction",
    "tool.result": "ToolResult", "tool_result": "ToolResult",
    "usage": "Usage", "turn.started": "TurnStarted", "turn_started": "TurnStarted",
    "turn.complete": "TurnComplete", "turn_complete": "TurnComplete",
    "text.delta": "TextDelta", "text_delta": "TextDelta",
    "block.start": "BlockStart", "block_start": "BlockStart",
    "error": "ErrorEvent", "provider.retry": "ProviderRetry",
    "context.compaction": "ContextCompaction",
}


def _payload(ev: dict[str, Any]) -> dict[str, Any]:
    data = ev.get("data")
    if isinstance(data, dict):
        merged = dict(data)
        merged.setdefault("ts", ev.get("ts"))
        # App usage rows nest the counters under "usage".
        if isinstance(merged.get("usage"), dict) and "input_tokens" in merged["usage"]:
            u = merged["usage"]
            merged.setdefault("input_tokens", u.get("input_tokens"))
            merged.setdefault("output_tokens", u.get("output_tokens"))
            merged.setdefault("cache_read_tokens", u.get("cache_read_tokens") or u.get("cache_read_input_tokens"))
            merged.setdefault("raw", u)
        if "output" not in merged and "content_preview" in merged:
            merged["output"] = merged["content_preview"]
        return merged
    return ev


def _ts(p: dict[str, Any]) -> float | None:
    for key in ("ts", "timestamp"):
        v = p.get(key)
        if isinstance(v, (int, float)) and v > 0:
            return float(v)
    created = p.get("created_at")
    if isinstance(created, str) and created:
        from datetime import datetime
        try:
            return datetime.fromisoformat(created).timestamp()
        except ValueError:
            return None
    return None


def from_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Tools, derived calls and final text from a fluid event list."""
    tools: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    derived: list[dict[str, Any]] = []
    boundary: float | None = None
    first_output: float | None = None
    texts: dict[str, list[str]] = {}
    last_turn_text: list[str] = []
    turn_starts: list[float] = []
    turn_ends: list[float] = []
    errors: list[str] = []
    compactions = 0
    retries = 0
    for raw in events:
        kind = _event_kind(raw)
        p = _payload(raw)
        t = _ts(p)
        if kind == "TurnStarted":
            if t is not None:
                turn_starts.append(t)
                boundary = t if boundary is None else max(boundary, t)
            last_turn_text = []
        elif kind == "TurnComplete":
            if t is not None:
                turn_ends.append(t)
        elif kind in ("BlockStart", "TextDelta", "ThinkingDelta", "ToolArgsDelta"):
            if first_output is None and t is not None:
                first_output = t
            if kind == "TextDelta":
                last_turn_text.append(str(p.get("text") or ""))
        elif kind == "ToolUse":
            tid = str(p.get("tool_use_id") or len(order))
            tools[tid] = {"id": tid, "name": p.get("name"), "t_start": t, "t_end": None,
                          "is_error": False, "input": _preview(p.get("input"))}
            order.append(tid)
        elif kind == "ToolResult":
            tid = str(p.get("tool_use_id") or "")
            if tid in tools:
                tools[tid]["t_end"] = t
                tools[tid]["is_error"] = bool(p.get("is_error"))
                tools[tid]["result_chars"] = len(_preview_full(p.get("output")))
            if t is not None:
                boundary = t if boundary is None else max(boundary, t)
        elif kind == "Usage":
            raw_usage = p.get("raw") or {}
            derived.append({
                "t_start": boundary, "t_end": t, "t_first": first_output,
                "input": int(p.get("input_tokens", 0) or 0),
                "cached": int(p.get("cache_read_tokens", 0) or 0),
                "output": int(p.get("output_tokens", 0) or 0),
                "reasoning": int(raw_usage.get("reasoning_tokens", 0) or 0)
                if isinstance(raw_usage, dict) else 0,
                "role": "main", "exact": False,
            })
            boundary = t
            first_output = None
        elif kind == "ErrorEvent":
            errors.append(_preview(p.get("message") or p))
        elif kind == "ContextCompaction":
            compactions += 1
        elif kind == "ProviderRetry":
            retries += 1
    return {"calls": derived, "tools": [tools[i] for i in order],
            "final_text": "".join(last_turn_text),
            "turn_starts": turn_starts, "turn_ends": turn_ends,
            "errors": errors, "compactions": compactions, "provider_retries": retries}


def from_fluid_store(store: Path, calls_path: Path, stream_path: Path) -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    for path in sorted(Path(store).glob("*.events.jsonl")):
        events.extend(read_jsonl(path))
    events.sort(key=lambda e: (e.get("ts") or 0, e.get("seq") or 0))
    tr = from_events(events)
    tr["derived_calls"] = tr.pop("calls")
    tr["calls"] = _metered_calls(calls_path)
    done = {}
    for row in read_jsonl(stream_path):
        m = _DONE_RE.search(row.get("line", ""))
        if m:
            done = {"stop": m.group(1), "calls": int(m.group(2)),
                    "cost": float(m.group(3)) if m.group(3) else None}
    tr["self_reported"] = done
    return tr


def from_app_events(events: list[dict[str, Any]], session: dict[str, Any] | None) -> dict[str, Any]:
    tr = from_events(events)
    session = session or {}
    # A turn the app ended on an error (a dead provider sign-in, a harness
    # exception) is not the agent's failure; the runner turns it into a
    # harness_error so the run is redone.
    tr["aborts"] = [str(_payload(e).get("message") or "")[:500] for e in events
                    if e.get("event_type") == "turn.aborted"
                    and _payload(e).get("reason") == "error"]
    tr["subagents"] = [{k: _payload(e).get(k) for k in ("worker_session_id", "agent_name", "description")}
                       for e in events if e.get("event_type") == "subagent.started"]
    tr["self_reported"] = {k: session.get(k) for k in (
        "total_tokens_in", "total_tokens_out", "total_cache_read_tokens",
        "total_cache_creation_tokens", "total_cost_usd", "model", "status")}
    if not tr["final_text"]:
        msgs = [m for m in session.get("messages") or [] if m.get("role") == "assistant"
                and m.get("type") in (None, "text") and m.get("content")]
        if msgs:
            tr["final_text"] = str(msgs[-1].get("content"))[:20000]
    return tr


# ─── checks ─────────────────────────────────────────────────────────────────

_CONTAMINATION = re.compile(
    r"(^|[\s;&|(`])gh\s+(pr|issue|api|repo|search)|github\.com/|api\.github\.com|"
    r"git\s+(fetch|pull|clone|log\s+--all)|/workspace/ai-shuffle(?!-)|harness_h2h|\.fluid/worktrees",
    re.IGNORECASE)


def contamination(tools: list[dict[str, Any]]) -> list[str]:
    """Tool calls that could have reached the real fix (GitHub, the source repo)."""
    hits = []
    for tool in tools or ():
        text = str(tool.get("input") or "")
        if _CONTAMINATION.search(text):
            hits.append(f"{tool.get('name')}: {text[:200]}")
    return hits
