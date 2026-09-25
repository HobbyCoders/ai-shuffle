"""
Incremental turn persistence — Phase 2.5 proof battery (prod-readiness plan).

The plan's premise ("messages persist only at turn end") is OUTDATED for
``session_store_mode="postgres_only"`` — the default since Phase 2.1 and what
both prod installs run. Under postgres_only the durability mechanism already
in place is exactly what 2.5 asks for:

1. FLUSH POINTS — the SDK's transcript mirror ships every completed transcript
   block over stdout as a ``transcript_mirror`` frame; the app wires the
   ``PgSessionStore`` with ``session_store_flush="eager"``
   (query_options.py), which zeroes the batcher thresholds so EVERY frame
   schedules a background ``store.append`` → ``INSERT INTO
   session_store_entries``. Block-boundary granularity, off the hot path.
2. ID STRATEGY — store entries carry the SDK transcript uuids; the reload read
   path (``parse_session_history_via_store``) runs them through the same
   transform as JSONL, generating the same frontend message ids as any reload.
   Zero frontend awareness needed.
3. EARLY LINK — ``sessions.sdk_session_id`` is written at the SDK ``init``
   SystemMessage (query_engine.py), so the store rows are reachable by the
   read path even if the turn never completes.
4. IDEMPOTENCY — ``_persist_turn_results`` step 3 (the legacy
   ``session_messages`` batch) is GATED OFF under postgres_only
   (query_persistence.py: ``settings.session_store_mode != "postgres_only"``).
   The SDK mirror is the single durable message writer, so turn-end
   persistence structurally cannot duplicate mid-turn flushes.

So a mid-turn CLI death loses at most the in-flight (never-mirrored) block.
These tests prove that end to end THROUGH THE REAL SEAMS: the real
``stream_to_websocket`` streaming loop, the real ``_persist_turn_results``,
the real DB layer (pg_shim executes the app's actual SQL on SQLite), the real
``parse_session_history_via_store`` read path, and the real SDK
``TranscriptMirrorBatcher``.

Simulation seams (each is a documented dialect/subprocess boundary, not a mock
of app logic under test):
- Mirror flushes are simulated with the same ``INSERT INTO
  session_store_entries (project_key, session_id, subpath, entry)`` the real
  ``PgSessionStore._append_sync`` issues. The real ``_append_sync`` body can't
  run on the shim (psycopg2 ``Json`` params, ``FOR UPDATE``, ``EXTRACT(EPOCH
  FROM NOW())``); real-PG write↔read parity is covered by the ALLOW_REAL_PG
  conformance suite in tests/core/test_pg_session_store.py.
- ``_load_entries_by_session_id_sync`` returns JSON strings under the shim
  (psycopg2 auto-decodes JSONB; sqlite doesn't) — the ``store_read`` fixture
  wraps the REAL function with a json.loads decode.
- The SDK client is faked (no subprocess in tests); the batcher tests exercise
  the real batcher against the frames the subprocess would emit.
"""

