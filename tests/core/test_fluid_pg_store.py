"""PgFluidStore — fluid's SessionStore protocol over the app's Postgres.

Runs against the pg_shim (`mock_db`), exactly like the PgSessionStore tests,
so the DDL under test is the real schema_postgres v70 tables. The protocol
semantics asserted here mirror fluid's own JsonlStore behaviors: append moves
the head, load_path resolves leaf→root root-first, set_head forks without
copying, and the event log replays idempotently by (session_id, seq).
"""

from __future__ import annotations

import json
import logging

import pytest

from app.core.provider.fluid_pg_store import PgFluidStore

from fluid.events.model import TextDelta
from fluid.messages.content import Message, Text
from fluid.store.base import SessionNode, SessionSearchHit


def _node(node_id: str, parent_id, text: str) -> SessionNode:
    return SessionNode(
        node_id=node_id,
        parent_id=parent_id,
        message=Message(role="user", content=[Text(text=text)]),
    )


@pytest.mark.asyncio
async def test_round_trip_and_head_advance(mock_db):
    store = PgFluidStore()
    meta = await store.create_session("ses_pg1")
    assert meta.session_id == "ses_pg1" and meta.head_node_id is None

    await store.append("ses_pg1", _node("n1", None, "hello"))
    await store.append("ses_pg1", _node("n2", "n1", "world"))

    path = await store.load_path("ses_pg1")
    assert [n.node_id for n in path] == ["n1", "n2"]
    assert path[0].message.text() == "hello"
    assert (await store.get_session("ses_pg1")).head_node_id == "n2"


@pytest.mark.asyncio
async def test_persistence_across_store_instances(mock_db):
    store = PgFluidStore()
    await store.create_session("ses_pg2")
    await store.append("ses_pg2", _node("a", None, "root"))
    await store.append("ses_pg2", _node("b", "a", "leaf"))
    await store.append_event("ses_pg2", TextDelta(session_id="ses_pg2", index=0,
                                                  text="x", seq=1))

    # Fresh instance = cold cache = must load everything back from Postgres.
    reloaded = PgFluidStore()
    meta = await reloaded.get_session("ses_pg2")
    assert meta is not None and meta.head_node_id == "b"
    path = await reloaded.load_path("ses_pg2")
    assert [n.node_id for n in path] == ["a", "b"]
    assert path[1].message.text() == "leaf"
    events = await reloaded.events_since("ses_pg2", 0)
    assert len(events) == 1 and events[0].text == "x" and events[0].seq == 1


@pytest.mark.asyncio
async def test_fork_via_set_head(mock_db):
    store = PgFluidStore()
    await store.create_session("ses_pg3")
    await store.append("ses_pg3", _node("r", None, "root"))
    await store.append("ses_pg3", _node("c1", "r", "branch-1"))

    # Rewind to root, then grow a second branch — nothing is copied.
    await store.set_head("ses_pg3", "r")
    await store.append("ses_pg3", _node("c2", "r", "branch-2"))

    assert [n.node_id for n in await store.load_path("ses_pg3")] == ["r", "c2"]
    # The abandoned branch is still addressable by explicit leaf.
    assert [n.node_id for n in await store.load_path("ses_pg3", "c1")] == ["r", "c1"]

    # Fork survives a cold reload.
    reloaded = PgFluidStore()
    assert (await reloaded.get_session("ses_pg3")).head_node_id == "c2"


@pytest.mark.asyncio
async def test_event_append_is_idempotent_per_seq(mock_db):
    store = PgFluidStore()
    await store.create_session("ses_pg4")
    ev = TextDelta(session_id="ses_pg4", index=0, text="dup", seq=7)
    await store.append_event("ses_pg4", ev)

    # A replayed append of the same seq must not duplicate the durable row.
    replayer = PgFluidStore()
    await replayer.append_event("ses_pg4", ev)

    fresh = PgFluidStore()
    events = await fresh.events_since("ses_pg4", 0)
    assert len(events) == 1

    # Cursor semantics: strictly-greater-than.
    assert await fresh.events_since("ses_pg4", 7) == []


