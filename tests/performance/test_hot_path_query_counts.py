"""Hot API paths issue a fixed number of SQL statements, whatever the row count.

Every read the UI polls or opens constantly — the session sidebar (list,
paginated, cursor, search), a session's transcript (REST detail, ``/live`` and
the chat WebSocket's ``load_session`` hydration), projects, automations, notes
and the usage/analytics panel — is measured end to end at two sizes. The
statement count at ``LARGE`` rows must not exceed the count at ``SMALL`` rows:
a per-row query (an N+1) anywhere in the route, its enrichment or its
``run_sync`` closures makes the large count grow and fails the test with the
statements that multiplied.

Counting uses ``sqlite3.Connection.set_trace_callback`` on the per-test
database behind the Postgres shim (``tests/pg_shim.py``), so every statement the
app layer executes is seen — no production hook. Routes are mounted on a bare
FastAPI app (no lifespan), so startup's background loops never add noise.

Each measurement runs against its own cold entity (a separate session for the
small and the large transcript) after a warm-up request, so per-session caches
cannot hide a per-row read and one-time process caches cannot fake one.
"""

from __future__ import annotations

import contextlib
import json
import re
import threading
from collections import Counter
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

SMALL = 25
LARGE = 250

_DML = re.compile(r"^\s*(SELECT|INSERT|UPDATE|DELETE|WITH)\b", re.IGNORECASE)


@contextlib.contextmanager
def count_statements(raw_conn):
    """Collect every DML statement executed on ``raw_conn`` inside the block."""
    statements: list[str] = []

    def _trace(sql: str) -> None:
        if _DML.match(sql):
            statements.append(sql)

    raw_conn.set_trace_callback(_trace)
    try:
        yield statements
    finally:
        raw_conn.set_trace_callback(None)


