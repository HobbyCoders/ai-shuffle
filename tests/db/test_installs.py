"""Unit tests for the install-registry row helpers.

The SQL in ``app/db/installs.py`` is Postgres-only (``make_interval``,
``ON CONFLICT``, server-side ``online`` derivation) and isn't exercised by the
SQLite test harness. What IS worth locking down is the pure row-shaping logic
every read passes through: parsing the ``capabilities`` field (which arrives as
a dict from JSONB but as a string from some drivers) and coercing the derived
``online`` flag. These run with no database.
"""

from app.db.installs import _parse_capabilities, _row_to_install, ONLINE_WINDOW_SECONDS


# ---------------------------------------------------------------------------
# _parse_capabilities — JSONB dict vs. serialized string vs. empty
# ---------------------------------------------------------------------------

def test_parse_capabilities_passes_dict_through():
    caps = {"advertise_url_source": "config", "feature": True}
    assert _parse_capabilities(caps) == caps


def test_parse_capabilities_decodes_json_string():
    assert _parse_capabilities('{"advertise_url_source": "guess"}') == {
        "advertise_url_source": "guess"
    }


def test_parse_capabilities_bad_string_falls_back_to_empty():
    assert _parse_capabilities("not json") == {}


def test_parse_capabilities_none_and_empty_become_empty_dict():
    assert _parse_capabilities(None) == {}
    assert _parse_capabilities("") == {}
    assert _parse_capabilities({}) == {}


# ---------------------------------------------------------------------------
# _row_to_install — None passthrough, online coercion, capabilities parsing
# ---------------------------------------------------------------------------

def test_row_to_install_returns_none_for_missing_row():
    assert _row_to_install(None) is None


def test_row_to_install_coerces_online_and_parses_capabilities():
    row = {
        "id": "box-1",
        "device_name": "Box",
        "base_url": "http://box:1455",
        "platform": "docker",
        "version": "1.2.3",
        "capabilities": '{"advertise_url_source": "config"}',
        "last_seen": "2026-06-25T00:00:00+00:00",
        "online": 1,  # SQLite/driver may hand back an int for the bool expr
    }
    out = _row_to_install(row)
    assert out["online"] is True  # coerced to a real bool
    assert out["capabilities"] == {"advertise_url_source": "config"}
    assert out["id"] == "box-1"
    # The transient ``online`` expression alias is consumed, not left dangling
    # alongside a stale value — it's re-added as the coerced bool above.


def test_row_to_install_defaults_online_false_when_absent():
    row = {"id": "box-2", "device_name": "Box2", "capabilities": None}
    out = _row_to_install(row)
    assert out["online"] is False
    assert out["capabilities"] == {}


def test_online_window_is_a_few_heartbeats():
    # ~3x the ~30s heartbeat so a single missed beat doesn't flap the dot.
    assert ONLINE_WINDOW_SECONDS >= 60