import asyncio
import json
import uuid
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.provider.events import (
    AssistantMessage,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

import app.core.query_engine as qe
import app.core.query_persistence as qp
from app.core import jsonl_parser as jp
from app.core.session_pool import SessionState
from app.db import database


# ─── mode + cache hygiene ───────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _fresh_parse_caches():
    jp.invalidate_parse_cache()
    yield
    jp.invalidate_parse_cache()


# ─── real read path (with the psycopg2-JSONB decode seam) ───────────────────


@pytest.fixture
def store_read(monkeypatch):
    """The REAL postgres_only reload read path, callable as read(sdk_sid).

    parse_session_history_via_store imports _load_entries_by_session_id_sync
    from app.core.pg_session_store at call time, so patching the module
    attribute with a decoding wrapper around the REAL function keeps the whole
    read path genuine while bridging the sqlite-returns-TEXT-for-JSONB gap.
    """
    import app.core.pg_session_store as pss

    real_load = pss._load_entries_by_session_id_sync

    def _decoding_load(session_id, subpath=None):
        return [
            json.loads(e) if isinstance(e, str) else e
            for e in real_load(session_id, subpath)
        ]

    monkeypatch.setattr(pss, "_load_entries_by_session_id_sync", _decoding_load)

    from app.api.sessions_helpers import parse_session_history_via_store

    def _read(sdk_sid, working_dir="/workspace", use_cache=True):
        messages, usage = parse_session_history_via_store(
            sdk_sid, working_dir, use_cache=use_cache
        )
        return messages

    return _read


# ─── durable-write simulation (matches PgSessionStore._append_sync's INSERT) ─


def _mirror_flush(test_db, sdk_sid, entry, subpath=None):
    """One eager mirror flush: the durability-bearing write _append_sync does."""
    test_db.execute(
        "INSERT INTO session_store_entries (project_key, session_id, subpath, entry) "
        "VALUES (?, ?, ?, ?)",
        ("-workspace", sdk_sid, subpath, json.dumps(entry)),
    )
    test_db.commit()


# Transcript entry shapes — exactly what the CLI mirrors (see
# tests/core/test_jsonl_parser_store_parity.py).


def _user_entry(text, entry_uuid):
    return {
        "type": "user",
        "message": {"role": "user", "content": text},
        "uuid": entry_uuid,
        "timestamp": "2026-07-06T00:00:00Z",
    }


def _asst_text_entry(text, entry_uuid, model="claude-test"):
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": 10, "output_tokens": 5},
            "model": model,
        },
        "uuid": entry_uuid,
        "timestamp": "2026-07-06T00:00:01Z",
    }


def _asst_tool_entry(tool_id, name, tool_input, entry_uuid):
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}
            ],
        },
        "uuid": entry_uuid,
        "timestamp": "2026-07-06T00:00:02Z",
    }


def _tool_result_entry(tool_id, output, entry_uuid):
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "content": output,
                    "is_error": False,
                }
            ],
        },
        "uuid": entry_uuid,
        "timestamp": "2026-07-06T00:00:03Z",
    }


# ─── engine harness (adapted from test_busy_write_guard_engine.py, but with ──
# ─── run_sync + database + _persist_turn_results REAL) ───────────────────────


def _make_state():
    adapter = MagicMock()
    adapter.is_alive = MagicMock(return_value=True)
    adapter.sdk_client = MagicMock()
    adapter.send_query = AsyncMock()
    return SessionState(adapter=adapter, is_connected=True)


class _StubPool:
    def __init__(self, state=None):
        self.state = state

    def get(self, session_id):
        return self.state

    def peek(self, session_id):
        return self.state

    def put(self, session_id, state):
        self.state = state

    def admission_check(self, lane="delegated"):
        return None

    async def evict_if_needed(self, incoming_lane="delegated"):
        return None

    async def remove(self, session_id):
        self.state = None

    async def remove_dead(self, session_id):
        self.state = None

    async def remove_broken(self, session_id):
        self.state = None


def _fake_sync_engine():
    sync = MagicMock()
    sync.broadcast_message_added = AsyncMock()
    sync.broadcast_stream_start = AsyncMock()
    sync.broadcast_stream_chunk = AsyncMock()
    sync.broadcast_stream_end = AsyncMock()
    return sync


def _engine_patches(stack: ExitStack, pool: _StubPool, cwd: str):
    """Patch the subprocess/profile boundaries ONLY.

    Deliberately REAL (they are the subject under test): run_sync, the whole
    app.db layer (routed to the shim by conftest), _persist_turn_results, and
    the early sdk_session_id link inside the streaming loop.
    """
    stack.enter_context(patch.object(
        qe, "get_profile", return_value={"id": "default", "config": {}}
    ))
    options = MagicMock()
    options.include_partial_messages = False
    options.cwd = cwd
    stack.enter_context(patch("app.core.sdk_options.build_options_from_profile", return_value=(options, {})
    ))
    stack.enter_context(patch.object(qe, "get_session_pool", return_value=pool))
    stack.enter_context(patch.object(qe, "is_subprocess_alive", return_value=True))
    stack.enter_context(patch.object(qe, "requires_reconnect", return_value=False))
    stack.enter_context(patch.object(qe, "mark_subprocess_oom_killable", new=MagicMock()))
    stack.enter_context(patch.object(qe, "resolve_note_references", side_effect=lambda p: p))
    stack.enter_context(patch.object(qe, "push_memory", new=AsyncMock()))
    stack.enter_context(patch.object(qe, "sync_engine", new=_fake_sync_engine()))
    return options


