"""Tests for app/core/placement.py — the pure placement resolver (V2 Phase 4).

Uses a pg_shim SQLite DB for real ``checkouts`` rows; the install registry
(``get_installs_map``) is stubbed because its online-derivation SQL is
Postgres-only. ``effective_instance_id`` is pinned so "the local runner" is
deterministic. The resolver writes nothing — these tests only assert its
decisions.
"""

import pytest

from app.core import placement
from app.core.placement import PlacementDecision
from app.db import checkouts as checkouts_db
from app.db import database

RUNNER_ME = "local-me"
PROJECT = "pv2-x"


@pytest.fixture
def test_db():
    from tests.pg_shim import new_sqlite_connection
    conn = new_sqlite_connection()
    conn.execute("PRAGMA foreign_keys = OFF")
    yield conn
    conn.close()


@pytest.fixture
def mock_db(test_db):
    from tests.pg_shim import route_db_to
    with route_db_to(test_db) as shim:
        yield shim


@pytest.fixture(autouse=True)
def pin_runner(monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(
        type(settings), "effective_instance_id", property(lambda self: RUNNER_ME)
    )


def _stub_installs(monkeypatch, online_ids, caps_by_id=None):
    caps_by_id = caps_by_id or {}
    rows = {
        iid: {"id": iid, "online": True, "capabilities": caps_by_id.get(iid, {})}
        for iid in online_ids
    }
    monkeypatch.setattr(database, "get_installs_map", lambda: rows)


def _seed_checkout(project_id, runner_id, path, branch=None):
    checkouts_db.upsert_checkout(project_id, runner_id, path, branch=branch)


# ---------------------------------------------------------------------------
# elect_runner — deterministic lowest-id.
# ---------------------------------------------------------------------------

def test_elect_runner_is_lowest_id():
    assert placement.elect_runner(["runner-c", "runner-a", "runner-b"]) == "runner-a"


def test_elect_runner_none_when_empty():
    assert placement.elect_runner([]) is None


# ---------------------------------------------------------------------------
# capable_runners — checkout presence + capability filtering.
# ---------------------------------------------------------------------------

def test_capable_runners_filters_by_checkout_presence(mock_db, monkeypatch):
    _stub_installs(monkeypatch, ["runner-a", "runner-b", "runner-c"])
    _seed_checkout(PROJECT, "runner-a", "/a/x")
    _seed_checkout(PROJECT, "runner-b", "/b/x")
    # runner-c is online but has NO checkout for this project.
    assert placement.capable_runners(PROJECT) == ["runner-a", "runner-b"]


def test_capable_runners_skips_offline_runner(mock_db, monkeypatch):
    _stub_installs(monkeypatch, ["runner-a"])  # runner-b offline
    _seed_checkout(PROJECT, "runner-a", "/a/x")
    _seed_checkout(PROJECT, "runner-b", "/b/x")
    assert placement.capable_runners(PROJECT) == ["runner-a"]


def test_capable_runners_filters_by_capability(mock_db, monkeypatch):
    _stub_installs(
        monkeypatch,
        ["runner-a", "runner-b"],
        caps_by_id={
            "runner-a": {"has_chrome": True},
            "runner-b": {"has_chrome": False},
        },
    )
    _seed_checkout(PROJECT, "runner-a", "/a/x")
    _seed_checkout(PROJECT, "runner-b", "/b/x")
    assert placement.capable_runners(PROJECT, {"has_chrome": True}) == ["runner-a"]


def test_capable_runners_os_equality_constraint(mock_db, monkeypatch):
    _stub_installs(
        monkeypatch,
        ["runner-a", "runner-b"],
        caps_by_id={
            "runner-a": {"os": "linux"},
            "runner-b": {"os": "windows"},
        },
    )
    _seed_checkout(PROJECT, "runner-a", "/a/x")
    _seed_checkout(PROJECT, "runner-b", "/b/x")
    assert placement.capable_runners(PROJECT, {"os": "windows"}) == ["runner-b"]


def test_capable_runners_projectless_returns_all_online(mock_db, monkeypatch):
    _stub_installs(monkeypatch, [RUNNER_ME, "runner-a", "runner-b"])
    # No project_v2_id -> no checkout requirement. The approved local registry
    # row is retained even if its heartbeat is racing startup.
    assert placement.capable_runners(None) == ["local-me", "runner-a", "runner-b"]


def test_capable_runners_excludes_unapproved_local_runner(mock_db, monkeypatch):
    rows = {
        RUNNER_ME: {
            "id": RUNNER_ME,
            "online": True,
            "approved": False,
            "capabilities": {},
        },
        "runner-a": {
            "id": "runner-a",
            "online": True,
            "approved": True,
            "capabilities": {},
        },
    }
    monkeypatch.setattr(database, "get_installs_map", lambda: rows)

    assert placement.capable_runners(None) == ["runner-a"]


def test_capable_runners_counts_self_when_registry_read_fails(mock_db, monkeypatch):
    def boom():
        raise RuntimeError("registry down")
    monkeypatch.setattr(database, "get_installs_map", boom)
    assert placement.capable_runners(None) == [RUNNER_ME]


def test_capable_runners_counts_self_before_own_row_registers(mock_db, monkeypatch):
    rows = {
        "runner-a": {
            "id": "runner-a",
            "online": True,
            "approved": True,
            "capabilities": {},
        },
    }
    monkeypatch.setattr(database, "get_installs_map", lambda: rows)
    assert placement.capable_runners(None) == [RUNNER_ME, "runner-a"]


# ---------------------------------------------------------------------------
# resolve_placement — the three canonical forms.
# ---------------------------------------------------------------------------

def test_null_placement_ownerless_is_local_default(mock_db, monkeypatch):
    # A11: local-default requires BOTH placement NULL and instance_id empty —
    # an ownerless row is the only truly install-relative "local" case.
    _stub_installs(monkeypatch, [])
    d = placement.resolve_placement(
        {"placement": None, "instance_id": None, "project_v2_id": PROJECT}
    )
    assert d == PlacementDecision("local", RUNNER_ME, d.reason)
    assert d.kind == "local" and d.runner_id == RUNNER_ME


def test_blank_placement_string_ownerless_is_local_default(mock_db, monkeypatch):
    _stub_installs(monkeypatch, [])
    d = placement.resolve_placement(
        {"placement": "   ", "instance_id": "", "project_v2_id": PROJECT}
    )
    assert d.kind == "local" and d.runner_id == RUNNER_ME


# ---------------------------------------------------------------------------
# A11 — NULL placement falls back to instance_id as an IMPLICIT runner pin.
# ---------------------------------------------------------------------------

def test_null_placement_foreign_instance_id_is_implicit_pin(mock_db, monkeypatch):
    # No checkout seeded for the owner: like an explicit pin (A10), the
    # implicit pin is validated on online + capability ONLY.
    _stub_installs(monkeypatch, ["runner-owner"])
    d = placement.resolve_placement(
        {"placement": None, "instance_id": "runner-owner", "project_v2_id": PROJECT}
    )
    assert d.kind == "runner" and d.runner_id == "runner-owner"
    assert "implicit-pin:instance_id" in d.reason


def test_null_placement_foreign_owner_offline_yields_none(mock_db, monkeypatch):
    _stub_installs(monkeypatch, ["runner-a"])  # runner-owner offline
    d = placement.resolve_placement(
        {"placement": None, "instance_id": "runner-owner", "project_v2_id": PROJECT}
    )
    assert d.kind == "none" and d.runner_id is None


def test_null_placement_own_instance_id_resolves_runner_me(mock_db, monkeypatch):
    # Own-instance rows resolve ("runner", me) — the submit seam treats that as
    # NOT placed_remote and the execute gate passes, so behavior stays local.
    _stub_installs(monkeypatch, [RUNNER_ME])
    d = placement.resolve_placement(
        {"placement": None, "instance_id": RUNNER_ME, "project_v2_id": PROJECT}
    )
    assert d.kind == "runner" and d.runner_id == RUNNER_ME
    assert "implicit-pin:instance_id" in d.reason


def test_pinned_runner_honored_when_online_and_capable(mock_db, monkeypatch):
    _stub_installs(monkeypatch, ["runner-b"])
    _seed_checkout(PROJECT, "runner-b", "/b/x")
    d = placement.resolve_placement(
        {"placement": {"where": "runner:runner-b"}, "project_v2_id": PROJECT}
    )
    assert d.kind == "runner" and d.runner_id == "runner-b"


def test_pinned_runner_offline_yields_none(mock_db, monkeypatch):
    _stub_installs(monkeypatch, ["runner-a"])  # runner-b offline
    _seed_checkout(PROJECT, "runner-b", "/b/x")
    d = placement.resolve_placement(
        {"placement": {"where": "runner:runner-b"}, "project_v2_id": PROJECT}
    )
    assert d.kind == "none" and d.runner_id is None


def test_pinned_runner_without_checkout_still_honored(mock_db, monkeypatch):
    # A10: a pin is validated on online + capability ONLY (no checkout filter).
    # Checkout rows materialize lazily via Step-E adoption, so an online+capable
    # owner with no checkouts row yet is still honored; checkout honesty is
    # enforced later at spawn by the resolver-first refusal.
    _stub_installs(monkeypatch, ["runner-b"])  # online but no checkout seeded
    d = placement.resolve_placement(
        {"placement": {"where": "runner:runner-b"}, "project_v2_id": PROJECT}
    )
    assert d.kind == "runner" and d.runner_id == "runner-b"


def test_pinned_runner_failing_capability_constraint_yields_none(mock_db, monkeypatch):
    # A10: capability constraints STILL apply to a pin, even though the checkout
    # filter does not. runner-b is online (with a checkout) but lacks has_chrome.
    _stub_installs(
        monkeypatch,
        ["runner-b"],
        caps_by_id={"runner-b": {"has_chrome": False}},
    )
    _seed_checkout(PROJECT, "runner-b", "/b/x")
    d = placement.resolve_placement(
        {
            "placement": {"where": "runner:runner-b", "constraints": {"has_chrome": True}},
            "project_v2_id": PROJECT,
        }
    )
    assert d.kind == "none" and d.runner_id is None


def test_placement_as_json_string_is_parsed(mock_db, monkeypatch):
    _stub_installs(monkeypatch, ["runner-b"])
    _seed_checkout(PROJECT, "runner-b", "/b/x")
    # Some drivers / the SQLite shim hand placement back as a JSON string.
    d = placement.resolve_placement(
        {"placement": '{"where": "runner:runner-b"}', "project_v2_id": PROJECT}
    )
    assert d.kind == "runner" and d.runner_id == "runner-b"


def test_anywhere_elects_lowest_capable(mock_db, monkeypatch):
    _stub_installs(monkeypatch, ["runner-a", "runner-b"])
    _seed_checkout(PROJECT, "runner-a", "/a/x")
    _seed_checkout(PROJECT, "runner-b", "/b/x")
    d = placement.resolve_placement(
        {"placement": {"where": "anywhere"}, "project_v2_id": PROJECT}
    )
    assert d.kind == "runner" and d.runner_id == "runner-a"


def test_anywhere_with_constraint_filters_then_elects(mock_db, monkeypatch):
    _stub_installs(
        monkeypatch,
        ["runner-a", "runner-b"],
        caps_by_id={
            "runner-a": {"has_chrome": False},
            "runner-b": {"has_chrome": True},
        },
    )
    _seed_checkout(PROJECT, "runner-a", "/a/x")
    _seed_checkout(PROJECT, "runner-b", "/b/x")
    d = placement.resolve_placement(
        {
            "placement": {"where": "anywhere", "constraints": {"has_chrome": True}},
            "project_v2_id": PROJECT,
        }
    )
    assert d.kind == "runner" and d.runner_id == "runner-b"


def test_capability_where_token_folds_into_constraint(mock_db, monkeypatch):
    _stub_installs(
        monkeypatch,
        ["runner-a", "runner-b"],
        caps_by_id={
            "runner-a": {"has_display": False},
            "runner-b": {"has_display": True},
        },
    )
    _seed_checkout(PROJECT, "runner-a", "/a/x")
    _seed_checkout(PROJECT, "runner-b", "/b/x")
    d = placement.resolve_placement(
        {"placement": {"where": "has_display"}, "project_v2_id": PROJECT}
    )
    assert d.kind == "runner" and d.runner_id == "runner-b"


def test_anywhere_no_capable_runner_yields_none(mock_db, monkeypatch):
    _stub_installs(monkeypatch, ["runner-a"])  # online but no checkout for PROJECT
    d = placement.resolve_placement(
        {"placement": {"where": "anywhere"}, "project_v2_id": PROJECT}
    )
    assert d.kind == "none" and d.runner_id is None
