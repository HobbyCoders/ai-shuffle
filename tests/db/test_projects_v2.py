"""Unit tests for app/db/projects_v2.py — the origin-keyed project registry
(v2 core, Phase 3)."""

import pytest

from app.db import projects_v2


@pytest.fixture
def test_db():
    from tests.pg_shim import new_sqlite_connection
    conn = new_sqlite_connection()
    yield conn
    conn.close()


@pytest.fixture
def mock_db(test_db):
    from tests.pg_shim import route_db_to
    with route_db_to(test_db):
        yield test_db


def test_upsert_inserts_then_reads_back(mock_db):
    p = projects_v2.upsert_project("git:abc123", name="My Repo")
    assert p["origin"] == "git:abc123"
    assert p["name"] == "My Repo"
    assert p["settings"] == {}
    # id is deterministic in origin.
    assert p["id"] == projects_v2.project_v2_id_for_origin("git:abc123")

    got = projects_v2.get_project_v2(p["id"])
    assert got is not None and got["origin"] == "git:abc123"

    by_origin = projects_v2.get_project_v2_by_origin("git:abc123")
    assert by_origin is not None and by_origin["id"] == p["id"]


def test_upsert_is_idempotent_on_origin(mock_db):
    a = projects_v2.upsert_project("git:same", name="First")
    b = projects_v2.upsert_project("git:same", name="Second")
    # Same origin -> same row (id stable); name refreshed on the second upsert.
    assert a["id"] == b["id"]
    assert b["name"] == "Second"
    # Exactly one row for the origin.
    assert len(projects_v2.list_projects_v2()) == 1


def test_on_conflict_origin_dedupes_distinct_names(mock_db):
    projects_v2.upsert_project("manual:group-1", name="Alpha")
    projects_v2.upsert_project("manual:group-1", name="Beta")
    projects_v2.upsert_project("local:proj-x", name="Gamma")
    rows = projects_v2.list_projects_v2()
    origins = {r["origin"] for r in rows}
    assert origins == {"manual:group-1", "local:proj-x"}
    assert len(rows) == 2


def test_name_defaults_to_origin_when_omitted(mock_db):
    p = projects_v2.upsert_project("local:no-name")
    assert p["name"] == "local:no-name"


def test_settings_set_on_insert(mock_db):
    p = projects_v2.upsert_project("git:s", name="S", settings={"k": "v"})
    assert p["settings"] == {"k": "v"}
    got = projects_v2.get_project_v2_by_origin("git:s")
    assert got["settings"] == {"k": "v"}


def test_get_missing_returns_none(mock_db):
    assert projects_v2.get_project_v2("nope") is None
    assert projects_v2.get_project_v2_by_origin("nope") is None