@pytest.mark.asyncio
async def test_seq_collision_keeps_first_row_and_warns(mock_db, caplog):
    """The conflict clause exists for legitimate replays, but it also hid real
    loss (an engine whose bus restarted numbering at 1 for a resumed session).
    A dropped insert must now be visible in the logs, and must not raise."""
    store = PgFluidStore()
    await store.create_session("ses_pg7")
    await store.append_event(
        "ses_pg7", TextDelta(session_id="ses_pg7", index=0, text="first", seq=3)
    )

    with caplog.at_level(logging.WARNING, logger="app.core.provider.fluid_pg_store"):
        await PgFluidStore().append_event(
            "ses_pg7", TextDelta(session_id="ses_pg7", index=0, text="second", seq=3)
        )

    events = await PgFluidStore().events_since("ses_pg7", 0)
    assert len(events) == 1 and events[0].text == "first"
    assert any(
        "DROPPED by seq collision" in r.getMessage()
        and "ses_pg7" in r.getMessage()
        and "seq=3" in r.getMessage()
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_create_session_resumes_existing(mock_db):
    store = PgFluidStore()
    await store.create_session("ses_pg5")
    await store.append("ses_pg5", _node("n", None, "hi"))

    # create_session on an existing id returns the stored meta (resume), it
    # must not reset the head.
    resumed = await PgFluidStore().create_session("ses_pg5")
    assert resumed.head_node_id == "n"


@pytest.mark.asyncio
async def test_missing_session_is_none(mock_db):
    assert await PgFluidStore().get_session("ses_missing") is None


@pytest.mark.asyncio
async def test_cli_session_id_round_trip(mock_db):
    """The delegated CLI's --resume handle survives a cold reload (v70)."""
    store = PgFluidStore()
    meta = await store.create_session("ses_pg6")
    assert meta.cli_session_id is None  # nothing known until the CLI reports it

    await store.set_cli_session_id("ses_pg6", "cli-uuid-abc")
    # Live cache updates immediately...
    assert (await store.get_session("ses_pg6")).cli_session_id == "cli-uuid-abc"
    # ...and a fresh instance reads it back from Postgres.
    assert (await PgFluidStore().get_session("ses_pg6")).cli_session_id == "cli-uuid-abc"

    # A later init frame overwrites the stored handle.
    await PgFluidStore().set_cli_session_id("ses_pg6", "cli-uuid-def")
    assert (await PgFluidStore().get_session("ses_pg6")).cli_session_id == "cli-uuid-def"

    # Other meta fields are untouched by the update.
    await store.append("ses_pg6", _node("n1", None, "hi"))
    reloaded = await PgFluidStore().get_session("ses_pg6")
    assert reloaded.head_node_id == "n1"
    assert reloaded.cli_session_id == "cli-uuid-def"


@pytest.mark.asyncio
async def test_project_root_round_trip(mock_db, tmp_path):
    """create_session(cwd=…) records the session's project identity (v83) —
    the enclosing git repo for a nested cwd — and a cold reload reads it back."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    nested = repo / "src" / "deep"
    nested.mkdir(parents=True)

    store = PgFluidStore()
    meta = await store.create_session("ses_root", cwd=str(nested))
    assert meta.project_root == str(repo.resolve())
    assert (await PgFluidStore().get_session("ses_root")).project_root == str(repo.resolve())

    # No cwd = no project — stored as NULL, the legacy-session shape recall
    # treats as "unknown", never as "another project".
    assert (await store.create_session("ses_rootless")).project_root is None
    assert (await PgFluidStore().get_session("ses_rootless")).project_root is None


@pytest.mark.asyncio
async def test_resume_keeps_the_stored_project_root(mock_db, tmp_path):
    """Resuming an existing id returns the stored meta — a different cwd on the
    resume call must not rewrite the session's recorded project."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    other = tmp_path / "elsewhere"
    other.mkdir()

    await PgFluidStore().create_session("ses_root2", cwd=str(repo))
    resumed = await PgFluidStore().create_session("ses_root2", cwd=str(other))
    assert resumed.project_root == str(repo.resolve())


@pytest.mark.asyncio
async def test_illegal_code_points_are_scrubbed_at_the_jsonb_boundary(mock_db):
    """A model that puts NUL (or a lone surrogate) in its output must not be
    able to kill the turn.

    Postgres json/jsonb cannot hold U+0000 or an unpaired UTF-16 surrogate, and
    the event append raises straight back into the engine, so an unscrubbed
    write took the whole run down with it. Both writers sanitize to U+FFFD.
    """
    dirty = "a\x00b\ud800c"
    clean = "a�b�c"

    store = PgFluidStore()
    await store.create_session("ses_nul")
    await store.append("ses_nul", _node("n1", None, dirty))
    await store.append_event(
        "ses_nul", TextDelta(session_id="ses_nul", index=0, text=dirty, seq=1)
    )

    node_json = mock_db.execute(
        "SELECT message FROM fluid_session_nodes WHERE session_id = ?",
        ("ses_nul",),
    ).fetchone()[0]
    event_json = mock_db.execute(
        "SELECT event FROM fluid_session_events WHERE session_id = ?",
        ("ses_nul",),
    ).fetchone()[0]

    for stored in (node_json, event_json):
        assert "\\u0000" not in stored
        assert "\\ud800" not in stored
        assert "\\ufffd" in stored

    # ...and the scrubbed content is what reads back out of the store.
    reloaded = PgFluidStore()
    path = await reloaded.load_path("ses_nul")
    assert path[0].message.text() == clean
    events = await reloaded.events_since("ses_nul", 0)
    assert events[0].text == clean


@pytest.mark.asyncio
async def test_legal_content_survives_the_scrub_untouched(mock_db):
    store = PgFluidStore()
    await store.create_session("ses_ok")
    await store.append("ses_ok", _node("n1", None, "plain unicode ✓"))
    reloaded = PgFluidStore()
    assert (await reloaded.load_path("ses_ok"))[0].message.text() == (
        "plain unicode ✓"
    )


# ─── Idle / streaming performance regressions ────────────────────────────────

@pytest.mark.asyncio
async def test_native_producer_batches_deltas_before_durable_barrier(mock_db):
    from fluid.engine.native import NativeLoop
    from fluid.events.model import TurnComplete
    store = PgFluidStore()
    store.STREAM_BATCH_SECONDS = 60  # Deterministic count boundary, not wall-clock speed.
    await store.create_session("batch")
    statements = []
    mock_db.set_trace_callback(statements.append)
    loop = NativeLoop(provider=object(), store=store, model="test", session_id="batch")
    try:
        for i in range(65):
            await loop._emit(TextDelta(session_id="batch", index=0, text=str(i)))
        await loop._emit(TurnComplete(session_id="batch", stop_reason="end_turn", num_model_calls=1))
    finally:
        await loop.aclose()
    commits = [s for s in statements if s.upper() == "COMMIT"]
    assert len(commits) == 3
    assert not store._pending_events
    replay = await PgFluidStore().events_since("batch", 0)
    assert len(replay) == 66
    assert [e.seq for e in replay] == sorted(e.seq for e in replay)
    assert isinstance(replay[-1], TurnComplete)
    assert await PgFluidStore().last_event_seq("batch") == replay[-1].seq


@pytest.mark.asyncio
async def test_resume_loads_nodes_not_events_and_bounds_replay_cache(mock_db):
    store = PgFluidStore()
    await store.create_session("large")
    store.STREAM_BATCH_SECONDS = 60
    for seq in range(1, 301):
        await store.append_stream_event("large", TextDelta(session_id="large", index=0, text="x", seq=seq))
    await store.flush_events("large")
    queries = []
    mock_db.set_trace_callback(queries.append)
    resumed = PgFluidStore()
    assert await resumed.get_session("large")
    assert await resumed.last_event_seq("large") == 300
    assert not any("SELECT event FROM" in q for q in queries)
    assert "large" not in resumed._events
    assert len(await resumed.events_since("large", 0)) == 300
    assert len(resumed._events["large"]) == resumed.EVENT_CACHE_LIMIT
    queries.clear()
    assert len(await resumed.events_since("large", 299)) == 1
    assert not queries
    assert len(await resumed.events_since("large", 20)) == 280
    assert len(resumed._events["large"]) == resumed.EVENT_CACHE_LIMIT


@pytest.mark.asyncio
async def test_node_append_and_replay_flush_pending_deltas(mock_db):
    store = PgFluidStore()
    await store.create_session("barriers")
    await store.append_stream_event("barriers", TextDelta(session_id="barriers", index=0, text="a", seq=1))
    assert not mock_db.execute("SELECT seq FROM fluid_session_events").fetchall()
    await store.append("barriers", _node("answer", None, "a"))
    assert await PgFluidStore().last_event_seq("barriers") == 1
    await store.append_stream_event("barriers", TextDelta(session_id="barriers", index=0, text="b", seq=2))
    assert len(await store.events_since("barriers", 0)) == 2
    assert not store._pending_events


@pytest.mark.asyncio
async def test_real_native_turn_interrupt_flushes_reconstructible_tail(mock_db):
    import asyncio
    from fluid.engine.native import NativeLoop
    from fluid.events import Interrupt, Interrupted, UserTurn
    from fluid.providers.base import ProviderCaps, StreamChunk
    reached = asyncio.Event()
    class Provider:
        name = "anthropic"
        caps = ProviderCaps()
        async def stream(self, request):
            yield StreamChunk("message_start", {"usage": {"input_tokens": 1}})
            yield StreamChunk("block_start", {"index": 0, "block_type": "text"})
            yield StreamChunk("text_delta", {"index": 0, "text": "partial"})
            reached.set()
            await asyncio.Event().wait()
    store = PgFluidStore()
    loop = NativeLoop(provider=Provider(), store=store, model="claude-sonnet-5")
    try:
        await loop.submit(UserTurn(content="Start then stop"))
        await asyncio.wait_for(reached.wait(), 5)
        await loop.submit(Interrupt())
        await asyncio.wait_for(loop.wait_turn(), 5)
        sid = loop.session_id
        replay = await PgFluidStore().events_since(sid, 0)
        assert any(isinstance(e, TextDelta) and e.text == "partial" for e in replay)
        assert any(isinstance(e, Interrupted) for e in replay)
        assert not store._pending_events
    finally:
        await loop.aclose()


@pytest.mark.asyncio
async def test_failed_writes_do_not_publish_phantom_events(mock_db, monkeypatch):
    store = PgFluidStore()
    await store.create_session("failed")
    assert await store.events_since("failed", 0) == []
    def fail(*args):
        raise RuntimeError("write rejected")
    from app.core.provider import fluid_pg_store as _mod
    monkeypatch.setattr(_mod, "_append_events_sync", fail)
    with pytest.raises(RuntimeError, match="write rejected"):
        await store.append_event("failed", TextDelta(session_id="failed", index=0, text="lost", seq=1))
    assert await store.events_since("failed", 0) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["native", "delegated"])