def _shape(sql: str) -> str:
    """A statement with its literals folded, for grouping in failure output."""
    sql = re.sub(r"'[^']*'", "?", sql)
    sql = re.sub(r"\b\d+\b", "?", sql)
    sql = re.sub(r"\bIN \([^)]*\)", "IN (...)", sql, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", sql).strip()[:160]


def assert_flat(small: list[str], large: list[str], what: str) -> None:
    """``large`` ran no more statements than ``small``; show what grew if not."""
    if len(large) <= len(small):
        return
    grown = Counter(map(_shape, large))
    grown.subtract(Counter(map(_shape, small)))
    detail = "\n".join(
        f"  +{n} x {shape}" for shape, n in grown.most_common() if n > 0
    )
    pytest.fail(
        f"{what}: {len(large)} statements at {LARGE} rows vs {len(small)} at "
        f"{SMALL} rows — a per-row query crept in:\n{detail}"
    )


# ---------------------------------------------------------------------------
# App + seeding helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def api(mock_db):
    """The hot-path routers on a bare app, authenticated as the owner."""
    from app.api import analytics, automations, notes, projects, sessions, tags
    from app.api.auth import require_admin, require_auth

    app = FastAPI()
    for module in (sessions, projects, tags, analytics, automations, notes):
        app.include_router(module.router)
    app.dependency_overrides[require_auth] = lambda: "test-token"
    app.dependency_overrides[require_admin] = lambda: None
    return TestClient(app)


def _seed_directory():
    """Projects, API users and tags the seeded sessions reference."""
    from app.db import database
    from app.db.api_users import create_api_user
    from app.db.tags import create_tag

    for i in range(10):
        database.create_project(project_id=f"p{i}", name=f"P{i}", description=None, path=f"p{i}")
    for i in range(5):
        create_api_user(user_id=f"u{i}", name=f"User {i}", api_key_hash=f"hash-{i}")
        create_tag(tag_id=f"t{i}", name=f"Tag {i}")


def _seed_sessions(start: int, stop: int) -> None:
    """Sessions spread over projects, API users, tags, forks and usage rows."""
    from app.db import database
    from app.db.analytics import log_usage
    from app.db.tags import add_session_tag

    for i in range(start, stop):
        database.create_session(
            session_id=f"s{i}",
            profile_id="default",
            project_id=f"p{i % 10}",
            title=f"hello session {i}",
            api_user_id=f"u{i % 5}" if i % 2 else None,
            # Every fifth session is a fork of the previous one.
            parent_session_id=f"s{i - 1}" if i % 5 == 4 else None,
        )
        add_session_tag(f"s{i}", f"t{i % 5}")
        log_usage(f"s{i}", "default", "claude-sonnet", 10, 20, 0.01, 50)


def _seed_legacy_transcript(session_id: str, turns: int) -> None:
    """A pre-cutover transcript: ``turns`` user/assistant pairs in session_messages."""
    from app.db import database
    from app.db.analytics import log_usage

    database.create_session(session_id=session_id, profile_id="default", title=session_id)
    rows = []
    for j in range(turns):
        rows.append({"session_id": session_id, "role": "user", "content": f"question {j}",
                     "profile_id": "default", "metadata": {"n": j}})
        rows.append({"session_id": session_id, "role": "assistant", "content": f"answer {j}"})
        log_usage(session_id, "default", "claude-sonnet", 10, 20, 0.01, 50)
    database.add_session_messages_batch(rows)


def _seed_store_transcript(session_id: str, turns: int) -> None:
    """A post-cutover transcript: ``turns`` user/assistant pairs in the session store."""
    from app.core import pg_session_store
    from app.db import database
    from app.db.analytics import log_usage

    sdk_id = f"sdk-{session_id}"
    database.create_session(session_id=session_id, profile_id="default", title=session_id)
    database.update_session(session_id, sdk_session_id=sdk_id)
    entries = []
    for j in range(turns):
        entries.append({"type": "user", "uuid": f"{sdk_id}-u{j}",
                        "message": {"role": "user", "content": f"question {j}"}})
        entries.append({"type": "assistant", "uuid": f"{sdk_id}-a{j}",
                        "message": {"role": "assistant",
                                    "content": [{"type": "text", "text": f"answer {j}"}]}})
        database.add_session_message(session_id, "user", f"question {j}", profile_id="default")
        log_usage(session_id, "default", "claude-sonnet", 10, 20, 0.01, 50)
    # The summary fold is a separate, coalesced background concern; skip it so
    # seeding does not depend on its Postgres-only JSON handling.
    with patch.object(pg_session_store, "_claim_fold", return_value=False):
        pg_session_store._append_sync({"project_key": "-workspace", "session_id": sdk_id}, entries)


# ---------------------------------------------------------------------------
# Session sidebar
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["", "/paginated", "/cursor"])
def test_session_listing_is_flat_in_page_size(api, mock_db, path):
    url = f"/api/v1/sessions{path}?limit=100&sync_claude_sessions=false"
    _seed_directory()
    _seed_sessions(0, SMALL)
    api.get(url)  # warm process-level caches (settings, installs registry)

    with count_statements(mock_db) as small:
        resp = api.get(url)
    assert resp.status_code == 200
    body = resp.json()
    assert len(body if isinstance(body, list) else body["items"]) == SMALL

    _seed_sessions(SMALL, LARGE)
    with count_statements(mock_db) as large:
        resp = api.get(url)
    assert resp.status_code == 200
    items = resp.json() if not path else resp.json()["items"]
    assert len(items) == 100
    # The enrichment really ran over the page (tags, forks, api user names).
    assert all(item["tags"] for item in items)
    assert any(item["has_forks"] for item in items)
    assert any(item.get("api_user_name") for item in items)

    assert_flat(small, large, f"GET /sessions{path}")


