"""Arg elision: shrink the inputs of old tool calls whose results are trimmed.

Tier 1 replaces an old tool result with a placeholder, but the call that
produced it still carries its full input. For code-mode sessions that input is
the whole ``run_python`` cell, often as large as the result it produced. This
module swaps such an input for a small JSON summary object::

    {"_elided": true, "summary": "<first line, 120 chars>", "chars": N,
     "node": "<assistant node id>"}

It is a view policy layered on tier 1, behind ``CompactionConfig.elide_old_args``
(off by default), and it follows the same invariants:

* **No new decisions.** A call is elided exactly when its result is in the
  trimmer's ``_trimmed`` set on the rendered path. Nothing is persisted here:
  the trimmed set already is, so resume replays the same view byte for byte and
  elision lands in the same batch rewrite as the trim that enabled it.
* **The tree is never mutated.** The stored ``ToolUseBlock`` keeps its input;
  ``HistoryExpand`` on the named node returns it in full.
* **Errors stay verbatim.** Tier 1 never trims an error result, so the call
  behind an error is never elided either.
* **Schema-valid.** The replacement is a plain JSON object, so both serializers
  (Anthropic ``tool_use.input`` and the Responses ``function_call.arguments``
  string) accept it unchanged.
* **Never grows the prompt.** Inputs under ``MIN_ELIDE_CHARS`` stay as they
  are, and so does any input whose summary would not be shorter.
"""

from __future__ import annotations

import json
from collections.abc import Container, Iterable
from dataclasses import replace
from typing import Any

from fluid.messages import Message, ToolUseBlock

ELIDED_KEY = "_elided"
# Inputs smaller than this are left alone: a short path or command is worth
# more to the model than the few tokens eliding it would save.
MIN_ELIDE_CHARS = 400
SUMMARY_CHARS = 120


def _dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""


def summarize_input(input_: dict[str, Any]) -> str:
    """The first non-empty line of the input's longest string field.

    For ``run_python`` that is the first line of the cell; for Write or Edit it
    is the first line of the new text. Inputs with no string field fall back
    to their compact JSON. Clipped to ``SUMMARY_CHARS``.
    """
    strings = [v for v in input_.values() if isinstance(v, str)]
    text = _first_line(max(strings, key=len)) if strings else _dumps(input_)
    if len(text) > SUMMARY_CHARS:
        text = text[: SUMMARY_CHARS - 1] + "\u2026"
    return text


def elided_input(input_: dict[str, Any], node_id: str) -> dict[str, Any] | None:
    """The summary object that replaces ``input_``, or None to keep it."""
    if not isinstance(input_, dict) or input_.get(ELIDED_KEY) is True:
        return None
    chars = len(_dumps(input_))
    if chars < MIN_ELIDE_CHARS:
        return None
    elided = {
        ELIDED_KEY: True,
        "summary": summarize_input(input_),
        "chars": chars,
        "node": node_id,
    }
    return elided if len(_dumps(elided)) < chars else None


def elidable_call_ids(
    trimmed: Iterable[tuple[str, str]], on_path: Container[str]
) -> frozenset[str]:
    """Call ids whose result is trimmed on the rendered path.

    ``trimmed`` holds (result node id, tool_use_id) pairs. A pair whose result
    node is off the current path (a sibling branch after a fork) does not
    count, so a call is only elided when the model also sees its placeholder.
    """
    return frozenset(tuid for nid, tuid in trimmed if nid in on_path)


def elide_tool_args(
    node_id: str, message: Message, call_ids: Container[str]
) -> Message:
    """``message`` with the inputs of the named calls elided.

    Returns ``message`` itself when nothing changes, so an unaffected message
    stays the identical object and renders byte-identical.
    """
    if message.role != "assistant" or not call_ids:
        return message
    blocks: list[Any] = []
    changed = False
    for block in message.content:
        if isinstance(block, ToolUseBlock) and block.id in call_ids:
            elided = elided_input(block.input, node_id)
            if elided is not None:
                blocks.append(replace(block, input=elided))
                changed = True
                continue
        blocks.append(block)
    if not changed:
        return message
    return Message(
        role=message.role, content=blocks, provider_meta=message.provider_meta
    )
