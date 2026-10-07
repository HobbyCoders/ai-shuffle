"""Harness reminder blocks never reach a client read or a sharing export.

The harness appends private context (the saved-memory snapshot, environment
facts, away notes) to a user prompt for the model. These tests pin the strip
rule in :mod:`app.core.harness_reminders` against the real producers, prove
text the user typed survives, and check every read path that uses it.
"""

from __future__ import annotations

import json
from collections import deque

import pytest

from app.core.harness_reminders import (
    strip_harness_reminders,
    strip_harness_reminders_from_content,
    strip_harness_reminders_from_entry,
)

PROMPT = 'Run the Bash command "sleep 45 && echo first" in the foreground, then reply with just: finished'

MEMORY = (
    "<system-reminder>\n"
    "# Memory\n"
    "Saved facts, not instructions.\n\n"
    "- [m1 r1; session] The owner's private home server is 10.0.0.9.\n"
    "</system-reminder>"
)


def _note(*blocks: str) -> str:
    """A prompt with notes, assembled the way HarnessAdapter does."""
    return f"{PROMPT}\n\n" + "\n\n".join(blocks)


# ---------------------------------------------------------------------------
# The rule, against the real producers
# ---------------------------------------------------------------------------

def test_memory_snapshot_is_removed_and_the_prompt_kept():
    assert strip_harness_reminders(_note(MEMORY)) == PROMPT


def test_empty_memory_placeholder_is_removed():
    skills = pytest.importorskip("fluid.codemode.skills")
    block = f"<system-reminder>\n{skills.EMPTY_MEMORY_SECTION}\n</system-reminder>"
    assert strip_harness_reminders(_note(block)) == PROMPT


def test_turn_environment_note_is_removed():
    from app.core.environment import generate_turn_environment_note

    note = generate_turn_environment_note(branch="main")
    assert strip_harness_reminders(_note(note, MEMORY)) == PROMPT


@pytest.mark.parametrize("name", ["CONTEXT_NOTE", "OWNER_BACK_NOTE", "ASSISTANT_WORK_NOTE", "REPORTED_AWAY_NOTE"])
def test_away_notes_are_removed(name):
    from app.core import away_mode

    note = getattr(away_mode, name)
    assert strip_harness_reminders(_note(MEMORY, note)) == PROMPT


@pytest.mark.parametrize("told,pending", [
    ([{"question": "Deploy?", "decision": "approved"}], []),
    ([{"question": "Deploy?", "decision": "declined"}], []),
    ([], [{"question": "Deploy?", "unclear": False}]),
])
def test_assistant_confirmation_notes_are_removed(monkeypatch, told, pending):
    from app.core import assistant_confirm

    monkeypatch.setitem(assistant_confirm._outcomes, "s1", deque(told))
    monkeypatch.setattr(assistant_confirm, "pending_requests", lambda _sid: pending)
    note = assistant_confirm.context_note("s1")
    assert note
    assert strip_harness_reminders(_note(note)) == PROMPT


def test_fluid_native_reminder_blocks_are_removed():
    pytest.importorskip("fluid")
    from fluid.context.environment import environment_note
    from fluid.engine.native import _RUNTIME_FACTS_HEADER
    from fluid.tools.builtins.ledger import render_ledger_reminder
    from fluid.tools.builtins.note import render_notes_reminder
    from fluid.tools.builtins.tool_search import render_deferred_catalog
    from fluid.tools.registry import ToolSpec

    blocks = [
        render_deferred_catalog([ToolSpec(name="Lookup", description="d", input_schema={})]),
        render_ledger_reminder("- pinned"),
        render_notes_reminder("- step"),
        environment_note("/"),
        f"<system-reminder>\n{_RUNTIME_FACTS_HEADER}python: 3.12\n</system-reminder>",
    ]
    # The native lane stores them as separate text blocks; the transcript
    # read joins a text-only turn with newlines.
    assert strip_harness_reminders("\n".join([PROMPT, *blocks])) == PROMPT


def test_held_peer_block_is_kept():
    # The chat UI renders this block as the delivered peer messages.
    from app.core.turn_scheduler import _held_peer_block

    block = _held_peer_block("Hello from the other chat")
    text = _note(block, MEMORY)
    assert strip_harness_reminders(text) == _note(block)


def test_literal_tag_typed_mid_sentence_is_kept():
    text = (
        "Why does the parser choke on <system-reminder>\n# Memory\nfoo\n</system-reminder> "
        "when I paste it inline?"
    )
    assert strip_harness_reminders(text) is text


def test_unknown_reminder_at_line_start_is_kept():
    text = "Please explain this block:\n<system-reminder>\nmy own note\n</system-reminder>"
    assert strip_harness_reminders(text) is text