async def _collect(agen):
    events = []
    async for ev in agen:
        events.append(ev)
    return events


def _new_session(profile_id="default"):
    session_id = f"sess-{uuid.uuid4()}"
    database.create_session(session_id=session_id, profile_id=profile_id)
    return session_id


def _stream_response(test_db, sdk_sid, script):
    """Build a receive_response factory that mirrors entries as it yields.

    ``script`` is a list of ("flush", entry) / ("yield", message) /
    ("raise", exc) steps — the flush-at-block-boundary interleaving the real
    eager mirror produces (the CLI mirrors a block when it completes, i.e. by
    the time the SDK message for it reaches the app).
    """

    async def _gen():
        for kind, payload in script:
            if kind == "flush":
                _mirror_flush(test_db, sdk_sid, payload)
            elif kind == "yield":
                yield payload
            elif kind == "raise":
                raise payload

    return MagicMock(side_effect=lambda: _gen())


def _content_signature(messages):
    """Order-preserving, load-bearing view of the read path's parsed messages.

    Real output shapes (probed through parse_session_history_from_store):
    user → role="user", type=None, id=<uuid>; assistant text → type="text",
    id="text-<uuid>-<n>"; tool_use → type="tool_use" with the matching
    tool_result FOLDED IN as toolResult (no separate message).
    """
    sig = []
    for m in messages:
        if m.get("type") == "tool_use":
            sig.append(("tool_use", m.get("toolName"), m.get("toolResult")))
        elif m.get("type") == "text":
            sig.append(("assistant", m.get("content")))
        else:
            sig.append((m.get("role"), m.get("content")))
    return sig


def _assistant_rows_in_legacy_table(session_id):
    rows = database.get_session_messages(session_id)
    return [r for r in rows if r["role"] in ("assistant", "tool_use", "tool_result",
                                             "thinking", "subagent", "system")]


# ═════════════════════════════════════════════════════════════════════════════
# 1. Mid-turn death: flushed blocks durable via the REAL read path
# ═════════════════════════════════════════════════════════════════════════════


async def test_mid_turn_death_preserves_flushed_blocks_via_real_read_path(test_db, store_read, tmp_path):
    """The incident scenario: CLI dies after N completed blocks. The N mirrored
    blocks must be readable through the real postgres_only reload path, the
    in-flight (never-mirrored) block absent, sdk_session_id already linked,
    and no crash artifacts / duplicate rows anywhere."""
    session_id = _new_session()
    sdk_sid = str(uuid.uuid4())
    state = _make_state()
    pool = _StubPool(state)

    script = [
        ("yield", SystemMessage(subtype="init", data={"session_id": sdk_sid})),
        # The CLI mirrors the user line as soon as the turn starts.
        ("flush", _user_entry("run the thing", "u-1")),
        # Block 1: assistant text — completed, mirrored.
        ("flush", _asst_text_entry("Block one done.", "a-1")),
        ("yield", AssistantMessage(content=[TextBlock(text="Block one done.")],
                                   model="claude-test", uuid="a-1")),
        # Block 2: tool_use — completed, mirrored.
        ("flush", _asst_tool_entry("t-1", "Bash", {"command": "echo hi"}, "a-2")),
        ("yield", AssistantMessage(
            content=[ToolUseBlock(id="t-1", name="Bash", input={"command": "echo hi"})],
            model="claude-test", uuid="a-2")),
        # Block 3: tool_result — completed, mirrored.
        ("flush", _tool_result_entry("t-1", "hi", "u-2")),
        ("yield", UserMessage(
            content=[ToolResultBlock(tool_use_id="t-1", content="hi")], uuid="u-2")),
        # Block 4 is IN FLIGHT — the CLI dies mid-write. Never mirrored,
        # never yielded complete.
        ("raise", RuntimeError("Fatal error in message reader")),
    ]
    state.adapter.receive_events = _stream_response(test_db, sdk_sid, script)

    with ExitStack() as stack:
        _engine_patches(stack, pool, str(tmp_path))
        events = await _collect(
            qe.stream_to_websocket("run the thing", session_id, "default")
        )

    # The crash surfaced as an error frame, with no misleading `done`.
    assert any(e.get("type") == "error" for e in events), events
    assert not any(e.get("type") == "done" for e in events), events

    # Early link: the sessions row points at the SDK transcript even though the
    # turn never completed — the store rows are REACHABLE.
    sess = database.get_session(session_id)
    assert sess["sdk_session_id"] == sdk_sid

    # The REAL read path returns exactly the flushed blocks, in stream order.
    messages = store_read(sdk_sid)
    assert _content_signature(messages) == [
        ("user", "run the thing"),
        ("assistant", "Block one done."),
        ("tool_use", "Bash", "hi"),
    ]

    # No duplicates and no crash artifacts in the legacy table: the
    # postgres_only gate kept _persist_turn_results (which DID run — the crash
    # path falls through to it) from double-writing the flushed blocks.
    assert _assistant_rows_in_legacy_table(session_id) == []


