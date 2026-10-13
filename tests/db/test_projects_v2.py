"""Unit tests for app/db/projects_v2.py — the origin-keyed project registry
(v2 core, Phase 3)."""

from app.db import projects_v2


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


# ---------------------------------------------------------------------------
# Batched readers — the session-list enrichment path (one query per page).
# ---------------------------------------------------------------------------

def test_get_projects_v2_by_ids_matches_the_per_id_path(mock_db):
    """The batched read must return exactly what ``get_project_v2`` per id did."""
    a = projects_v2.upsert_project("git:a", name="A")
    b = projects_v2.upsert_project("git:b", name="B")
    projects_v2.upsert_project("git:c", name="C")  # not requested

    wanted = [a["id"], b["id"]]
    batched = projects_v2.get_projects_v2_by_ids(wanted)
    per_id = {pid: projects_v2.get_project_v2(pid) for pid in wanted}
    assert batched == per_id
    assert set(batched) == set(wanted)


def test_get_projects_v2_by_ids_omits_missing_and_dedupes(mock_db):
    a = projects_v2.upsert_project("git:a", name="A")
    got = projects_v2.get_projects_v2_by_ids([a["id"], a["id"], "pv2-nope"])
    assert list(got) == [a["id"]]
    # Unknown id resolves to nothing in BOTH forms.
    assert projects_v2.get_project_v2("pv2-nope") is None


def test_get_projects_v2_by_ids_empty_input_makes_no_query(mock_db):
    """``WHERE id IN ()`` is a Postgres syntax error — short-circuit instead."""
    projects_v2.upsert_project("git:a", name="A")
    assert projects_v2.get_projects_v2_by_ids([]) == {}
    assert projects_v2.get_projects_v2_by_ids([None, ""]) == {}


def test_get_projects_v2_by_origins_matches_the_per_origin_path(mock_db):
    projects_v2.upsert_project("git:a", name="A")
    projects_v2.upsert_project("manual:g1", name="G")
    projects_v2.upsert_project("local:x", name="X")  # not requested

    wanted = ["git:a", "manual:g1"]
    batched = projects_v2.get_projects_v2_by_origins(wanted)
    per_origin = {o: projects_v2.get_project_v2_by_origin(o) for o in wanted}
    assert batched == per_origin
    assert batched["git:a"]["id"] == projects_v2.project_v2_id_for_origin("git:a")


def test_get_projects_v2_by_origins_omits_missing_and_handles_empty(mock_db):
    projects_v2.upsert_project("git:a", name="A")
    assert projects_v2.get_projects_v2_by_origins(["git:a", "git:none"]) == {
        "git:a": projects_v2.get_project_v2_by_origin("git:a")
    }
    assert projects_v2.get_projects_v2_by_origins([]) == {}
    assert projects_v2.get_projects_v2_by_origins(["", None]) == {}


def test_batched_readers_agree_with_the_full_scan(mock_db):
    """Together the two targeted reads reproduce what the old full
    ``list_projects_v2()`` scan gave the enrichment pipeline."""
    for origin in ("git:a", "manual:g1", "local:x"):
        projects_v2.upsert_project(origin, name=origin.upper())

    scanned = projects_v2.list_projects_v2()
    by_id_full = {r["id"]: r for r in scanned}
    origin_to_id_full = {r["origin"]: r["id"] for r in scanned}

    ids = list(by_id_full)
    origins = list(origin_to_id_full)
    assert projects_v2.get_projects_v2_by_ids(ids) == by_id_full
    assert {
        o: r["id"] for o, r in projects_v2.get_projects_v2_by_origins(origins).items()
    } == origin_to_id_full
