"""Project-filtered session lists, counts and searches follow the one rule (#1659).

A chat's project is its shared id (``project_v2_id``) when it has one; the
legacy slug decides only for a chat with no shared id. Slugs are per-worker:
another worker's ``sandbox`` may be an unrelated project here.
"""
from unittest.mock import MagicMock, patch

import pytest

from app.db import database as db
from app.db import sessions as db_sessions
from app.db.base import get_db
from app.db.projects_v2 import project_v2_id_for_origin

P = project_v2_id_for_origin("git:acme/p")
Q = project_v2_id_for_origin("git:acme/q")


@pytest.fixture
def world(mock_db):
    profile = db.create_profile("agent", "Agent", "", {"model": "m"}, False, [])["id"]
    db.create_project("mine", "P", None, "/p", link_group_id="git:acme/p")
    db.create_project("sandbox", "Q", None, "/q", link_group_id="git:acme/q")
    rows = {
        "mirrored": (None, P),           # another worker's chat of P, slug cleared
        "unreconciled": ("sandbox", P),  # sender's colliding slug not yet cleared
        "own-q": ("sandbox", Q),
        "legacy-q": ("sandbox", None),   # from before shared ids: the slug decides
    }
    for sid, (slug, shared) in rows.items():
        db.create_session(sid, profile, project_id=slug, title=f"hooks {sid}")
        with get_db() as conn:
            conn.cursor().execute("UPDATE sessions SET project_v2_id = %s WHERE id = %s", (shared, sid))
    return rows


def test_list_files_a_chat_by_its_shared_id(world):
    assert {row["id"] for row in db_sessions.get_sessions(project_id="mine")} == {"mirrored", "unreconciled"}
    assert {row["id"] for row in db_sessions.get_sessions(project_id="sandbox")} == {"own-q", "legacy-q"}


def test_count_files_a_chat_by_its_shared_id(world):
    rows, total = db_sessions.get_sessions_paginated(project_id="mine")
    assert total == 2 and {row["id"] for row in rows} == {"mirrored", "unreconciled"}
    _rows, total = db_sessions.get_sessions_paginated(project_id="sandbox")
    assert total == 2


@pytest.mark.parametrize("search", ["search_sessions", "search_sessions_paginated"])
def test_search_filters_by_the_shared_id_first(world, search):
    """FTS cannot run on the SQLite shim: check the filter the search sends."""
    captured = []
    cursor = MagicMock()
    cursor.fetchall.return_value = []
    # Schema discovery sees a pre-upgrade schema; the count query sees none.
    cursor.fetchone.side_effect = lambda: {"cnt": 0} if "COUNT(*)" in captured[-1][0] else None
    cursor.execute.side_effect = lambda sql, params=None: captured.append((sql, list(params or ())))
    with patch("app.db.sessions.get_cursor", return_value=cursor):
        getattr(db_sessions, search)("hooks", project_id="mine")
    searches = [(sql, params) for sql, params in captured if "title_matches" in sql]
    assert searches
    for sql, params in searches:
        assert "project_v2_id IN (" in sql and "project_v2_id IS NULL AND project_id = " in sql
        assert P in params and "mine" in params
        assert Q not in params


def test_a_project_unknown_here_matches_only_its_legacy_slug(world):
    from app.core.project_resolver import session_project_predicate

    sql, params = session_project_predicate("gone", "%s")
    assert sql == "(project_v2_id IS NULL AND project_id = %s)" and params == ["gone"]
