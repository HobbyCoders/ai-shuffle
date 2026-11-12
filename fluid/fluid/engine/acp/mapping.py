"""Map ACP ``session/update`` payloads onto fluid's event vocabulary.

Pure bookkeeping: :class:`TurnMapper` turns one update into the fluid events
it implies and keeps the turn's assistant content so the engine can store it
as transcript nodes. No I/O, so tests drive it directly.
"""

from __future__ import annotations

import difflib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from fluid.events.model import (
    BlockStart,
    BlockStop,
    Event,
    TextDelta,
    ThinkingDelta,
    ToolResult,
    ToolUse,
)
from fluid.messages.content import Text, Thinking, ToolResultBlock, ToolUseBlock

# ACP ``ToolKind`` → the tool name the app's transcript already knows how to
# render. ``title`` and the raw input ride along in the input.
KIND_TOOL_NAMES = {
    "execute": "Bash",
    "read": "Read",
    "edit": "Edit",
    "delete": "Delete",
    "move": "Move",
    "search": "Grep",
    "fetch": "WebFetch",
    "think": "Think",
    "switch_mode": "SwitchMode",
}
PLAN_TOOL_NAME = "TodoWrite"
_PLAN_STATUS = {"pending": "pending", "in_progress": "in_progress", "completed": "completed"}


def _text_of(content: Any) -> str:
    """Text of an ACP ContentBlock (or a list of them)."""
    if isinstance(content, list):
        return "".join(_text_of(c) for c in content)
    if not isinstance(content, Mapping):
        return ""
    kind = content.get("type")
    if kind == "text":
        return str(content.get("text") or "")
    if kind == "resource_link":
        return f"[{content.get('name') or content.get('uri') or 'resource'}]({content.get('uri') or ''})"
    if kind == "resource":
        resource = content.get("resource") or {}
        return str(resource.get("text") or resource.get("uri") or "")
    if kind in ("image", "audio"):
        return f"[{kind}]"
    return ""


def render_diff(path: str, old: str | None, new: str) -> str:
    lines = difflib.unified_diff(
        (old or "").splitlines(keepends=True), (new or "").splitlines(keepends=True),
        fromfile=f"a/{path}" if old is not None else "/dev/null", tofile=f"b/{path}",
    )
    return "".join(lines)


def tool_call_content_text(items: Any) -> str:
    """Text of a ToolCallContent list: content blocks, diffs, terminals."""
    parts: list[str] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, Mapping):
            continue
        kind = item.get("type")
        if kind == "content":
            text = _text_of(item.get("content"))
            if text:
                parts.append(text)
        elif kind == "diff":
            parts.append(render_diff(str(item.get("path") or ""), item.get("oldText"),
                                     str(item.get("newText") or "")))
        elif kind == "terminal":
            parts.append(f"[terminal {item.get('terminalId') or ''}]")
    return "\n".join(p for p in parts if p)


def _first_diff(items: Any) -> Mapping[str, Any] | None:
    for item in items if isinstance(items, list) else []:
        if isinstance(item, Mapping) and item.get("type") == "diff":
            return item
    return None