def test_session_search_enrichment_is_flat_in_result_count(api, mock_db):
    """The search SQL itself is one Postgres-only statement (FTS/DISTINCT ON,
    which the SQLite shim cannot run); what must stay flat is everything the
    route does around it. The stub returns real session rows through one read,
    so the enrichment (tags, forks, projects, installs) runs against the DB."""
    from app.api import sessions as sessions_api

    def _fake_search(query, limit, offset, **_filters):
        rows = sessions_api.database.get_sessions(limit=limit, offset=offset)
        for row in rows:
            row.update(match_type="title", match_snippet=row["title"],
                       match_time=row["updated_at"], match_count=1)
        return rows, len(rows)

    url = "/api/v1/sessions/search/paginated?q=hello&limit=50"
    _seed_directory()
    _seed_sessions(0, SMALL)
    with patch.object(sessions_api.database, "search_sessions_paginated", _fake_search):
        api.get(url)
        with count_statements(mock_db) as small:
            assert len(api.get(url).json()["items"]) == SMALL
        _seed_sessions(SMALL, LARGE)
        with count_statements(mock_db) as large:
            items = api.get(url).json()["items"]
    assert len(items) == 50 and all(item["tags"] for item in items)
    assert_flat(small, large, "GET /sessions/search/paginated")


# ---------------------------------------------------------------------------
# Transcript loading: REST detail, /live, WebSocket hydration
# ---------------------------------------------------------------------------


@pytest.fixture
def jsonb_entries():
    """Decode store entries the way psycopg2 decodes JSONB (the shim stores TEXT).

    The real ``SELECT entry FROM session_store_entries`` still runs and is
    counted; only its column value is parsed, as production receives it.
    """
    from app.core import pg_session_store

    real = pg_session_store._load_entries_by_session_id_sync

    def _decoded(session_id, subpath=None):
        return [json.loads(e) if isinstance(e, str) else e for e in real(session_id, subpath)]

    with patch.object(pg_session_store, "_load_entries_by_session_id_sync", _decoded):
        yield


@pytest.mark.parametrize("seed", [_seed_legacy_transcript, _seed_store_transcript],
                         ids=["legacy-session_messages", "session-store"])
@pytest.mark.parametrize("suffix", ["", "/live"])
def test_transcript_reads_are_flat_in_message_count(api, mock_db, jsonb_entries, seed, suffix):
    seed("warm", 2)
    seed("small", SMALL)
    seed("large", LARGE)
    api.get(f"/api/v1/sessions/warm{suffix}")

    with count_statements(mock_db) as small:
        resp = api.get(f"/api/v1/sessions/small{suffix}")
    assert resp.status_code == 200
    assert len(resp.json()["messages"]) == 2 * SMALL

    with count_statements(mock_db) as large:
        resp = api.get(f"/api/v1/sessions/large{suffix}")
    assert resp.status_code == 200
    messages = resp.json()["messages"]
    assert len(messages) == 2 * LARGE
    # Cost metadata was attached per turn from ONE usage read.
    assert messages[-1]["metadata"].get("is_final_message") is True

    assert_flat(small, large, f"GET /sessions/{{id}}{suffix} ({seed.__name__})")


async def _load_session_over_ws(session_id: str) -> dict:
    import app.api.chat_ws_handler as cwh

    sync = MagicMock()
    for name in ("register_device", "unregister_device", "broadcast_session_opened"):
        setattr(sync, name, AsyncMock())
    sync.is_device_watching_session = MagicMock(return_value=False)
    with patch.object(cwh, "sync_engine", sync):
        handler = cwh.ChatWebSocketHandler(
            websocket=MagicMock(), device_id="dev-1", user_id="admin", api_user_id=None,
        )
        handler.send_json = AsyncMock(return_value=True)
        await handler.handle_load_session({"session_id": session_id})
    frames = [
        c.args[0] for c in handler.send_json.call_args_list
        if c.args and isinstance(c.args[0], dict) and c.args[0].get("type") == "history"
    ]
    assert len(frames) == 1
    return frames[0]


async def test_ws_load_session_hydration_is_flat_in_message_count(mock_db):
    _seed_legacy_transcript("warm", 2)
    _seed_legacy_transcript("small", SMALL)
    _seed_legacy_transcript("large", LARGE)
    await _load_session_over_ws("warm")

    with count_statements(mock_db) as small:
        frame = await _load_session_over_ws("small")
    assert len(frame["messages"]) == 2 * SMALL
    with count_statements(mock_db) as large:
        frame = await _load_session_over_ws("large")
    assert len(frame["messages"]) == 2 * LARGE

    assert_flat(small, large, "chat WebSocket load_session")


