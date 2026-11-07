"""The shared session factory follows the currently routed, isolated database."""
from app.db import session_events, sessions
from tests.pg_shim import new_sqlite_connection, route_db_to, close_connection


def test_session_factory_persists_before_real_event_append(session_factory):
    assert sessions.get_session("fixture-chat") is None
    row = session_factory("fixture-chat", title="Fixture chat")
    assert row["id"] == "fixture-chat"
    assert sessions.get_session("fixture-chat")["title"] == "Fixture chat"
    assert session_events.get_events("fixture-chat") == []
    assert session_events.append_events("fixture-chat", [{"type": "turn.started"}]) == (1, 1)
    # Sharing a setup helper must not overwrite a row's explicit attributes.
    assert session_factory("fixture-chat")["title"] == "Fixture chat"


def test_session_factory_uses_file_local_database_route(session_factory):
    connection = new_sqlite_connection()
    try:
        with route_db_to(connection):
            session_factory("file-local-chat")
            assert sessions.get_session("file-local-chat") is not None
            assert session_events.append_events("file-local-chat", [{"type": "turn.started"}]) == (1, 1)
        assert sessions.get_session("file-local-chat") is None
    finally:
        close_connection(connection)