@pytest.mark.parametrize("indexed", [True, False])
async def test_resume_sequence_read_failure_blocks_producer_until_retry(mock_db, monkeypatch, engine, indexed):
    from fluid.engine.native import NativeLoop
    from fluid.engine.delegated import DelegatedLoop
    from fluid.events.model import TurnComplete

    def make_loop(store):
        if engine == "native":
            return NativeLoop(provider=object(), store=store, model="test", session_id="resume-failure")
        return DelegatedLoop(store=store, model="test", session_id="resume-failure")

    original = make_loop(PgFluidStore())
    resumed = None
    try:
        await original._ensure_session()
        await original._emit(TurnComplete(session_id="resume-failure", stop_reason="end_turn", num_model_calls=1))
        persisted = await PgFluidStore().last_event_seq("resume-failure")
        store = PgFluidStore()
        if not indexed:
            monkeypatch.setattr(store, "last_event_seq", None)
        lookup = "last_event_seq" if indexed else "events_since"
        real_lookup = getattr(store, lookup)

        async def fail(*args):
            raise OSError("sequence lookup unavailable")

        monkeypatch.setattr(store, lookup, fail)
        resumed = make_loop(store)
        for _ in range(2):
            with pytest.raises(OSError, match="sequence lookup unavailable"):
                await resumed._ensure_session()
            assert resumed._session is None
            assert not resumed.bus.log("resume-failure")
        monkeypatch.setattr(store, lookup, real_lookup)
        await resumed._ensure_session()
        event = await resumed._emit(TurnComplete(session_id="resume-failure", stop_reason="end_turn", num_model_calls=1))
        assert event.seq == persisted + 1
        replay = await PgFluidStore().events_since("resume-failure", 0)
        assert [e.seq for e in replay] == list(range(1, event.seq + 1))
    finally:
        await original.aclose()
        if resumed is not None:
            await resumed.aclose()