async def test_ws_load_session_resolves_the_cwd_off_the_event_loop(mock_db, loop_thread):
    """The cwd resolution reads (and may self-heal) the session's checkout
    binding — DB work that must ride the same worker hop as the transcript."""
    import app.api.chat_ws_handler as cwh

    _seed_legacy_transcript("s-cwd", 1)
    threads: list[int] = []
    real = cwh.resolve_working_dir

    def _spy(session):
        threads.append(threading.get_ident())
        return real(session)

    with patch.object(cwh, "resolve_working_dir", _spy):
        await _load_session_over_ws("s-cwd")
    assert threads and loop_thread not in threads


# ---------------------------------------------------------------------------
# Projects, automations, notes, analytics
# ---------------------------------------------------------------------------


def _grow_and_measure(api, raw_conn, url, grow):
    """Measure ``url`` at SMALL rows, grow to LARGE, measure again."""
    grow(0, SMALL)
    api.get(url)
    with count_statements(raw_conn) as small:
        first = api.get(url)
    grow(SMALL, LARGE)
    with count_statements(raw_conn) as large:
        second = api.get(url)
    assert first.status_code == second.status_code == 200
    return small, large, first.json(), second.json()


def test_project_listing_is_flat(api, mock_db):
    from app.db import database

    def grow(start, stop):
        for i in range(start, stop):
            database.create_project(project_id=f"p{i}", name=f"P{i}", description=None, path=f"p{i}")
            database.create_session(session_id=f"s{i}", profile_id="default", project_id=f"p{i}")

    small, large, first, second = _grow_and_measure(api, mock_db, "/api/v1/projects", grow)
    assert len(first) == SMALL and len(second) == LARGE
    assert all(p["session_count"] == 1 for p in second)
    assert_flat(small, large, "GET /projects")


def test_automation_listing_is_flat(api, mock_db):
    from app.db.automations import create_automation, create_automation_run

    def grow(start, stop):
        for i in range(start, stop):
            create_automation(f"a{i}", f"Automation {i}", "manual", {}, "default", prompt="hi")
            create_automation_run(f"r{i}", f"a{i}")

    small, large, first, second = _grow_and_measure(api, mock_db, "/api/v1/automations", grow)
    assert len(first) == SMALL and len(second) == LARGE
    assert_flat(small, large, "GET /automations")


def test_note_listing_and_tags_are_flat(api, mock_db):
    from app.db.notes import create_note

    def grow(start, stop):
        for i in range(start, stop):
            create_note(f"n{i}", f"Note {i}", "body " * 50, tags=[f"tag{i % 7}"])

    for url in ("/api/v1/notes?limit=500", "/api/v1/notes/tags", "/api/v1/notes/autocomplete?limit=50"):
        mock_db.execute("DELETE FROM notes")
        small, large, _, _ = _grow_and_measure(api, mock_db, url, grow)
        assert_flat(small, large, f"GET {url}")


def test_note_autocomplete_reads_only_the_preview(api, mock_db):
    """Autocomplete shows a 100-char preview; it must not pull every whole
    note body across the wire to build it — and the preview is unchanged."""
    from app.db.notes import create_note

    create_note("long", "Long", "x" * 5000)
    create_note("edge", "Edge", "y" * 100)
    create_note("short", "Short", "tiny")
    create_note("empty", "Empty", "")

    with count_statements(mock_db) as statements:
        resp = api.get("/api/v1/notes/autocomplete?limit=50")
    assert resp.status_code == 200
    previews = {n["id"]: n["preview"] for n in resp.json()}
    assert previews == {
        "long": "x" * 100 + "...",
        "edge": "y" * 100,
        "short": "tiny",
        "empty": "",
    }
    reads = [s for s in statements if "FROM notes" in s]
    assert len(reads) == 1
    assert re.search(r"SUBSTR\(content, 1, 101\)", reads[0], re.IGNORECASE), reads[0]
    assert not re.search(r"[\s,]content\s*,", reads[0].split("FROM")[0]), reads[0]


