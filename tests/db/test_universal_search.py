from unittest.mock import MagicMock, patch

import pytest

from app.db import session_recall, sessions


@pytest.mark.parametrize("paginated", [False, True])
@pytest.mark.parametrize("deferred", [False, True])
@pytest.mark.parametrize("indexed", [False, True])
def test_native_search_query_contract(paginated: bool, deferred: bool, indexed: bool) -> None:
    cursor = MagicMock()
    cursor.fetchall.return_value = []
    captured: list[tuple[str, list]] = []
    cursor.execute.side_effect = lambda sql, params: captured.append((sql, params))

    def fetchone() -> dict | None:
        if captured[-1][0].startswith("SELECT column_name "):
            return {"column_name": "history_index_state"} if deferred else None
        return {"cnt": 0}

    cursor.fetchone.side_effect = fetchone
    with patch.object(sessions, "get_db"), \
         patch.object(session_recall, "native_search_ready", return_value=indexed), \
         patch.object(sessions, "get_cursor", return_value=cursor), \
         patch.object(sessions, "_instance_filter",
                      return_value=(" AND s.instance_id = %s", ["install"])):
        search = sessions.search_sessions_paginated if paginated else sessions.search_sessions
        search('50%_complete', project_id='project', api_user_id='owner')
    searches = 0
    for sql, params in captured:
        assert sql.count('%s') == len(params)
        if sql.startswith("SELECT column_name "):
            assert "information_schema.columns" in sql
            assert params[0] in ("fluid_session_nodes", "session_messages", "session_store_entries", "session_recall_text")
            assert params[1] == "history_index_state"
            continue
        searches += 1
        assert sql.lstrip().startswith("WITH title_matches AS (")
        assert 's.sdk_session_id = n.session_id' in sql
        assert "n.message->>'role' IN ('user', 'assistant')" in sql
        assert "block->>'kind' = 'text'" in sql
        assert 'texts.content ILIKE %s' in sql
        assert ("r.body ILIKE %s" in sql) == indexed
        assert (f"r.part >= {session_recall.NATIVE_CAPPED_PART}" in sql) == indexed
        assert sql.count('AND s.instance_id = %s') == 4
        assert 'AND project_id = %s' in sql
        assert 'AND api_user_id = %s' in sql
        assert r'%50\%\_complete%' in params
        assert 'SELECT * FROM native_matches' in sql
        for state in (0, 2):
            assert (f"history_index_state = {state}" in sql) == deferred
        assert "history_index_state = 1" not in sql
    assert searches == (2 if paginated else 1)


def test_literal_pattern_escapes_wildcards() -> None:
    assert sessions._search_literal_pattern(r'a_b%\c') == r'%a\_b\%\\c%'
