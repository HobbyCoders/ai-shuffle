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
  names the result it points at, and :func:`settle_pointers` rewrites the
  pointer's own words as a "no longer above, call again" note in any request
  that does not carry that result verbatim; a steer, hook context or any
  other block riding on the same result is kept as it was. The target always
  sits before the pointer, so this only
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

#: A tool-use id a pointer can name and settlement can read back. A call
#: whose id has spaces or parentheses gets its content in full instead.
_ID_RE = re.compile(r"[^\s()]+")

#: Every pointer opens with the mark and the target, before any label the
#: tool interpolates (a path, a Grep pattern), so a label can never stand in
#: for the target.
_HEAD_RE = re.compile(re.escape(POINTER_MARK) + r" \(earlier result ([^\s()]+)\) ")


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
    return target if seen == version and _ID_RE.fullmatch(target) else None


#: Closing words of every pointer: settlement rewrites the text up to and
#: including them and keeps whatever the engine appended after (a steer, hook
#: context). A label that contains them is neutralized in :func:`pointer`, so
#: the first occurrence after the head is always the real end.
_POINTER_END = "make the same call again and the full result comes back.]"
_NEUTRAL_END = _POINTER_END[:-1] + ")"


def pointer(what: str, where: str, target: str) -> str:
    """The text that replaces a repeat result: what is not repeated, where the
    earlier copy is, and how to get the content back if it is gone."""
    what = what.replace(_POINTER_END, _NEUTRAL_END)
    where = where.replace(_POINTER_END, _NEUTRAL_END)
    return (
        f"{POINTER_MARK} (earlier result {target}) {what} is unchanged since "
        f"{where}, which is still above in this conversation, so it is not "
        f"repeated here. If you cannot find it, {_POINTER_END}"
    )


def _gone(target: str) -> str:
    return (
        f"{GONE_MARK} (earlier result {target}) this call matched an earlier "
        "result, but that result has since been trimmed or summarized out of "
        "the conversation. Make the same call again and the full result "
        "comes back.]"
    )


def _settled(block: ToolResultBlock, in_view: frozenset[str]) -> ToolResultBlock:
    """``block`` with its pointer rewritten as gone when the pointer's target
    is not in ``in_view``; otherwise ``block`` itself.

    The handler's pointer is always the start of the result's first text
    block. Only that span is rewritten: text the engine appended to it (a
    delivered steer, PostToolUse hook context) and every other block (a
    nested-instruction reminder, the reflection nudge, question answers,
    images) are kept, as are the delivery records.
    """
    if block.is_error or not block.content:
        return block
    first = block.content[0]
    if not isinstance(first, Text):
        return block
    match = _HEAD_RE.match(first.text)
    if match is None or match.group(1) in in_view:
        return block
    end = first.text.find(_POINTER_END, match.end())
    if end < 0:
        return block
    end += len(_POINTER_END)
    text = _gone(match.group(1)) + first.text[end:]
    return replace(block, content=[replace(first, text=text), *block.content[1:]])


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
                settled = _settled(block, in_view)
                changed = changed or settled is not block
                block = settled
            content.append(block)
        out.append(replace(message, content=content) if changed else message)
    return out