async def test_mid_turn_death_before_any_flush_loses_only_that_turn(test_db, store_read, tmp_path):
    """Crash before the first mirrored block: the read path returns empty (not
    an error), the session row survives, and nothing is fabricated."""
    session_id = _new_session()
    sdk_sid = str(uuid.uuid4())
    state = _make_state()
    pool = _StubPool(state)

    script = [
        ("yield", SystemMessage(subtype="init", data={"session_id": sdk_sid})),
        ("raise", RuntimeError("Fatal error in message reader")),
    ]
    state.adapter.receive_events = _stream_response(test_db, sdk_sid, script)

    with ExitStack() as stack:
        _engine_patches(stack, pool, str(tmp_path))
        events = await _collect(qe.stream_to_websocket("hi", session_id, "default"))

    assert any(e.get("type") == "error" for e in events), events
    assert database.get_session(session_id)["sdk_session_id"] == sdk_sid
    assert store_read(sdk_sid) == []
    assert _assistant_rows_in_legacy_table(session_id) == []


# ═════════════════════════════════════════════════════════════════════════════
# 2. Normal turn end: identical durable rows, zero dupes, idempotent persist
# ═════════════════════════════════════════════════════════════════════════════


async def test_normal_turn_end_no_dupes_and_aggregates(test_db, store_read, tmp_path):
    """A full turn through the real engine loop + real _persist_turn_results:
    the durable transcript holds exactly one copy of every block (the mirror's),
    turn-end persistence adds only the sessions-row aggregates, and a reload
    (cached or fresh) returns the identical transcript."""
    session_id = _new_session()
    sdk_sid = str(uuid.uuid4())
    state = _make_state()
    pool = _StubPool(state)

    script = [
        ("yield", SystemMessage(subtype="init", data={"session_id": sdk_sid})),
        ("flush", _user_entry("do it", "u-1")),
        ("flush", _asst_text_entry("Working on it.", "a-1")),
        ("yield", AssistantMessage(content=[TextBlock(text="Working on it.")],
                                   model="claude-test", uuid="a-1")),
        ("flush", _asst_tool_entry("t-1", "Bash", {"command": "make"}, "a-2")),
        ("yield", AssistantMessage(
            content=[ToolUseBlock(id="t-1", name="Bash", input={"command": "make"})],
            model="claude-test", uuid="a-2")),
        ("flush", _tool_result_entry("t-1", "ok", "u-2")),
        ("yield", UserMessage(
            content=[ToolResultBlock(tool_use_id="t-1", content="ok")], uuid="u-2")),
        ("flush", _asst_text_entry("All done.", "a-3")),
        ("yield", AssistantMessage(content=[TextBlock(text="All done.")],
                                   model="claude-test", uuid="a-3")),
        ("yield", ResultMessage(
            subtype="success", duration_ms=1200, duration_api_ms=1000,
            is_error=False, num_turns=1, session_id=sdk_sid,
            total_cost_usd=0.05,
            usage={"input_tokens": 10, "output_tokens": 5},
        )),
    ]
    state.adapter.receive_events = _stream_response(test_db, sdk_sid, script)

    with ExitStack() as stack:
        _engine_patches(stack, pool, str(tmp_path))
        events = await _collect(qe.stream_to_websocket("do it", session_id, "default"))

    assert any(e.get("type") == "done" for e in events), events

    expected = [
        ("user", "do it"),
        ("assistant", "Working on it."),
        ("tool_use", "Bash", "ok"),
        ("assistant", "All done."),
    ]
    # Fresh parse and cached parse agree, and each block appears exactly once —
    # turn-end persistence did NOT re-emit what the mirror already flushed.
    fresh = store_read(sdk_sid, use_cache=False)
    assert _content_signature(fresh) == expected
    cached = store_read(sdk_sid, use_cache=True)
    assert _content_signature(cached) == expected

    # The one thing turn-end persistence DOES write in postgres_only: the
    # sessions-row aggregates (idempotent UPDATE, not message rows).
    sess = database.get_session(session_id)
    assert sess["sdk_session_id"] == sdk_sid
    assert float(sess["total_cost_usd"]) == pytest.approx(0.05)
    assert sess["turn_count"] == 1

    # Zero message rows in the legacy table — the no-dupes gate.
    assert _assistant_rows_in_legacy_table(session_id) == []