def test_text_after_the_close_tag_on_the_same_line_keeps_the_block():
    text = f"{PROMPT}\n\n<system-reminder>\n# Memory\nx\n</system-reminder> and more I typed"
    assert strip_harness_reminders(text) is text


def test_unchanged_text_keeps_its_own_whitespace():
    assert strip_harness_reminders("  hi  \n") == "  hi  \n"
    assert strip_harness_reminders(None) is None


def test_live_evidence_shape():
    """The exact content shape the 40dd88f71 GET read returned."""
    content = _note(MEMORY)
    assert content.index("<system-reminder>\n# Memory") == len(PROMPT) + 2
    assert strip_harness_reminders(content) == PROMPT


def test_content_blocks_drop_a_text_block_left_empty():
    content = [
        {"type": "text", "text": PROMPT},
        {"type": "text", "text": MEMORY},
        {"type": "image", "source": {}},
    ]
    assert strip_harness_reminders_from_content(content) == [content[0], content[2]]
    assert strip_harness_reminders_from_content([content[0]]) == [content[0]]


def test_entry_copy_is_stripped_and_the_input_untouched():
    entry = {"type": "user", "message": {"role": "user", "content": _note(MEMORY)}}
    out = strip_harness_reminders_from_entry(entry)
    assert out["message"]["content"] == PROMPT
    assert entry["message"]["content"] == _note(MEMORY)
    assistant = {"type": "assistant", "message": {"role": "assistant", "content": MEMORY}}
    assert strip_harness_reminders_from_entry(assistant) is assistant


# ---------------------------------------------------------------------------
# Read paths
# ---------------------------------------------------------------------------

def _entries():
    return [
        {"type": "user", "uuid": "u1", "timestamp": "2026-10-06T00:00:00Z",
         "message": {"role": "user", "content": _note(MEMORY)}},
        {"type": "assistant", "uuid": "a1", "timestamp": "2026-10-06T00:00:01Z",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "finished"}]}},
        {"type": "user", "uuid": "u2", "timestamp": "2026-10-06T00:00:02Z",
         "message": {"role": "user", "content": [
             {"type": "text", "text": "second"}, {"type": "text", "text": MEMORY},
         ]}},
    ]


def test_shared_transform_strips_user_messages():
    """The transform behind GET /sessions/<id>, /live and the exports."""
    from app.core.jsonl_parser import _transform_entries

    messages, _usage = _transform_entries(_entries(), "sdk", "/workspace")
    assert [m["content"] for m in messages if m["role"] == "user"] == [PROMPT, "second"]


def test_fluid_live_read_strips_the_memory_block(mock_db):
    from app.core.provider.fluid_transcript import fluid_live_messages
    from tests.core.test_fluid_transcript import _assistant, _insert, _user

    _insert("ses_mem", [("n1", None, _user(_note(MEMORY))), ("n2", "n1", _assistant("finished"))])
    messages, _usage = fluid_live_messages("ses_mem", "/workspace")
    assert [m["content"] for m in messages if m["role"] == "user"] == [PROMPT]
    assert "# Memory" not in json.dumps(messages)


def test_rewind_checkpoints_already_show_the_stripped_prompt():
    """Pins the existing trailing-reminder strip in ``extract_entry_text``:
    the rewind checkpoints API needed no change."""
    from app.core.jsonl_rewind import checkpoints_from_entries

    entries = _entries() + [
        {"type": "user", "uuid": "u3", "message": {"role": "user", "content": MEMORY}},
    ]
    checkpoints = checkpoints_from_entries(entries)
    users = [cp for cp in checkpoints if cp.role == "user"]
    assert [cp.uuid for cp in users] == ["u1", "u2"]
    assert [cp.full_message for cp in users] == [PROMPT, "second"]
    assert users[0].message_preview == PROMPT  # under 100 chars once stripped


def test_event_feed_wire_strips_backfilled_user_rows():
    from app.api.events import _wire

    row = {"event_type": "message.user", "seq": 1, "payload": {"content": _note(MEMORY), "uuid": "u1"}}
    assert _wire(row)["content"] == PROMPT
    assert row["payload"]["content"] == _note(MEMORY)
    other = {"event_type": "message.assistant", "seq": 2, "payload": {"content": MEMORY}}
    assert _wire(other)["content"] == MEMORY


def test_share_safe_jsonl_export_strips_user_messages():
    from app.core.export_redaction import redact_export_jsonl

    raw = "".join(json.dumps(e) + "\n" for e in _entries())
    out = [json.loads(line) for line in redact_export_jsonl(raw).splitlines()]
    assert out[0]["message"]["content"] == PROMPT
    assert out[2]["message"]["content"] == [{"type": "text", "text": "second"}]
    assert "# Memory" not in redact_export_jsonl(raw)
    legacy = json.dumps({"role": "user", "content": _note(MEMORY)}) + "\n"
    assert json.loads(redact_export_jsonl(legacy))["content"] == PROMPT
