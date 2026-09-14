from unittest.mock import MagicMock, patch

import pytest

from app.db import sessions


@pytest.mark.parametrize("paginated", [False, True])
def test_native_search_query_contract(paginated):
    cursor = MagicMock()
    cursor.fetchall.return_value = []
    cursor.fetchone.return_value = {"cnt": 0}
    captured = []
    cursor.execute.side_effect = lambda sql, params: captured.append((sql, params))
    with patch.object(sessions, "get_db"), patch.object(sessions, "get_cursor", return_value=cursor), patch.object(sessions, "_instance_filter", return_value=(" AND s.instance_id = %s", ["install"])):
        search = sessions.search_sessions_paginated if paginated else sessions.search_sessions
        search('50%_complete', project_id='project', api_user_id='owner')
    for sql, params in captured:
        assert sql.count('%s') == len(params)
        assert 's.sdk_session_id = n.session_id' in sql
        assert "n.message->>'role' IN ('user', 'assistant')" in sql
        assert "block->>'kind' = 'text'" in sql
        assert 'texts.content ILIKE %s' in sql
        assert sql.count('AND s.instance_id = %s') == 4
        assert 'AND project_id = %s' in sql
        assert 'AND api_user_id = %s' in sql
        assert r'%50\%\_complete%' in params
        assert 'SELECT * FROM native_matches' in sql


def test_literal_pattern_escapes_wildcards():
    assert sessions._search_literal_pattern(r'a_b%\c') == r'%a\_b\%\\c%'
