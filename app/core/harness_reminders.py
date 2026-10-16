"""Strip harness-injected ``<system-reminder>`` blocks from user-message text.

The harness appends private context to a user prompt before the model sees
it: the saved-memory snapshot (``# Memory``) or its change list, the per-turn environment facts,
the away-mode and assistant-confirmation notes, and on the native lane the
ledger, notes, runtime-facts and deferred-tool catalog blocks. The model must
keep seeing them, but they are not what the person typed, and the memory
snapshot holds private facts that must not leave the installation through a
shared transcript. Every read that hands message content to a client or an
export calls :func:`strip_harness_reminders`; the stored transcript and the
prompt the model receives are never changed.

The rule, chosen so text the user typed is never dropped:

* a block is removed only when it starts at the beginning of a line (start of
  the text or right after a newline), is exactly ``<system-reminder>`` + newline
  + body + newline + ``</system-reminder>``, and its close tag ends a line;
* the body's first line must start with one of the known harness headers in
  :data:`HARNESS_REMINDER_OPENERS`. Any other reminder is kept, in particular
  the held-peer-message block the chat UI renders as delivered messages;
* the literal text ``<system-reminder>`` anywhere else, for example typed in
  the middle of a sentence, is left alone.

Whitespace around the message is trimmed only when a block was removed.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

# First words of every reminder the harness writes into a user turn. Each is
# pinned to its producer by tests (tests/core/test_harness_reminders.py), so
# a reworded producer fails a test instead of leaking silently.
HARNESS_REMINDER_OPENERS = (
    # app.core.provider.harness_adapter memory snapshot / fluid skills
    # render_memory_section (EMPTY_MEMORY_SECTION too).
    "# Memory\n",
    # app.core.memory_reminder CHANGES_HEADER: the change list a turn carries
    # instead of the full snapshot when saved facts changed mid-session.
    "# Memory changes\n",
    # app.core.environment.TURN_ENV_HEADER
    "Environment facts that change while a session runs",
    # app.core.away_mode CONTEXT_NOTE, OWNER_BACK_NOTE, ASSISTANT_WORK_NOTE,
    # REPORTED_AWAY_NOTE
    "The owner is away. Do not ask questions",
    "The owner is back. Away mode is OFF.",
    "The owner's assistant started this chat.",
    "Your final reply goes back to the owner's assistant automatically",
    # app.core.assistant_confirm note lines
    "The owner answered yes, so the app approved",
    "The app declined this request",
    "Waiting for the owner's decision.",
    # fluid native lane: ledger, notes, runtime facts, environment, catalog
    "Session ledger (",
    "# Harness scratch\n",
    "Session notes (",
    "Runtime facts as of this message (",
    "Environment as of this message (",
    "The following deferred tools are available.",
)

_OPEN = "<system-reminder>"

_BLOCK_RE = re.compile(
    r"(?:\n+|^)<system-reminder>\n(?:"
    + "|".join(re.escape(o) for o in HARNESS_REMINDER_OPENERS)
    + r")[\s\S]*?\n</system-reminder>(?=\n|\Z)"
)


def strip_harness_reminders(text: Any) -> Any:
    """``text`` without harness reminder blocks (see the module rule).

    Non-strings are returned unchanged, so callers can pass message content of
    any shape. A string that held nothing but reminders becomes ``""``.
    """
    if not isinstance(text, str) or _OPEN not in text:
        return text
    stripped = _BLOCK_RE.sub("", text)
    return stripped.strip() if stripped != text else text


def strip_harness_reminders_from_content(content: Any) -> Any:
    """Message ``content`` (a string or a list of blocks) with reminder blocks
    stripped from its text. Text blocks left empty are dropped; other blocks
    are untouched. Returns the original object when nothing changed."""
    if isinstance(content, str):
        return strip_harness_reminders(content)
    if not isinstance(content, list):
        return content
    out = []
    changed = False
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            new = strip_harness_reminders(text)
            if new is not text:
                changed = True
                if not new:
                    continue
                block = {**block, "text": new}
        out.append(block)
    return out if changed else content


def strip_harness_reminders_from_entry(entry: Mapping[str, Any]) -> Mapping[str, Any]:
    """A raw transcript ``entry`` with reminder blocks stripped from a user
    message's content (a copy when changed; the input is never mutated).
    An already-rendered legacy row (``role``/``content``, no ``message``) is
    handled too."""
    if "message" not in entry and entry.get("role") == "user":
        content = entry.get("content")
        new = strip_harness_reminders_from_content(content)
        return entry if new is content else {**entry, "content": new}
    if entry.get("type") != "user":
        return entry
    message = entry.get("message")
    if not isinstance(message, dict) or message.get("role") != "user":
        return entry
    content = message.get("content")
    new = strip_harness_reminders_from_content(content)
    if new is content:
        return entry
    out: dict[str, Any] = dict(entry)
    out["message"] = {**message, "content": new}
    return out