def _jsonable(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return str(value)


@dataclass
class _ToolState:
    tool_use_id: str
    name: str
    input: dict[str, Any]
    started: bool = False
    finished: bool = False
    content: list[Any] = field(default_factory=list)


class TurnMapper:
    """One turn's mapping state.

    ``bridged_name`` resolves a tool call to the registry name the bundled
    MCP bridge serves (None when it is one of the agent's own tools).
    """

    def __init__(self, *, session_id: str, turn_id: str,
                 bridged_name: Callable[[Mapping[str, Any]], str | None] | None = None) -> None:
        self.session_id = session_id
        self.turn_id = turn_id
        self.bridged_name = bridged_name
        self._next_index = 0
        self._open_index: int | None = None
        self._open_kind: str | None = None
        self._open_text: list[str] = []
        self.tools: dict[str, _ToolState] = {}
        # Transcript: a list of ("assistant", blocks) / ("tool_results", blocks).
        self._assistant_blocks: list[Any] = []
        self._segments: list[tuple[str, list[Any]]] = []
        self.text_parts: list[str] = []
        self.plan_count = 0
        self.mode_id: str | None = None
        self.config_options: list[dict[str, Any]] | None = None
        self.usage_update: dict[str, Any] | None = None
        self.commands: list[dict[str, Any]] | None = None

    def _ev(self, cls: type[Event], **kw: Any) -> Event:
        return cls(session_id=self.session_id, turn_id=self.turn_id, **kw)

    # ─── blocks ─────────────────────────────────────────────────────────

    def close_block(self) -> list[Event]:
        if self._open_index is None:
            return []
        index, kind = self._open_index, self._open_kind
        text = "".join(self._open_text)
        self._open_index = self._open_kind = None
        self._open_text = []
        if text:
            self._assistant_blocks.append(Text(text) if kind == "text" else Thinking(text))
        return [self._ev(BlockStop, index=index)]

    def _stream(self, kind: str, text: str) -> list[Event]:
        if not text:
            return []
        out: list[Event] = []
        if self._open_kind != kind:
            out.extend(self.close_block())
            self._open_index = self._next_index
            self._next_index += 1
            self._open_kind = kind
            out.append(self._ev(BlockStart, index=self._open_index, block_type=kind))
        self._open_text.append(text)
        if kind == "text":
            self.text_parts.append(text)
            out.append(self._ev(TextDelta, index=self._open_index, text=text))
        else:
            out.append(self._ev(ThinkingDelta, index=self._open_index, text=text))
        return out

    # ─── tools ──────────────────────────────────────────────────────────

    def tool_name_and_input(self, call: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        raw = call.get("rawInput")
        data: dict[str, Any] = dict(raw) if isinstance(raw, Mapping) else (
            {} if raw is None else {"input": _jsonable(raw)}
        )
        bridged = self.bridged_name(call) if self.bridged_name is not None else None
        if bridged:
            return bridged, data
        kind = str(call.get("kind") or "other")
        name = KIND_TOOL_NAMES.get(kind) or str(call.get("title") or "Tool")
        title = call.get("title")
        if title and "description" not in data:
            data["description"] = str(title)
        diff = _first_diff(call.get("content"))
        if diff is not None:
            data.setdefault("file_path", str(diff.get("path") or ""))
            data.setdefault("old_string", diff.get("oldText") or "")
            data.setdefault("new_string", diff.get("newText") or "")
            if diff.get("oldText") is None and name == "Edit":
                name = "Write"
                data.setdefault("content", diff.get("newText") or "")
        locations = call.get("locations")
        if isinstance(locations, list) and locations and "file_path" not in data:
            first = locations[0] if isinstance(locations[0], Mapping) else {}
            if first.get("path"):
                data["file_path"] = str(first["path"])
        return name, data

    def _start_tool(self, call: Mapping[str, Any]) -> list[Event]:
        tool_id = str(call.get("toolCallId") or "")
        if not tool_id:
            return []
        state = self.tools.get(tool_id)
        out = self.close_block()
        if state is None:
            name, data = self.tool_name_and_input(call)
            state = _ToolState(tool_use_id=tool_id, name=name, input=data)
            self.tools[tool_id] = state
        if not state.started:
            state.started = True
            self._assistant_blocks.append(ToolUseBlock(id=tool_id, name=state.name, input=state.input))
            out.append(self._ev(ToolUse, tool_use_id=tool_id, name=state.name, input=state.input))
        return out

    def _update_tool(self, update: Mapping[str, Any]) -> list[Event]:
        tool_id = str(update.get("toolCallId") or "")
        state = self.tools.get(tool_id)
        out: list[Event] = []
        if state is None or not state.started:
            out.extend(self._start_tool(update))
            state = self.tools.get(tool_id)
            if state is None:
                return out
        if isinstance(update.get("content"), list):
            state.content = list(update["content"])
        status = update.get("status")
        if status in ("completed", "failed") and not state.finished:
            state.finished = True
            text = tool_call_content_text(state.content)
            if not text and update.get("rawOutput") is not None:
                raw = update["rawOutput"]
                text = raw if isinstance(raw, str) else json.dumps(_jsonable(raw))
            if not text:
                text = "(failed)" if status == "failed" else "(done)"
            out.extend(self._finish_assistant())
            self._segments.append(("tool_results", [ToolResultBlock(
                tool_use_id=tool_id, content=text, is_error=status == "failed",
            )]))
            out.append(self._ev(ToolResult, tool_use_id=tool_id, name=state.name,
                                output=text, is_error=status == "failed"))
        return out

    def _finish_assistant(self) -> list[Event]:
        out = self.close_block()
        if self._assistant_blocks:
            self._segments.append(("assistant", self._assistant_blocks))
            self._assistant_blocks = []
        return out

    def _plan(self, entries: Any) -> list[Event]:
        todos = []
        for entry in entries if isinstance(entries, list) else []:
            if isinstance(entry, Mapping) and entry.get("content"):
                content = str(entry["content"])
                todos.append({"content": content, "activeForm": content,
                              "status": _PLAN_STATUS.get(str(entry.get("status")), "pending")})
        self.plan_count += 1
        tool_id = f"acp-plan-{self.turn_id}-{self.plan_count}"
        out = self.close_block()
        data = {"todos": todos}
        self._assistant_blocks.append(ToolUseBlock(id=tool_id, name=PLAN_TOOL_NAME, input=data))
        out.append(self._ev(ToolUse, tool_use_id=tool_id, name=PLAN_TOOL_NAME, input=data))
        out.extend(self._finish_assistant())
        result = "Plan updated."
        self._segments.append(("tool_results", [ToolResultBlock(tool_use_id=tool_id, content=result)]))
        out.append(self._ev(ToolResult, tool_use_id=tool_id, name=PLAN_TOOL_NAME, output=result))
        return out

    # ─── entry point ────────────────────────────────────────────────────

    def map_update(self, update: Mapping[str, Any]) -> list[Event]:
        kind = update.get("sessionUpdate")
        if kind == "agent_message_chunk":
            return self._stream("text", _text_of(update.get("content")))
        if kind == "agent_thought_chunk":
            return self._stream("thinking", _text_of(update.get("content")))
        if kind == "tool_call":
            out = self._start_tool(update)
            if update.get("status") in ("completed", "failed"):
                out.extend(self._update_tool(update))
            return out
        if kind == "tool_call_update":
            return self._update_tool(update)
        if kind == "plan":
            return self._plan(update.get("entries"))
        if kind == "current_mode_update":
            self.mode_id = str(update.get("currentModeId") or "") or None
        elif kind == "config_option_update":
            options = update.get("configOptions")
            if isinstance(options, list):
                self.config_options = [o for o in options if isinstance(o, dict)]
        elif kind == "usage_update":
            self.usage_update = dict(update)
        elif kind == "available_commands_update":
            commands = update.get("availableCommands")
            if isinstance(commands, list):
                self.commands = [c for c in commands if isinstance(c, dict)]
        # user_message_chunk (our own prompt echoed), session_info_update and
        # unknown kinds carry nothing the transcript shows.
        return []

    def finish(self) -> tuple[list[Event], list[tuple[str, list[Any]]]]:
        """Close the turn: trailing events and the transcript segments."""
        out = self._finish_assistant()
        segments, self._segments = self._segments, []
        return out, segments

    @property
    def text(self) -> str:
        return "".join(self.text_parts)


_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]")


def safe_server_name(name: str) -> str:
    return _UNSAFE_NAME.sub("_", name)[:64] or "server"
