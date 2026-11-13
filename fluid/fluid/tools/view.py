"""Answer a repeat call with a pointer only while the model can still see the
earlier result.

A Read of an unchanged file, a search that finds the same lines, or a Skill
whose file did not change returns content the model already has. Sending it
again doubles its cost for no gain. Saying "it is above" is only true while
the earlier result is still in the request the model reads: once a trim
replaced it with a placeholder, or a fold or a window moved it out, the note
would point at nothing. So the claim is checked against the request itself,
twice:

* **When the call runs.** The engine records which tool results the request
  the model just answered carried exactly as stored
  (:func:`verbatim_tool_results`) and publishes that set as
  ``ToolCtx.view_results`` for the duration of that session's own call. A
  handler records the call that delivered a payload in full
  (:func:`record_delivery`) and, on a repeat, asks whether that call's result
  is in the set (:func:`answer_from_view`). Everything that cannot answer —
  the delegated CLI, a code-mode cell, a host tool call outside the native
  loop — sees ``view_results`` as None, and every repeat is delivered in full.

* **When the pointer is read.** The pointer reaches the model in the NEXT
  request, and a size-pressure trim or fold may run in between. The pointer
  names the result it points at, and :func:`settle_pointers` shows it as a
  "no longer above, call again" note in any request that does not carry that
  result verbatim. The target always sits before the pointer, so this only
  changes the pointer's bytes in a request that already rewrote the target:
  bytes the provider has cached are never disturbed by it.

A pointer spends its delivery record, so making the same call again right
away returns the full content. That is the way back for a model that cannot
find the earlier copy.
"""

from __future__ import annotations

import re
from collections.abc import Hashable, Iterable, Sequence
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from fluid.messages import Message, Text, ToolResultBlock, ToolUseBlock

if TYPE_CHECKING:
    from fluid.store.base import SessionNode
    from fluid.tools.registry import ToolCtx

#: Tools whose repeat results may be answered with a pointer. Only their
#: results are ever settled, so a Bash command that prints the marker text is
#: left alone.
POINTER_TOOLS = frozenset({"Read", "Grep", "Glob", "Skill"})

#: Opening words of every pointer. A session audit counts hits per tool from
#: stored tool results by this prefix; the friction journal hooks it too.
POINTER_MARK = "[already in view:"

#: Opening words of a pointer whose target left the view before it was read.
GONE_MARK = "[no longer in view:"

#: Results shorter than this are sent again: a pointer costs about 300
#: characters, so on a short result it would save little and could confuse.
MIN_POINTED_CHARS = 1_000

_TARGET_RE = re.compile(r"\(earlier result ([^\s()]+)\)")


def verbatim_tool_results(
    nodes: Iterable["SessionNode"], history: Iterable[Message],
) -> frozenset[str]:
    """Ids of the tool results ``history`` carries exactly as ``nodes`` store them.

    A result a trim replaced, an image consumed out of it, steers re-rendered
    into it, or a fold or window that dropped it does not match its stored
    block, so it is not counted. Only exact copies are.
    """
    stored: dict[str, ToolResultBlock] = {}
    for node in nodes:
        message = node.message
        if message.role != "user":
            continue
        for block in message.content:
            if isinstance(block, ToolResultBlock):
                stored[block.tool_use_id] = block
    found: set[str] = set()
    for message in history:
        if message.role != "user":
            continue
        for block in message.content:
            if not isinstance(block, ToolResultBlock):
                continue
            original = stored.get(block.tool_use_id)
            if original is not None and (block is original or block == original):
                found.add(block.tool_use_id)
    return frozenset(found)


def _call_id(ctx: "ToolCtx") -> str | None:
    """The id of the call being answered, on a surface whose results reach the
    model whole (the native JSON tool path). None anywhere a pointer could lie."""
    if ctx.result_budget is not None or ctx.view_results is None:
        return None
    call_id = (ctx.extra or {}).get("tool_use_id")
    return call_id if isinstance(call_id, str) and call_id else None


def checks_view(ctx: "ToolCtx") -> bool:
    """Whether this call's surface can tell what the model still sees."""
    return _call_id(ctx) is not None


def record_delivery(ctx: "ToolCtx", key: Hashable, version: Any) -> None:
    """Remember that the current call delivered ``key`` at ``version`` in full."""
    call_id = _call_id(ctx)
    if call_id is None or version is None:
        ctx.deliveries.pop(key, None)
        return
    ctx.deliveries[key] = (call_id, version)


def answer_from_view(
    ctx: "ToolCtx", key: Hashable, version: Any, size: int,
) -> str | None:
    """The id of the earlier call this one may point at, or None to deliver.

    It is returned only when ``key`` was delivered in full at ``version`` by a
    call whose result the model's current request still carries verbatim,
    and the result (``size`` characters) is long enough for a pointer to save
    something. A returned id is spent: the delivery record is dropped, so the
    same call made again returns the full content. A record whose version
    moved is dropped too, so it never outlives the content it described.
    """
    if version is None or size < MIN_POINTED_CHARS or _call_id(ctx) is None:
        return None
    entry = ctx.deliveries.get(key)
    if entry is None:
        return None
    target, seen = entry
    if seen == version and target not in (ctx.view_results or ()):
        return None
    del ctx.deliveries[key]
    return target if seen == version else None


def pointer(what: str, where: str, target: str) -> str:
    """The text that replaces a repeat result: what is not repeated, where the
    earlier copy is, and how to get the content back if it is gone."""
    return (
        f"{POINTER_MARK} {what} is unchanged since {where} (earlier result "
        f"{target}), which is still above in this conversation, so it is not "
        "repeated here. If you cannot find it, make the same call again and "
        "the full result comes back.]"
    )


def _gone(target: str) -> str:
    return (
        f"{GONE_MARK} this call matched an earlier result (earlier result "
        f"{target}), but that result has since been trimmed or summarized out "
        "of the conversation. Make the same call again and the full result "
        "comes back.]"
    )


def _pointer_target(block: ToolResultBlock) -> str | None:
    if block.is_error or len(block.content) != 1:
        return None
    only = block.content[0]
    if not isinstance(only, Text) or not only.text.startswith(POINTER_MARK):
        return None
    match = _TARGET_RE.search(only.text)
    return match.group(1) if match else None


def settle_pointers(
    history: Sequence[Message], in_view: frozenset[str],
) -> list[Message]:
    """``history`` with each pointer whose target is not in ``in_view`` shown
    as a "no longer in view" note. Messages without such a pointer are
    returned as the same objects; the input is never mutated."""
    names: dict[str, str] = {}
    out: list[Message] = []
    for message in history:
        if message.role == "assistant":
            for block in message.content:
                if isinstance(block, ToolUseBlock):
                    names[block.id] = block.name
            out.append(message)
            continue
        changed = False
        content: list[Any] = []
        for block in message.content:
            if (
                isinstance(block, ToolResultBlock)
                and names.get(block.tool_use_id) in POINTER_TOOLS
            ):
                target = _pointer_target(block)
                if target is not None and target not in in_view:
                    block = replace(block, content=[Text(_gone(target))])
                    changed = True
            content.append(block)
        out.append(replace(message, content=content) if changed else message)
    return out