async def test_interrupt_mid_turn_no_dupes(test_db, store_read, tmp_path):
    """Interrupt (CancelledError from the stream) after a flushed block: the
    interrupted persist path must not duplicate the mirrored block either."""
    session_id = _new_session()
    sdk_sid = str(uuid.uuid4())
    state = _make_state()
    pool = _StubPool(state)

    script = [
        ("yield", SystemMessage(subtype="init", data={"session_id": sdk_sid})),
        ("flush", _user_entry("long task", "u-1")),
        ("flush", _asst_text_entry("Starting.", "a-1")),
        ("yield", AssistantMessage(content=[TextBlock(text="Starting.")],
                                   model="claude-test", uuid="a-1")),
        ("raise", asyncio.CancelledError()),
    ]
    state.adapter.receive_events = _stream_response(test_db, sdk_sid, script)

    with ExitStack() as stack:
        _engine_patches(stack, pool, str(tmp_path))
        events = await _collect(qe.stream_to_websocket("long task", session_id, "default"))

    assert any(e.get("type") == "interrupted" for e in events), events
    assert _content_signature(store_read(sdk_sid)) == [
        ("user", "long task"),
        ("assistant", "Starting."),
    ]
    assert _assistant_rows_in_legacy_table(session_id) == []


async def test_persist_turn_results_gate_is_the_no_dupe_mechanism(test_db, monkeypatch):
    """Pin the MECHANISM: _persist_turn_results never writes the legacy message
    batch (the store mirror is the single writer) and is idempotent across a
    crash-retry double call."""
    session_id = _new_session()
    sdk_sid = str(uuid.uuid4())

    async def _persist():
        await qp._persist_turn_results(
            session_id=session_id,
            sdk_session_id=sdk_sid,
            metadata={"model": "claude-test", "total_cost_usd": 0.01, "num_turns": 1},
            profile_id="default",
        )

    # postgres_only: zero rows, twice (a crash-retry re-persist cannot dupe).
    await _persist()
    assert _assistant_rows_in_legacy_table(session_id) == []
    await _persist()
    assert _assistant_rows_in_legacy_table(session_id) == []
    # ...while the store's row count is untouched by turn-end persistence.
    cur = test_db.execute(
        "SELECT COUNT(*) FROM session_store_entries WHERE session_id = ?", (sdk_sid,)
    )
    assert cur.fetchone()[0] == 0
    # The sessions-row aggregate update ran both times (idempotent by design:
    # increments reflect exactly what each ResultMessage reported).
    assert database.get_session(session_id)["sdk_session_id"] == sdk_sid


