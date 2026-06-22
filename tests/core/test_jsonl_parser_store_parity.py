"""PR C.1 parity tests: store-backed parse == JSONL parse on identical entries.

The postgres_only read cutover loads transcript entries from Postgres
(``session_store_entries``) and runs them through the SAME ``_transform_entries``
core as the JSONL path. These tests feed the EXACT same raw entries through both
paths and assert byte-identical ``(messages, usage_data)``.

Hooks-JSONL merge is the one deliberate difference (the store does not mirror the
hooks sidecar — an accepted parity gap), so fixtures here contain no hook
messages and the comparison is exact.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from app.core import jsonl_parser as jp


def _store_loader_for(main_entries, subagent_entries_by_id):
    """Build a sync store_loader matching the PgSessionStore read contract.

    ``store_loader(session_id, subpath=None)``: returns main_entries when
    subpath is None, or the subagent entries for ``subagents/agent-<id>``.
    """

    def _loader(session_id, subpath=None):
        if subpath is None:
            return list(main_entries)
        prefix = "subagents/agent-"
        if subpath.startswith(prefix):
            agent_id = subpath[len(prefix) :]
            return list(subagent_entries_by_id.get(agent_id, []))
        return []

    return _loader


def _parse_via_file(session_id, main_entries, agent_files=None):
    """Parse main_entries through the JSONL file path (parse_session_history)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        projects_dir = Path(tmpdir) / "projects"
        project_dir = projects_dir / "-workspace"
        project_dir.mkdir(parents=True)
        jsonl_file = project_dir / f"{session_id}.jsonl"
        jsonl_file.write_text("\n".join(json.dumps(e) for e in main_entries) + "\n")
        # Subagent transcripts live in the nested <sid>/subagents/ layout.
        for agent_id, entries in (agent_files or {}).items():
            subagents_dir = project_dir / session_id / "subagents"
            subagents_dir.mkdir(parents=True, exist_ok=True)
            af = subagents_dir / f"agent-{agent_id}.jsonl"
            af.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
        with patch("app.core.jsonl_parser.settings") as mock_settings:
            mock_settings.get_claude_projects_dir = projects_dir
            jp.invalidate_parse_cache()
            return jp.parse_session_history(session_id, "/workspace")


def setup_function(_func):
    jp.invalidate_parse_cache()


def teardown_function(_func):
    jp.invalidate_parse_cache()


# ---------------------------------------------------------------------------
# Parity: plain conversation (text + tool use/result)
# ---------------------------------------------------------------------------


def test_parity_text_and_tool():
    session_id = "parity-sess-1"
    entries = [
        {
            "type": "user",
            "message": {"role": "user", "content": "Hi"},
            "uuid": "u1",
            "timestamp": "2024-01-01T00:00:00Z",
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Hello!"}],
                "usage": {"input_tokens": 10, "output_tokens": 3},
                "model": "claude-x",
            },
            "uuid": "a1",
            "timestamp": "2024-01-01T00:00:01Z",
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t-1",
                        "name": "Bash",
                        "input": {"command": "echo hi"},
                    }
                ],
            },
            "uuid": "a2",
            "timestamp": "2024-01-01T00:00:02Z",
        },
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t-1",
                        "content": "hi",
                        "is_error": False,
                    }
                ],
            },
            "uuid": "u2",
            "timestamp": "2024-01-01T00:00:03Z",
        },
    ]

    file_msgs, file_usage = _parse_via_file(session_id, entries)

    store_loader = _store_loader_for(entries, {})
    store_msgs, store_usage = jp.parse_session_history_from_store(
        session_id, "/workspace", store_loader, use_cache=False
    )

    assert store_msgs == file_msgs
    assert store_usage == file_usage
    # Sanity: the transform actually did something.
    assert any(m.get("type") == "tool_use" for m in file_msgs)


