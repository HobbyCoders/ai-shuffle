"""Edge cases for which API user a session or a turn runs as (app.core.api_user_scope).

``None`` means the owner, the most privileged principal, so every walk that
cannot settle must stop (never loop) and the owner-socket exemption must only
honour the owner's own chat socket stamps.
"""

from __future__ import annotations

import pytest

from app.core import api_user_scope
from app.core.api_user_scope import session_api_user_id, turn_api_user_id
from app.db import database


class _Rows:
    """An in-memory ``sessions`` table standing in for ``database.get_session``."""

    def __init__(self, rows):
        self.rows = {r["id"]: r for r in rows}
        self.calls = 0

    def get(self, session_id):
        self.calls += 1
        return self.rows.get(session_id)


@pytest.fixture
def rows(monkeypatch):
    def install(*items):
        table = _Rows(items)
        monkeypatch.setattr(database, "get_session", table.get)
        monkeypatch.setattr(database, "get_session_by_sdk_id", lambda sid: None)
        return table

    return install


def _worker(sid, parent, api_user_id=None):
    return {"id": sid, "is_worker": True, "parent_session_id": parent, "api_user_id": api_user_id}


def _session(sid, api_user_id=None):
    return {"id": sid, "is_worker": False, "parent_session_id": None, "api_user_id": api_user_id}


# ── session_api_user_id ──────────────────────────────────────────────────────


def test_no_row_is_the_owner(rows):
    rows()
    assert session_api_user_id(None) is None
    assert session_api_user_id({}) is None


def test_own_column_wins_without_a_lookup(rows):
    table = rows()
    assert session_api_user_id(_worker("w", "p", api_user_id="key-w")) == "key-w"
    assert table.calls == 0


def test_non_worker_with_a_parent_is_not_walked(rows):
    table = rows(_session("p", "key-p"))
    row = {"id": "f", "is_worker": False, "parent_session_id": "p", "api_user_id": None}
    assert session_api_user_id(row) is None  # a fork belongs to whoever forked it
    assert table.calls == 0


def test_worker_without_a_parent_id_is_the_owner(rows):
    rows()
    assert session_api_user_id(_worker("w", None)) is None
    assert session_api_user_id(_worker("w", "")) is None


def test_worker_inherits_its_parents_key(rows):
    rows(_session("p", "key-p"), _worker("w1", "p"))
    assert session_api_user_id(_worker("w2", "w1")) == "key-p"


def test_worker_of_another_keys_session_is_that_keys(rows):
    # The worker itself carries no key; it belongs to whoever owns its parent,
    # not to whichever key happens to be asking.
    rows(_session("p", "key-other"))
    assert session_api_user_id(_worker("w", "p")) == "key-other"


def test_worker_with_its_own_key_is_not_overridden_by_the_parent(rows):
    rows(_session("p", "key-parent"))
    assert session_api_user_id(_worker("w", "p", api_user_id="key-own")) == "key-own"


def test_empty_string_key_is_treated_as_unset_and_walked(rows):
    rows(_session("p", "key-p"))
    assert session_api_user_id(_worker("w", "p", api_user_id="")) == "key-p"


def test_missing_parent_row_is_the_owner(rows):
    rows()
    assert session_api_user_id(_worker("w", "deleted")) is None


def test_cycle_terminates(rows):
    table = rows(_worker("a", "b"), _worker("b", "a"))
    assert session_api_user_id(table.rows["a"]) is None
    assert table.calls <= api_user_scope._OWNER_DEPTH


def test_self_parent_terminates(rows):
    table = rows(_worker("a", "a"))
    assert session_api_user_id(table.rows["a"]) is None
    assert table.calls <= api_user_scope._OWNER_DEPTH


def _chain(depth, root_key):
    """w0 -> w1 -> ... -> w{depth-1} -> root (with ``root_key``)."""
    items = [_session("root", root_key)]
    for i in range(depth):
        parent = f"w{i + 1}" if i + 1 < depth else "root"
        items.append(_worker(f"w{i}", parent))
    return items