@pytest.mark.parametrize("path", ["summary", "costs", "trends", "top-sessions"])
def test_usage_analytics_are_flat(api, mock_db, path):
    from app.db import database
    from app.db.analytics import log_usage

    def grow(start, stop):
        for i in range(start, stop):
            database.create_session(session_id=f"s{i}", profile_id="default", title=f"S{i}")
            log_usage(f"s{i}", "default", f"model-{i % 3}", 100, 200, 0.5, 10)

    small, large, _, _ = _grow_and_measure(api, mock_db, f"/api/v1/analytics/{path}", grow)
    assert_flat(small, large, f"GET /analytics/{path}")


# ---------------------------------------------------------------------------
# Chat-history @-mention listing (JSONL files, not SQL)
# ---------------------------------------------------------------------------


def _write_history(project_dir, count):
    import os

    project_dir.mkdir(parents=True)
    for i in range(count):
        path = project_dir / f"sess-{i:04d}.jsonl"
        lines = [
            {"type": "user", "message": {"role": "user", "content": f"Topic {i} alpha" if i % 3 else f"Topic {i} beta"}},
            {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": f"Reply {i}"}]}},
        ]
        path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")
        # Distinct mtimes, deliberately not in name order; a few ties.
        os.utime(path, (1_700_000_000 + (i * 7919) % count, 1_700_000_000 + (i * 7919) % count))
    (project_dir / "agent-sub.jsonl").write_text("{}\n")


def _reference_listing(jp, working_dir, search, limit):
    """The listing as it was defined before the lazy parse: parse all, sort, slice."""
    from datetime import datetime

    project_dir = jp.settings.get_claude_projects_dir / jp.get_project_dir_name(working_dir)
    rows = []
    for f in project_dir.glob("*.jsonl"):
        if f.name.startswith("agent-") or f.name.endswith(".hooks.jsonl"):
            continue
        st = f.stat()
        title, preview, count = jp._get_session_title_and_preview(f)
        if search and not ((title and search.lower() in title.lower())
                           or (preview and search.lower() in preview.lower())):
            continue
        rows.append({"name": title or f"Session {f.stem[:8]}...", "type": "chat_history",
                     "path": str(f), "sdk_session_id": f.stem, "title": title,
                     "preview": preview, "message_count": count,
                     "modified_at": datetime.fromtimestamp(st.st_mtime).isoformat(),
                     "size_bytes": st.st_size})
    rows.sort(key=lambda r: r["modified_at"], reverse=True)
    return rows[:limit]


@pytest.mark.parametrize("search", ["", "beta"])
def test_chat_history_listing_parses_only_what_it_returns(tmp_path, search):
    from app.core import jsonl_parser as jp

    projects_dir = tmp_path / "projects"
    working_dir = "/workspace/demo"
    with patch.object(jp, "settings") as mock_settings:
        mock_settings.get_claude_projects_dir = projects_dir
        _write_history(projects_dir / jp.get_project_dir_name(working_dir), LARGE)
        expected = _reference_listing(jp, working_dir, search, 20)
        newest_first = [r["path"] for r in _reference_listing(jp, working_dir, "", LARGE)]

        parsed = []
        real = jp._get_session_title_and_preview

        def _spy(path):
            parsed.append(path)
            return real(path)

        with patch.object(jp, "_get_session_title_and_preview", _spy):
            listed = jp.list_chat_history_sessions(working_dir=working_dir, search=search, limit=20)

    assert len(expected) == 20
    assert listed == expected
    # Transcripts are read newest first and only until `limit` rows are found:
    # exactly `limit` without a filter, and with one just up to the last match.
    assert [str(p) for p in parsed] == newest_first[: newest_first.index(expected[-1]["path"]) + 1]
    assert len(parsed) < LARGE