def test_parity_usage_accumulation():
    session_id = "parity-sess-usage"
    entries = [
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "one"}],
                "usage": {"input_tokens": 5, "output_tokens": 7},
                "model": "claude-y",
            },
            "uuid": "a1",
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "two"}],
                "usage": {"input_tokens": 11, "output_tokens": 13},
                "model": "claude-y",
            },
            "uuid": "a2",
        },
    ]

    file_msgs, file_usage = _parse_via_file(session_id, entries)
    store_loader = _store_loader_for(entries, {})
    store_msgs, store_usage = jp.parse_session_history_from_store(
        session_id, "/workspace", store_loader, use_cache=False
    )

    assert store_usage == file_usage
    assert store_msgs == file_msgs


# ---------------------------------------------------------------------------
# Parity: subagent (Task) with children loaded from a separate transcript
# ---------------------------------------------------------------------------


def test_parity_subagent_children():
    session_id = "parity-sess-sub"
    agent_id = "deadbeef01"
    main_entries = [
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "task-1",
                        "name": "Task",
                        "input": {
                            "description": "do work",
                            "prompt": "go",
                            "subagent_type": "general-purpose",
                        },
                    }
                ],
            },
            "uuid": "a1",
        },
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "task-1", "content": "done"}
                ],
            },
            "toolUseResult": {"agentId": agent_id},
            "uuid": "u1",
        },
    ]
    subagent_entries = [
        {
            "type": "user",
            "message": {"role": "user", "content": "child prompt"},
            "sessionId": session_id,
            "isSidechain": True,
            "agentId": agent_id,
            "uuid": "c1",
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "child reply"}],
            },
            "sessionId": session_id,
            "isSidechain": True,
            "agentId": agent_id,
            "uuid": "c2",
        },
    ]

    file_msgs, file_usage = _parse_via_file(
        session_id, main_entries, agent_files={agent_id: subagent_entries}
    )

    store_loader = _store_loader_for(main_entries, {agent_id: subagent_entries})
    store_msgs, store_usage = jp.parse_session_history_from_store(
        session_id, "/workspace", store_loader, use_cache=False
    )

    # The load-bearing assertion: store and file paths produce identical output
    # even with subagent children loaded from a separate transcript.
    assert store_msgs == file_msgs
    assert store_usage == file_usage
    # Sanity: the subagent message carried children from the separate transcript
    # (the agent's own opening prompt is filtered, so >=1 rendered child).
    sub = next((m for m in file_msgs if m.get("agentChildren")), None)
    assert sub is not None, "expected a subagent message with children"
    assert sub["agentId"] == agent_id
    assert len(sub["agentChildren"]) >= 1
    assert any(c.get("content") == "child reply" for c in sub["agentChildren"])


# ---------------------------------------------------------------------------
# Cache namespace isolation
# ---------------------------------------------------------------------------


def test_store_cache_is_separate_namespace():
    """A store parse populates _store_parse_cache, NOT _parse_cache."""
    session_id = "parity-cache-sess"
    entries = [
        {"type": "user", "message": {"role": "user", "content": "Hi"}, "uuid": "u1"},
    ]
    jp.invalidate_parse_cache()
    store_loader = _store_loader_for(entries, {})

    jp.parse_session_history_from_store(
        session_id, "/workspace", store_loader, use_cache=True
    )

    assert session_id in jp._store_parse_cache
    assert session_id not in jp._parse_cache

    # invalidate clears the store cache too.
    jp.invalidate_parse_cache(session_id)
    assert session_id not in jp._store_parse_cache


def test_transform_entries_identical_for_iterable_and_list():
    """_transform_entries is source-agnostic: a generator and a list of the same
    entries produce identical output (the property the file/store split relies on).
    """
    entries = [
        {"type": "user", "message": {"role": "user", "content": "Hi"}, "uuid": "u1"},
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Yo"}],
                "usage": {"input_tokens": 1, "output_tokens": 2},
            },
            "uuid": "a1",
        },
    ]

    as_list = jp._transform_entries(list(entries), "s", "/workspace")
    as_gen = jp._transform_entries((e for e in entries), "s", "/workspace")

    assert as_list == as_gen
