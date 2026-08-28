"""Short-TTL memo for hot-path system settings (finding B8).

``_share_sessions_across_installs`` reads ``cluster_share_sessions`` on EVERY
``get_sessions*`` / ``search_sessions*`` call — i.e. on every sidebar refresh —
and each read was a dedicated pool checkout + SELECT + commit. The memo removes
that per-request cost without making a toggle take effect late: every write
through ``app.db.settings`` invalidates the key immediately, and the TTL bounds
how long ANOTHER install's write to the shared database can go unnoticed.
"""

from __future__ import annotations

from app.db import settings as settings_db
from app.db import database


def setup_function(_func):
    settings_db.invalidate_system_setting_memo()


def teardown_function(_func):
    settings_db.invalidate_system_setting_memo()


def _count_reads(monkeypatch):
    calls = {"n": 0}
    real = settings_db.get_system_setting

    def counting(key):
        calls["n"] += 1
        return real(key)

    monkeypatch.setattr(settings_db, "get_system_setting", counting)
    return calls


def test_memo_serves_repeat_reads_without_hitting_the_db(mock_db, monkeypatch):
    settings_db.set_system_setting("cluster_share_sessions", "off")
    calls = _count_reads(monkeypatch)

    assert settings_db.get_system_setting_cached("cluster_share_sessions") == "off"
    assert calls["n"] == 1
    for _ in range(10):
        assert settings_db.get_system_setting_cached("cluster_share_sessions") == "off"
    assert calls["n"] == 1, "memoized read still queried the database"


def test_misses_are_memoized_too(mock_db, monkeypatch):
    """An UNSET setting is the common case (the default is 'shared'), so it must
    not cost a query per request either."""
    calls = _count_reads(monkeypatch)

    assert settings_db.get_system_setting_cached("never-set-key") is None
    assert settings_db.get_system_setting_cached("never-set-key") is None
    assert calls["n"] == 1


def test_write_invalidates_immediately(mock_db):
    settings_db.set_system_setting("cluster_share_sessions", "off")
    assert settings_db.get_system_setting_cached("cluster_share_sessions") == "off"

    settings_db.set_system_setting("cluster_share_sessions", "on")
    assert settings_db.get_system_setting_cached("cluster_share_sessions") == "on"


def test_delete_invalidates_immediately(mock_db):
    settings_db.set_system_setting("cluster_share_sessions", "off")
    assert settings_db.get_system_setting_cached("cluster_share_sessions") == "off"

    settings_db.delete_system_setting("cluster_share_sessions")
    assert settings_db.get_system_setting_cached("cluster_share_sessions") is None


def test_get_or_create_invalidates(mock_db):
    assert settings_db.get_system_setting_cached("seeded-key") is None
    settings_db.get_or_create_system_setting("seeded-key", lambda: "seed")
    assert settings_db.get_system_setting_cached("seeded-key") == "seed"


def test_ttl_expiry_re_reads(mock_db, monkeypatch):
    """A zero TTL always re-reads — the knob that bounds how stale another
    install's write can be."""
    settings_db.set_system_setting("cluster_share_sessions", "off")
    calls = _count_reads(monkeypatch)

    settings_db.get_system_setting_cached("cluster_share_sessions", ttl=0)
    settings_db.get_system_setting_cached("cluster_share_sessions", ttl=0)
    assert calls["n"] == 2


def test_explicit_invalidation_of_one_key_leaves_others(mock_db, monkeypatch):
    settings_db.set_system_setting("a", "1")
    settings_db.set_system_setting("b", "2")
    settings_db.get_system_setting_cached("a")
    settings_db.get_system_setting_cached("b")

    settings_db.invalidate_system_setting_memo("a")
    calls = _count_reads(monkeypatch)
    settings_db.get_system_setting_cached("a")
    settings_db.get_system_setting_cached("b")
    assert calls["n"] == 1  # only "a" was re-read


# ---------------------------------------------------------------------------
# The behaviour the memo exists for: the sessions-list instance filter
# ---------------------------------------------------------------------------

def test_session_list_filter_reads_through_the_memo(mock_db, monkeypatch):
    """The list path resolves cluster sharing once per TTL, not once per call,
    and still reflects a toggle made through the settings module at once."""
    from app.db import sessions as sessions_db

    calls = _count_reads(monkeypatch)
    for _ in range(5):
        database.get_sessions()
    assert calls["n"] <= 1

    # Toggling isolation ON takes effect immediately (no TTL wait).
    settings_db.set_system_setting("cluster_share_sessions", "off")
    assert sessions_db._share_sessions_across_installs() is False
    settings_db.set_system_setting("cluster_share_sessions", "on")
    assert sessions_db._share_sessions_across_installs() is True


def test_isolation_mode_still_filters_by_instance(mock_db):
    """Correctness guard: the memo must not change WHAT the filter does."""
    from app.core.config import settings as app_settings

    mine = database.create_session(session_id="s-mine", profile_id="default")
    assert mine["instance_id"] == app_settings.effective_instance_id

    mock_db.execute(
        "INSERT INTO sessions (id, profile_id, instance_id) VALUES (?, ?, ?)",
        ("s-other-install", "default", "some-other-install"),
    )
    mock_db.commit()

    settings_db.set_system_setting("cluster_share_sessions", "on")
    assert {s["id"] for s in database.get_sessions()} == {"s-mine", "s-other-install"}

    settings_db.set_system_setting("cluster_share_sessions", "off")
    assert {s["id"] for s in database.get_sessions()} == {"s-mine"}