@pytest.mark.asyncio
async def test_failed_load_is_retryable(mock_db, monkeypatch):
    await PgFluidStore().create_session("retry")
    resumed = PgFluidStore()
    from app.core.provider import fluid_pg_store as _mod
    original = _mod._load_session_sync
    def fail(*args):
        raise RuntimeError("read rejected")
    monkeypatch.setattr(_mod, "_load_session_sync", fail)
    with pytest.raises(RuntimeError):
        await resumed.get_session("retry")
    monkeypatch.setattr(_mod, "_load_session_sync", original)
    assert await resumed.get_session("retry")


# ─── Cross-session search (fluid's optional SessionStore.search hook) ────────
#
# The store backs the SessionSearch builtin over BOTH places a session's text
# can live: the native node tree it owns, and the SDK transcript mirror in
# session_store_entries. These tests seed one of each and pin the contract the
# tool relies on — scoring, exclusion of the caller's own session, and the
# meta/sidechain skips that mirror the sse_entry_text SQL function.


def _sse_row(mock_db, session_id, entry_json, subpath=None, created_at="2026-08-20 10:00:00"):
    mock_db.execute(
        "INSERT INTO session_store_entries "
        "(project_key, session_id, subpath, entry, created_at) VALUES (?, ?, ?, ?, ?)",
        ("proj", session_id, subpath, entry_json, created_at),
    )
    mock_db.commit()