def test_chain_within_the_cap_resolves(rows):
    depth = api_user_scope._OWNER_DEPTH - 1
    table = rows(*_chain(depth, "key-root"))
    assert session_api_user_id(table.rows["w0"]) == "key-root"


def test_chain_past_the_cap_stops_after_the_cap(rows):
    # Same bound as api_user_owns_session, which refuses the key access to
    # such a session; the walk never runs unbounded.
    table = rows(*_chain(50, "key-root"))
    assert session_api_user_id(table.rows["w0"]) is None
    assert table.calls == api_user_scope._OWNER_DEPTH


def test_viewer_api_user_id_blank_and_unknown(rows):
    rows(_session("s", "key-s"))
    assert api_user_scope.viewer_api_user_id(None) is None
    assert api_user_scope.viewer_api_user_id("") is None
    assert api_user_scope.viewer_api_user_id("nope") is None
    assert api_user_scope.viewer_api_user_id("s") == "key-s"


# ── turn_api_user_id ─────────────────────────────────────────────────────────


KEY_SESSION = _session("k", "key-k")
OWNER_SESSION = _session("o", None)

# Every other submit path: wakes, continuations, connectors, workers,
# automations and the API. None may carry the owner past a key's session.
NON_SOCKET_SOURCES = [
    "api", "worktree", "connector", "wakeup", "auto_continue", "resume",
    "promotion", "agent_message", "unpark", "unpark_pump", "automation",
    "automation_step", "idle_watchdog", "voice", "task", "USER", "Queue",
]


def test_explicit_payload_key_wins(rows):
    rows()
    assert turn_api_user_id({"api_user_id": "key-x"}, "wakeup", OWNER_SESSION) == "key-x"
    assert turn_api_user_id({"api_user_id": "key-x", "user_id": "admin"}, "user", KEY_SESSION) == "key-x"


def test_owner_session_runs_as_owner_whatever_the_payload(rows):
    rows()
    for payload in (None, {}, {"user_id": "someone"}, {"user_id": 7}):
        assert turn_api_user_id(payload, "wakeup", OWNER_SESSION) is None
    assert turn_api_user_id({}, "user", None) is None


@pytest.mark.parametrize("source", ["user", "queue", None, ""])
@pytest.mark.parametrize("user_id", ["admin", "paired:dev-1", "paired:"])
def test_owner_socket_stamp_on_a_key_session_runs_as_owner(rows, source, user_id):
    rows()
    assert turn_api_user_id({"user_id": user_id}, source, KEY_SESSION) is None


@pytest.mark.parametrize("source", NON_SOCKET_SOURCES)
@pytest.mark.parametrize("user_id", ["admin", "paired:dev-1"])
def test_owner_stamp_on_any_other_source_runs_as_the_key(rows, source, user_id):
    rows()
    assert turn_api_user_id({"user_id": user_id}, source, KEY_SESSION) == "key-k"


@pytest.mark.parametrize(
    "user_id",
    ["key-k", "Admin", "ADMIN", " admin", "admin ", "paired", "Paired:x", "xpaired:x", "", None],
)
def test_other_string_stamps_run_as_the_key(rows, user_id):
    rows()
    assert turn_api_user_id({"user_id": user_id}, "user", KEY_SESSION) == "key-k"


@pytest.mark.parametrize("user_id", [1, 0, True, ["admin"], {"admin": 1}, ("paired:x",), b"admin"])
def test_non_string_stamps_run_as_the_key(rows, user_id):
    rows()
    assert turn_api_user_id({"user_id": user_id}, "user", KEY_SESSION) == "key-k"


def test_no_payload_on_a_key_session_runs_as_the_key(rows):
    rows()
    assert turn_api_user_id(None, "user", KEY_SESSION) == "key-k"
    assert turn_api_user_id({}, None, KEY_SESSION) == "key-k"


def test_turn_on_a_legacy_worker_of_a_key_session_runs_as_the_key(rows):
    table = rows(_session("p", "key-p"), _worker("w", "p"))
    assert turn_api_user_id({"user_id": "admin"}, "wakeup", table.rows["w"]) == "key-p"
    assert turn_api_user_id({"user_id": "admin"}, "user", table.rows["w"]) is None
