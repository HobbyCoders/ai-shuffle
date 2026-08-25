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