async def _seed_searchable(mock_db):
    """Two past native sessions, one 'current' native session, and four SDK
    transcript rows (two searchable, two that must stay invisible)."""
    store = PgFluidStore()

    # Native arm: A matches two terms, B only one — A must outrank B.
    await store.create_session("ses_hit_a")
    await store.append(
        "ses_hit_a", _node("a1", None, "we tuned the zebrafish quokka pipeline")
    )
    await store.create_session("ses_hit_b")
    await store.append("ses_hit_b", _node("b1", None, "only the zebrafish part here"))

    # The caller's own session — SessionSearch excludes it (HistorySearch owns it).
    await store.create_session("ses_current")
    await store.append(
        "ses_current", _node("c1", None, "zebrafish quokka again, right here")
    )

    # SDK arm: a user entry with content blocks, an assistant entry with a
    # plain string body.
    _sse_row(
        mock_db,
        "ses_sdk_user",
        json.dumps({
            "type": "user",
            "message": {"content": [
                {"type": "text", "text": "remind me about the zebrafish run"},
                {"type": "image", "source": "ignored"},
            ]},
        }),
    )
    _sse_row(
        mock_db,
        "ses_sdk_assistant",
        json.dumps({
            "type": "assistant",
            "message": {"content": "the quokka zebrafish notes are in docs/"},
        }),
    )
    # Invisible: a meta entry, and a sidechain row stored under a subpath.
    _sse_row(
        mock_db,
        "ses_sdk_meta",
        json.dumps({
            "type": "user",
            "isMeta": True,
            "message": {"content": "zebrafish quokka meta noise"},
        }),
    )
    _sse_row(
        mock_db,
        "ses_sdk_subpath",
        json.dumps({
            "type": "assistant",
            "message": {"content": "zebrafish quokka sidechain noise"},
        }),
        subpath="subagent-1.jsonl",
    )
    return store