# ═════════════════════════════════════════════════════════════════════════════
# 3. Fluid's real delegated producer persists complete frames before turn end
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("fail_store", [False, True])
async def test_delegated_frame_persistence_without_sdk(mock_db, monkeypatch, fail_store):
    from types import SimpleNamespace
    from fluid.engine.delegated import DelegatedLoop
    from fluid.engine.delegated_proto import StreamJsonMapper
    from fluid.messages.content import Message, Text
    from fluid.store.base import SessionNode
    from app.core.provider.fluid_pg_store import PgFluidStore

    store = PgFluidStore()
    loop = DelegatedLoop(store=store, model="fixture")
    session = await loop._ensure_session()
    sid = session.session_id
    user = SessionNode(node_id="u", parent_id=None,
                       message=Message(role="user", content=[Text(text="hello")]))
    await store.append(sid, user)
    stdout = asyncio.StreamReader()
    # Actual CLI frame mapping + actual app store append, not a fixture that
    # pre-seeds the rows the history reader consumes. EOF simulates CLI death.
    stdout.feed_data((json.dumps({
        "type": "assistant", "uuid": "a", "message": {
            "id": "a", "role": "assistant", "model": "fixture",
            "content": [{"type": "text", "text": "durable before result"}],
        },
    }) + "\n").encode())
    stdout.feed_eof()
    loop._proc = SimpleNamespace(stdout=stdout)
    if fail_store:
        async def fail(*args):
            raise RuntimeError("store down")
        monkeypatch.setattr(store, "append", fail)
        # Fluid fails closed; it does not silently lose a completed frame.
        with pytest.raises(RuntimeError, match="store down"):
            await loop._read_until_result(sid, "turn", StreamJsonMapper(session_id=sid, turn_id="turn", model="fixture", user_node_id="u"), user)
    else:
        error = await loop._read_until_result(sid, "turn", StreamJsonMapper(session_id=sid, turn_id="turn", model="fixture", user_node_id="u"), user)
        assert error is not None  # no result frame was ever received
        path = await PgFluidStore().load_path(sid)
        assert [node.message.text() for node in path] == ["hello", "durable before result"]
        assert len(path) == 2


# ═════════════════════════════════════════════════════════════════════════════
# 4. Discard flows still work against the incrementally-written store
# ═════════════════════════════════════════════════════════════════════════════


def test_first_turn_discard_wipes_incremental_writes(test_db, store_read):
    """Stop-before-output on a brand-new session: discard_orphan_first_turn
    must erase the eagerly-mirrored rows AND the early-linked sdk_session_id,
    so the real read path shows a clean session."""
    session_id = _new_session()
    sdk_sid = str(uuid.uuid4())
    # The turn got as far as: early link + the mirrored user line.
    database.update_session(session_id, sdk_session_id=sdk_sid)
    _mirror_flush(test_db, sdk_sid, _user_entry("never mind", "u-1"))
    database.add_session_message(session_id, "user", "never mind")

    from app.db.sessions import discard_orphan_first_turn

    discarded = discard_orphan_first_turn(session_id)
    assert discarded == "never mind"

    assert database.get_session(session_id) is not None  # session row kept
    assert database.get_session(session_id)["sdk_session_id"] is None
    assert store_read(sdk_sid) == []  # store wiped through the real read path


def test_followup_turn_discard_keeps_prior_history(test_db, store_read):
    """Stop-before-output on a FOLLOW-UP turn: delete_orphan_user_turn removes
    the trailing legacy user row but the store transcript (prior turns'
    incremental writes) must be untouched.

    Known pre-existing gap (documented, not fixed here): the mirrored trailing
    user line of the discarded turn stays in the store — follow-up discard is
    legacy-table-only by design (wiping the shared transcript would destroy
    real history)."""
    session_id = _new_session()
    sdk_sid = str(uuid.uuid4())
    database.update_session(session_id, sdk_session_id=sdk_sid)
    # Prior completed turn, incrementally mirrored.
    _mirror_flush(test_db, sdk_sid, _user_entry("first prompt", "u-1"))
    _mirror_flush(test_db, sdk_sid, _asst_text_entry("first answer", "a-1"))
    # The stopped follow-up turn's eager legacy row.
    database.add_session_message(session_id, "user", "second prompt")

    from app.db.sessions import delete_orphan_user_turn

    discarded = delete_orphan_user_turn(session_id)
    assert discarded == "second prompt"

    messages = store_read(sdk_sid)
    assert _content_signature(messages) == [
        ("user", "first prompt"),
        ("assistant", "first answer"),
    ]
    assert database.get_session(session_id)["sdk_session_id"] == sdk_sid
    # The eager legacy user row is gone.
    assert all(r["content"] != "second prompt"
               for r in database.get_session_messages(session_id))
