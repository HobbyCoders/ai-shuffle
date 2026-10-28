"""A Claude Code re-seed starts from the newest compaction, whichever provider
made it, and never hands the CLI more than it compacts at.

A chat that compacted on the native loop and then switched to Claude Code was
re-seeded with its whole uncompacted history (the native decisions were
skipped), and the first Claude request overflowed the window. These tests
drive ``reseed_delegated_cli_session``, the function every switch, worker
move, fork and rewind calls.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from app.core.provider import fluid_transcript
from tests.core.test_fluid_transcript import (
    _cli_compaction,
    _evt,
    _insert,
    _insert_events,
    _read_jsonl,
    _seed,
    _user,
)
from tests.helpers import fluid_message

pytest.importorskip("fluid")

_REF = re.compile(r'HistoryExpand\("([^"]+)"\)')


def _reseed(session_id, tmp_path, **kwargs):
    result = fluid_transcript.reseed_delegated_cli_session(
        session_id, "/workspace", tmp_path, force=True, **kwargs,
    )
    assert result.status == "reseeded", result
    return _read_jsonl(Path(result.path))


def _native(**fields):
    """A native ladder decision as NativeLoop persists it."""
    return _evt("ContextCompaction", tier=3, reason="trigger", trimmed=[],
                render_version=2, **fields)


def _text(lines):
    return "".join(json.dumps(line["message"]["content"]) for line in lines)


def _plain(lines):
    """Every text the lines show the model, unescaped."""
    out = []
    for line in lines:
        content = line["message"]["content"]
        if isinstance(content, str):
            out.append(content)
            continue
        for block in content:
            for key in ("text", "content"):
                if isinstance(block.get(key), str):
                    out.append(block[key])
    return "\n".join(out)


def test_reseed_starts_from_a_native_compaction(mock_db, tmp_path):
    """GPT compacted the first turn into an epoch; Claude Code gets that
    epoch and the turn after it, not the first turn again."""
    session_id = _seed()
    _insert_events(session_id, [_native(node_id="n5", spans=[["n1", "n5", "epoch"]])])

    lines = _reseed(session_id, tmp_path)
    first = lines[0]
    assert first["type"] == "user" and first["isCompactSummary"] is True
    assert "n1..n5" in json.dumps(first["message"]["content"])
    text = _text(lines)
    # The epoch quotes the request it folds; the turn itself is gone.
    assert "file body" not in text and "first reply" not in text
    assert "first prompt" not in [line["message"]["content"] for line in lines]
    assert [line["message"]["content"] for line in lines[-2:]] == [
        "second prompt", [{"type": "text", "text": "second reply"}],
    ]


def test_native_trim_reaches_the_cli_and_keeps_its_pair(mock_db, tmp_path):
    session_id = _seed()
    _insert_events(session_id, [_evt(
        "ContextCompaction", tier=1, reason="trigger", node_id="n5",
        trimmed=[["n4", "t1"]], spans=[], render_version=2,
    )])
    lines = _reseed(session_id, tmp_path)
    text = _text(lines)
    assert 'trimmed' in text and 'HistoryExpand(\\"n4\\")' in text
    assert "first prompt" in text
    # The call and its (trimmed) result are both still there.
    assert '"tool_use_id": "t1"' in text and '"id": "t1"' in text


def test_native_compaction_off_the_path_or_older_than_a_summary(mock_db, tmp_path):
    # A rewound-away branch's native decision does not apply.
    session_id = _seed()
    _insert_events(session_id, [_native(node_id="n-gone", spans=[["n1", "n5", "epoch"]])])
    assert _reseed(session_id, tmp_path)[0]["message"]["content"] == "first prompt"

    # A newer Claude Code summary wins over an older native compaction...
    later = _seed("ses_later")
    _insert_events(later, [
        _native(node_id="n5", spans=[["n1", "n5", "epoch"]]),
        _cli_compaction(node_id="n5", summary="NEW summary"),
    ])
    assert _reseed(later, tmp_path)[0]["message"]["content"] == "NEW summary"

    # ...and a newer native compaction wins over an older summary.
    newest = _seed("ses_newest")
    _insert_events(newest, [
        _cli_compaction(node_id="n2", summary="OLD summary"),
        _native(node_id="n5", spans=[["n1", "n5", "epoch"]]),
    ])
    lines = _reseed(newest, tmp_path)
    assert "OLD summary" not in _text(lines) and "n1..n5" in _text(lines)


# ─ the size guard ─

def _node(i):
    return f"node_{i:04x}"


def _seed_long(session_id, turns=14, body=6_000):
    """``turns`` turns, each a prompt, a Read call and a large result
    carrying a unique marker."""
    nodes = []
    parent = None

    def add(message):
        nonlocal parent
        node_id = _node(len(nodes) + 1)
        nodes.append((node_id, parent, message))
        parent = node_id

    for t in range(turns):
        add(_user(f"prompt {t}"))
        add(fluid_message("assistant", [{
            "kind": "tooluseblock", "id": f"call{t}", "name": "Read",
            "input": {"file_path": f"/f{t}"},
        }]))
        add(fluid_message("user", [{
            "kind": "toolresultblock", "tool_use_id": f"call{t}",
            "content": [{"kind": "text", "text": f"MARKER-{t} " + "x" * body}],
            "is_error": False, "delivered_steers": [], "delivery_metadata_version": 0,
        }]))
        add(fluid_message("assistant", [{"kind": "text", "text": f"done {t}"}]))
    _insert(session_id, nodes)
    return [node_id for node_id, _, _ in nodes]


def _checkpoint_with_skills(ids, body=6_000):
    """A Claude mod checkpoint through turn 1 whose rows carry a skill body
    Claude Code added to each of turns 0 and 1: rows the stored path does
    not hold."""
    rows = []
    for t in range(2):
        rows += [
            {"role": "user", "text": f"prompt {t}", "toolUses": []},
            {"role": "user", "text": f"SKILL BODY {t} " + "s" * 400, "toolUses": []},
            {"role": "assistant", "text": "", "toolUses": [
                {"tool_use_id": f"call{t}", "tool": "Read", "input": {"file_path": f"/f{t}"}}]},
            {"role": "user", "text": "", "toolUses": [], "toolResults": [
                {"tool_use_id": f"call{t}", "text": f"MARKER-{t} " + "x" * body,
                 "isError": False}]},
            {"role": "assistant", "text": f"done {t}", "toolUses": []},
        ]
    return _evt("CompactionCheckpoint", through=ids[7], rows=rows)


def test_an_oversized_reseed_is_compacted_and_stays_recoverable(mock_db, tmp_path):
    session_id = "ses_oversized"
    ids = _seed_long(session_id)
    _insert_events(session_id, [_checkpoint_with_skills(ids)])
    window = 20_000
    threshold = fluid_transcript.reseed_compaction_threshold(window)
    whole = [fluid_transcript._node_to_entry(n, session_id)
             for n in fluid_transcript.load_fluid_path(session_id)]
    assert fluid_transcript._estimated_tokens(whole) > threshold

    lines = _reseed(session_id, tmp_path, context_window=window)

    assert fluid_transcript._estimated_tokens(lines) <= threshold
    assert lines[0]["isCompactSummary"] is True
    text = _text(lines)
    assert "prompt 13" in text  # the newest work stays verbatim
    events = [fluid_transcript._as_dict(row["event"])
              for row in fluid_transcript._event_rows(session_id)]
    archived = {entry["node_id"]: entry["text"]
                for event in events if event.get("_cls") == "ContextArchived"
                for entry in event["entries"]}
    # The older skill body left the context through the note, archived
    # first; the newest earlier one stays, as on a live compaction.
    assert any(body.startswith("SKILL BODY 0") for body in archived.values())
    assert "SKILL BODY 0 " + "s" * 400 not in text
    # Every ref names a stored node (or range) or an archived row.
    index = {node_id: i for i, node_id in enumerate(ids)}
    covered = set()
    for ref in _REF.findall(_plain(lines)):
        first, _, last = ref.partition("..")
        if first in archived:
            continue
        assert first in index and (last or first) in index, ref
        covered.update(range(index[first], index[last or first] + 1))
    # Each marker that left the transcript is behind a ref to its node.
    gone = [t for t in range(14) if f"MARKER-{t} " not in text]
    assert gone
    for t in gone:
        assert index[_node(4 * t + 3)] in covered or _node(4 * t + 3) in text, t
    # The event log is still numbered without gaps for the next engine.
    seqs = [row["seq"] for row in fluid_transcript._event_rows(session_id)]
    assert seqs == list(range(1, len(seqs) + 1))


def test_a_reseed_under_the_threshold_is_written_whole(mock_db, tmp_path):
    session_id = "ses_small"
    _seed_long(session_id, turns=2, body=100)
    lines = _reseed(session_id, tmp_path, context_window=200_000)
    assert lines[0]["message"]["content"] == "prompt 0"
    assert "MARKER-0" in _text(lines) and "MARKER-1" in _text(lines)
    assert not any(row["event"] for row in fluid_transcript._event_rows(session_id))


def test_threshold_tracks_the_cli_compaction_point():
    assert fluid_transcript.reseed_compaction_threshold(400_000) == 367_000
    assert fluid_transcript.reseed_compaction_threshold(200_000) == 167_000
    # A small window never compacts below half of it.
    assert fluid_transcript.reseed_compaction_threshold(20_000) == 10_000


def test_images_count_at_a_fixed_cost_not_their_base64_size():
    """A screenshot's base64 data is not text the model reads: counted as
    text, a few images would push a small transcript over the threshold."""
    shot = {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                        "data": "A" * 400_000}}
    entries = [
        {"message": {"role": "user", "content": [{"type": "text", "text": "look"}, shot]}},
        {"message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1",
                                                   "content": [shot]}]}},
    ]
    tokens = fluid_transcript._estimated_tokens(entries)
    assert 2 * fluid_transcript._RESEED_IMAGE_TOKENS <= tokens < 2 * fluid_transcript._RESEED_IMAGE_TOKENS + 200