@pytest.mark.asyncio
async def test_search_spans_both_arms_and_ranks_by_score(mock_db):
    store = await _seed_searchable(mock_db)

    hits = await store.search(["zebrafish", "quokka"])
    by_id = {h.session_id: h for h in hits}

    # Both storage arms answer the same query.
    assert "ses_hit_a" in by_id and "ses_hit_b" in by_id
    assert "ses_sdk_user" in by_id and "ses_sdk_assistant" in by_id

    # Two terms in one text beats one.
    assert by_id["ses_hit_a"].score == 2
    assert by_id["ses_hit_b"].score == 1
    assert hits.index(by_id["ses_hit_a"]) < hits.index(by_id["ses_hit_b"])
    assert [h.score for h in hits] == sorted((h.score for h in hits), reverse=True)


@pytest.mark.asyncio
async def test_search_returns_search_hits_with_capped_texts(mock_db):
    store = await _seed_searchable(mock_db)
    # Four more matching nodes on one session — texts must cap at 3.
    for i in range(4):
        await store.append(
            "ses_hit_b", _node(f"extra{i}", None, f"zebrafish note {i}")
        )

    hit = next(h for h in await store.search(["zebrafish"]) if h.session_id == "ses_hit_b")
    assert isinstance(hit, SessionSearchHit)
    assert 0 < len(hit.texts) <= 3
    assert all(isinstance(role, str) and "zebrafish" in text.lower()
               for role, text in hit.texts)
    assert hit.last_ts > 0

    # The SDK arm carries a usable timestamp too (created_at, not the node ts).
    sdk = next(h for h in await store.search(["zebrafish"])
               if h.session_id == "ses_sdk_user")
    assert sdk.last_ts > 0
    assert sdk.texts[0][0] == "user"


@pytest.mark.asyncio
async def test_search_excludes_the_calling_session(mock_db):
    store = await _seed_searchable(mock_db)

    assert any(h.session_id == "ses_current" for h in await store.search(["zebrafish"]))
    excluded = await store.search(["zebrafish"], exclude_session_id="ses_current")
    assert not any(h.session_id == "ses_current" for h in excluded)
    # Everything else still comes back.
    assert {"ses_hit_a", "ses_hit_b"} <= {h.session_id for h in excluded}


@pytest.mark.asyncio
async def test_search_skips_meta_and_sidechain_transcript_rows(mock_db):
    store = await _seed_searchable(mock_db)
    found = {h.session_id for h in await store.search(["zebrafish", "quokka"])}
    assert "ses_sdk_meta" not in found
    assert "ses_sdk_subpath" not in found


@pytest.mark.asyncio
async def test_search_unknown_term_returns_nothing(mock_db):
    store = await _seed_searchable(mock_db)
    assert await store.search(["capybara"]) == []


@pytest.mark.asyncio
async def test_search_sql_wildcards_in_a_term_are_inert(mock_db):
    """The SQL LIKE is only a prefilter — the Python substring test is the real
    match, so a '%' a user typed cannot widen the result set."""
    store = await _seed_searchable(mock_db)
    assert await store.search(["zebra%fish"]) == []


# ─── Titles, recent listing, and session slices (recall hooks) ───────────────
#
# ``sessions.sdk_session_id`` carries the store-side id for both legs, so the
# app's own titles decorate search hits and listings; ``recent_sessions`` backs
# SessionSearch's no-query listing and ``session_slice`` backs SessionView.

from fluid.messages.content import ToolResultBlock, ToolUseBlock  # noqa: E402
from fluid.store.base import SessionSlice, SessionSummary  # noqa: E402


def _session_row(
    mock_db, app_id, sdk_id, title,
    updated_at="2026-08-20 10:00:00", hidden=None, status="active",
):
    mock_db.execute(
        "INSERT INTO sessions (id, sdk_session_id, title, status, hidden, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (app_id, sdk_id, title, status, hidden, updated_at),
    )
    mock_db.commit()


def _assistant_node(node_id, parent_id, text):
    return SessionNode(
        node_id=node_id,
        parent_id=parent_id,
        message=Message(role="assistant", content=[Text(text=text)]),
    )


@pytest.mark.asyncio
async def test_search_hits_carry_the_apps_session_titles(mock_db):
    store = await _seed_searchable(mock_db)
    _session_row(mock_db, "app-a", "ses_hit_a", "Zebrafish pipeline tuning")

    hits = await store.search(["zebrafish"])
    by_id = {h.session_id: h for h in hits}
    assert by_id["ses_hit_a"].title == "Zebrafish pipeline tuning"
    # A session without an app row (or without a title) stays untitled.
    assert by_id["ses_hit_b"].title is None


@pytest.mark.asyncio
async def test_search_hits_and_listings_carry_project_roots(mock_db, tmp_path):
    """SessionSearch scopes recall on project_root, so hits and recent-session
    rows must surface the recorded root for native sessions — and None (never a
    wrong project) for SDK-mirror sessions, which have no fluid_sessions row."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)

    store = await _seed_searchable(mock_db)
    await store.create_session("ses_scoped", cwd=str(repo))
    await store.append("ses_scoped", _node("s1", None, "zebrafish in this repo"))

    by_id = {h.session_id: h for h in await store.search(["zebrafish"])}
    assert by_id["ses_scoped"].project_root == str(repo.resolve())
    assert by_id["ses_hit_a"].project_root is None  # created with no cwd
    assert by_id["ses_sdk_user"].project_root is None  # SDK arm: unknown

    _session_row(mock_db, "app-s", "ses_scoped", "Scoped session")
    rows = await store.recent_sessions(limit=10)
    roots = {r.session_id: r.project_root for r in rows}
    assert roots["ses_scoped"] == str(repo.resolve())


@pytest.mark.asyncio
async def test_recent_sessions_lists_newest_first_with_previews(mock_db):
    store = PgFluidStore()
    await store.create_session("ses_r1")
    await store.append("ses_r1", _node("u1", None, "fix the login redirect"))
    await store.append("ses_r1", _assistant_node("a1", "u1", "Fixed — lands on /home now."))
    _session_row(mock_db, "app-1", "ses_r1", "Login redirect fix",
                 updated_at="2026-08-20 10:00:00")

    _sse_row(mock_db, "ses_r2", json.dumps({
        "type": "user", "message": {"content": "polish the readme"},
    }))
    _sse_row(mock_db, "ses_r2", json.dumps({
        "type": "assistant", "message": {"content": "Readme polished and pushed."},
    }))
    _session_row(mock_db, "app-2", "ses_r2", "Readme polish",
                 updated_at="2026-08-21 10:00:00")

    # Hidden and archived sessions never surface in the listing.
    _session_row(mock_db, "app-3", "ses_hidden", "Hidden one",
                 updated_at="2026-08-22 10:00:00", hidden=1)
    _session_row(mock_db, "app-4", "ses_archived", "Archived one",
                 updated_at="2026-08-23 10:00:00", status="archived")

    rows = await store.recent_sessions(limit=10)
    assert [r.session_id for r in rows] == ["ses_r2", "ses_r1"]
    assert all(isinstance(r, SessionSummary) for r in rows)
    assert rows[0].title == "Readme polish"
    assert rows[0].preview == "Readme polished and pushed."
    assert rows[0].message_count == 2
    assert rows[1].title == "Login redirect fix"
    assert rows[1].preview == "Fixed — lands on /home now."
    assert rows[1].message_count == 2
    assert rows[0].last_ts > rows[1].last_ts > 0

    # The caller's own session is excluded, like search.
    excluded = await store.recent_sessions(limit=10, exclude_session_id="ses_r2")
    assert [r.session_id for r in excluded] == ["ses_r1"]


@pytest.mark.asyncio
async def test_session_slice_fluid_arm_renders_tool_lines(mock_db):
    store = PgFluidStore()
    await store.create_session("ses_v1")
    await store.append("ses_v1", _node("n1", None, "run the tests"))
    await store.append("ses_v1", SessionNode(
        node_id="n2", parent_id="n1",
        message=Message(role="assistant", content=[
            Text(text="Running them now."),
            ToolUseBlock(id="t1", name="Bash", input={"command": "pytest -q"}),
        ]),
    ))
    await store.append("ses_v1", SessionNode(
        node_id="n3", parent_id="n2",
        message=Message(role="user", content=[
            ToolResultBlock(tool_use_id="t1", content=[Text(text="2 passed")]),
        ]),
    ))
    _session_row(mock_db, "app-v1", "ses_v1", "Test run")

    window = await store.session_slice("ses_v1")
    assert isinstance(window, SessionSlice)
    assert window.total == 3 and window.start == 0
    assert window.title == "Test run"
    assert window.messages[0].text == "run the tests"
    assert any("Bash(" in line for line in window.messages[1].tools)
    assert any("-> 2 passed" in line for line in window.messages[2].tools)

    # Paging: an explicit window keeps python-slice semantics.
    paged = await store.session_slice("ses_v1", start=1, count=1)
    assert paged.start == 1 and len(paged.messages) == 1
    assert paged.messages[0].text == "Running them now."
    tail = await store.session_slice("ses_v1", start=-1, count=5)
    assert tail.start == 2 and len(tail.messages) == 1


@pytest.mark.asyncio
async def test_session_slice_sdk_arm_skips_meta_and_renders_tools(mock_db):
    _sse_row(mock_db, "ses_v2", json.dumps({
        "type": "user", "message": {"content": "check the deploy"},
    }))
    _sse_row(mock_db, "ses_v2", json.dumps({
        "type": "assistant",
        "message": {"content": [
            {"type": "text", "text": "Checking."},
            {"type": "tool_use", "name": "Bash", "input": {"command": "gh run list"}},
        ]},
    }))
    _sse_row(mock_db, "ses_v2", json.dumps({
        "type": "user",
        "message": {"content": [
            {"type": "tool_result", "content": [{"type": "text", "text": "all green"}]},
        ]},
    }))
    _sse_row(mock_db, "ses_v2", json.dumps({
        "type": "user", "isMeta": True, "message": {"content": "meta noise"},
    }))

    window = await PgFluidStore().session_slice("ses_v2")
    assert window is not None
    assert window.total == 3  # the meta row is invisible
    assert window.messages[0].text == "check the deploy"
    assert any("Bash(" in line for line in window.messages[1].tools)
    assert any("-> all green" in line for line in window.messages[2].tools)
    assert "meta noise" not in " ".join(m.text for m in window.messages)


@pytest.mark.asyncio
async def test_session_slice_unknown_session_is_none(mock_db):
    assert await PgFluidStore().session_slice("ses_nope") is None
