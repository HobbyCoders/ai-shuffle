"""Pairing grants and paired-client sessions: the refusal and rotation paths.

Complements tests/db/test_pairing.py (happy paths). Runs on the per-test SQLite
shim; hashes are opaque strings, as the API hashes before calling in.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.db import mobile as mobile_db
from app.db import pairing
from app.db.base import get_db


def _now():
    return datetime.now(timezone.utc)


def _grant(token_hash, *, purpose="controller", scopes=None, target=None,
           expires=timedelta(minutes=5), max_attempts=10):
    return pairing.create_pairing_grant(
        token_hash=token_hash, purpose=purpose,
        scopes=scopes if scopes is not None else ["controller", "web:session"],
        target_install_id=target, endpoint_url=None, device_label=None,
        created_by="test", expires_at=_now() + expires, max_attempts=max_attempts,
    )


def _browser(token_hash, device_id="browser-1", session="sess-hash", target=None):
    return pairing.exchange_grant_for_client(
        token_hash=token_hash, device_id=device_id, device_name="Browser",
        platform="web", client_type="browser", session_token_hash=session,
        session_expires_at=_now() + timedelta(days=7), target_install_id=target,
    )


def _phone(token_hash, device_id="phone-1", key="key-hash", key_expiry=timedelta(days=90), **kw):
    return pairing.exchange_grant_for_mobile(
        token_hash=token_hash, device_id=device_id, device_name="Phone",
        platform="android", capabilities={}, api_key_hash=key,
        api_key_expires_at=_now() + key_expiry, **kw,
    )


def _grant_row(token_hash):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM pairing_grants WHERE token_hash=%s", (token_hash,))
        return dict(cur.fetchone())


def _paired_phone(grant="g-phone", device_id="phone-1", **kw):
    _grant(grant)
    result = _phone(grant, device_id=device_id, **kw)
    return result["client"], mobile_db.get_mobile_device(device_id)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    ('["a", 1, "b"]', ["a", "b"]),
    ("{not json", []),
    ('{"a": 1}', []),
    (None, []),
    (["x", None], ["x"]),
])
def test_json_list(raw, expected):
    assert pairing._json_list(raw) == expected


def test_row_helpers_on_missing_rows():
    assert pairing._grant(None) is None and pairing._client(None) is None


# ---------------------------------------------------------------------------
# Grants
# ---------------------------------------------------------------------------


def test_list_grants_active_vs_all(mock_db):
    _grant("live")
    _grant("used")
    assert _browser("used") is not None
    active = {g["token_hash"] for g in pairing.list_pairing_grants()}
    everything = {g["token_hash"] for g in pairing.list_pairing_grants(active_only=False)}
    assert active == {"live"}
    assert everything == {"live", "used"}
    assert all(isinstance(g["scopes"], list) for g in pairing.list_pairing_grants(active_only=False))


def test_revoke_grant_twice(mock_db):
    grant = _grant("g")
    assert pairing.revoke_pairing_grant(grant["id"]) is True
    assert pairing.revoke_pairing_grant(grant["id"]) is False
    assert pairing.revoke_pairing_grant("pgr_unknown") is False


def test_failed_attempts_on_a_dead_grant_are_counted_and_capped(mock_db):
    _grant("dead", expires=timedelta(days=-1), max_attempts=2)
    for i in range(4):
        assert _browser("dead", device_id=f"b{i}", session=f"s{i}") is None
    assert _grant_row("dead")["attempt_count"] == 2
    # A random guess has no row to count against and changes nothing.
    assert _browser("never-issued") is None


def test_wrong_target_install_does_not_consume(mock_db):
    _grant("targeted", target="runner-a")
    assert _browser("targeted", target="runner-b") is None
    row = _grant_row("targeted")
    assert row["consumed_at"] is None and row["attempt_count"] == 0
    assert _browser("targeted", target="runner-a") is not None


def test_wrong_purpose_cannot_be_redeemed(mock_db):
    _grant("join", purpose="network:join")
    assert _browser("join") is None
    assert pairing.consume_grant_for_network_join(token_hash="join")["purpose"] == "network:join"
    assert pairing.consume_grant_for_network_join(token_hash="join") is None


def test_runner_grant_for_unknown_install(mock_db):
    _grant("runner", purpose="runner", scopes=["runner:enroll"])
    assert pairing.consume_grant_for_runner(token_hash="runner", install_id="nope") is None
    assert _grant_row("runner")["consumed_at"] is None


# ---------------------------------------------------------------------------
# Browser / mobile exchange refusals
# ---------------------------------------------------------------------------


def test_browser_exchange_refuses_a_retained_account_identity(mock_db):
    _grant("g")
    with get_db() as conn:
        conn.cursor().execute(
            "INSERT INTO mobile_hub_authorizations (device_id, paired_client_id, account_id, hub_device_id, server_id) "
            "VALUES (%s, %s, %s, %s, %s)",
            ("browser-1", "pcl_x", "acct", "hub-dev", "srv"),
        )
    assert _browser("g") is None
    assert _grant_row("g")["consumed_at"] is None


def test_browser_exchange_keeps_only_the_controller_scope(mock_db):
    _grant("g", scopes=["mobile:actions", "web:session"])
    assert _browser("g")["client"]["scopes"] == ["controller"]


def test_mobile_duplicate_does_not_consume_the_grant(mock_db):
    _paired_phone("first")
    _grant("second")
    assert _phone("second") == {"duplicate": True}
    assert _grant_row("second")["consumed_at"] is None


def test_mobile_exchange_requires_managed_repair_for_account_devices(mock_db):
    _grant("g")
    with get_db() as conn:
        conn.cursor().execute(
            "INSERT INTO mobile_worker_authorizations (device_id, paired_client_id, account_id, hub_device_id, server_id) "
            "VALUES (%s, %s, %s, %s, %s)",
            ("phone-1", "pcl_owner", "acct", "hub-dev", "srv"),
        )
    assert _phone("g") == {"managed_repair_required": True}
    owner = {"device_id": "phone-1", "paired_client_id": "pcl_owner", "account_id": "someone-else",
             "hub_device_id": "hub-dev", "server_id": "srv"}
    assert _phone("g", managed_repair={"owner": owner, "instance_id": "i"}) == {"repair_invalid": True}
    # Matching owner fields, but the device id is not the one derived from the hub identity.
    owner["account_id"] = "acct"
    assert _phone("g", managed_repair={"owner": owner, "instance_id": "i"}) == {"repair_invalid": True}
    assert _grant_row("g")["consumed_at"] is None


def test_managed_repair_without_owner_row_is_invalid(mock_db):
    _grant("g")
    owner = {"device_id": "phone-1"}
    assert _phone("g", managed_repair={"owner": owner, "instance_id": "i"}) == {"repair_invalid": True}


def test_mobile_exchange_default_scopes_when_grant_has_none(mock_db):
    _grant("g", scopes=[])
    result = _phone("g")
    assert set(result["client"]["scopes"]) == {"controller", "mobile:notifications", "mobile:actions", "web:session"}
    assert mobile_db.get_mobile_device("phone-1")["paired_client_id"] == result["client"]["id"]


# ---------------------------------------------------------------------------
# Paired clients
# ---------------------------------------------------------------------------


def test_list_rename_and_revoked_clients(mock_db):
    client, _device = _paired_phone()
    renamed = pairing.rename_paired_client(client["id"], "Work phone")
    assert renamed["device_name"] == "Work phone"
    assert mobile_db.get_mobile_device("phone-1")["device_name"] == "Work phone"
    assert pairing.rename_paired_client("pcl_missing", "x") is None

    assert pairing.revoke_paired_client(client["id"]) is True
    assert pairing.revoke_paired_client(client["id"]) is False
    assert pairing.rename_paired_client(client["id"], "again") is None
    assert pairing.list_paired_clients() == []
    assert [c["id"] for c in pairing.list_paired_clients(include_revoked=True)] == [client["id"]]


def test_ensure_paired_client_for_legacy_mobile(mock_db):
    with get_db() as conn:
        conn.cursor().execute(
            "INSERT INTO mobile_devices (id, device_id, device_name, platform, api_key_hash, scopes) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            ("legacy1", "legacy-phone", "Old phone", "android", "legacy-key", '["controller", "mobile:actions"]'),
        )
    device = mobile_db.get_mobile_device("legacy-phone")
    # Presenting a stale key hash does not mint a controller row.
    assert pairing.ensure_paired_client_for_mobile(dict(device, api_key_hash="other")) is None

    client = pairing.ensure_paired_client_for_mobile(device)
    assert client["client_type"] == "mobile" and client["scopes"] == ["controller", "mobile:actions"]
    linked = mobile_db.get_mobile_device("legacy-phone")
    assert linked["paired_client_id"] == client["id"]
    # Once linked, the existing row is returned as-is.
    assert pairing.ensure_paired_client_for_mobile(linked)["id"] == client["id"]


def test_ensure_paired_client_does_not_resurrect_a_revoked_client(mock_db):
    client, device = _paired_phone()
    pairing.revoke_paired_client(client["id"])
    with get_db() as conn:
        conn.cursor().execute("UPDATE mobile_devices SET revoked_at=NULL WHERE device_id=%s", ("phone-1",))
    stale = dict(mobile_db.get_mobile_device("phone-1"), paired_client_id=None)
    assert pairing.ensure_paired_client_for_mobile(stale) is None


# ---------------------------------------------------------------------------
# Sessions and web reservations
# ---------------------------------------------------------------------------


def test_set_session_argument_guards(mock_db):
    with pytest.raises(ValueError, match="Unknown session purpose"):
        pairing.set_paired_client_session("c", "t", _now(), purpose="api")
    with pytest.raises(ValueError, match="Web reservation"):
        pairing.set_paired_client_session("c", "t", _now(), purpose="native", web_reservation_hash="r")
    with pytest.raises(ValueError, match="Web reservation"):
        pairing.set_paired_client_session("c", "t", _now(), web_reservation_hash="r")


def test_native_session_round_trip(mock_db):
    client, _device = _paired_phone()
    expiry = _now() + timedelta(days=1)
    assert pairing.set_paired_client_session(client["id"], "native-1", expiry, purpose="native")["id"] == client["id"]
    assert pairing.get_paired_client_by_session_hash("native-1")["id"] == client["id"]
    # Rotation replaces the token.
    pairing.set_paired_client_session(client["id"], "native-2", expiry, purpose="native")
    assert pairing.get_paired_client_by_session_hash("native-1") is None
    assert pairing.set_paired_client_session("pcl_missing", "n", expiry, purpose="native") is None

    assert pairing.clear_paired_client_session(client["id"], "wrong-token") is False
    assert pairing.clear_paired_client_session(client["id"], "native-2") is True
    assert pairing.get_paired_client_by_session_hash("native-2") is None


def test_web_session_clear_paths(mock_db):
    _grant("g")
    client = _browser("g", session="web-1")["client"]
    assert pairing.clear_paired_client_session("pcl_missing") is False
    assert pairing.clear_paired_client_session(client["id"], "not-current") is False
    assert pairing.get_paired_client_by_session_hash("web-1") is not None
    assert pairing.clear_paired_client_session(client["id"]) is True
    assert pairing.get_paired_client_by_session_hash("web-1") is None
    assert pairing.set_paired_client_session("pcl_missing", "x", _now() + timedelta(days=1)) is None


def test_web_reservation_is_single_use(mock_db):
    client, device = _paired_phone()
    expiry = _now() + timedelta(minutes=5)
    assert pairing.reserve_paired_client_web_session(client["id"], "res-1", expiry, mobile_device=device)
    # A newer reservation replaces the pending one; the old hash is dead.
    assert pairing.reserve_paired_client_web_session(client["id"], "res-2", expiry, mobile_device=device)
    with pytest.raises(pairing.WebSessionReservationConflict):
        pairing.set_paired_client_session(client["id"], "web-a", _now() + timedelta(days=1),
                                          mobile_device=device, web_reservation_hash="res-1")
    rotated = pairing.set_paired_client_session(client["id"], "web-b", _now() + timedelta(days=1),
                                                mobile_device=device, web_reservation_hash="res-2")
    assert rotated["session_token_hash"] == "web-b"
    with pytest.raises(pairing.WebSessionReservationConflict):
        pairing.set_paired_client_session(client["id"], "web-c", _now() + timedelta(days=1),
                                          mobile_device=device, web_reservation_hash="res-2")


def test_web_reservation_requires_the_current_mobile_credential(mock_db):
    client, device = _paired_phone()
    expiry = _now() + timedelta(minutes=5)
    stale_key = dict(device, api_key_hash="rotated-away")
    assert pairing.reserve_paired_client_web_session(client["id"], "r", expiry, mobile_device=stale_key) is False
    assert pairing.set_paired_client_session(client["id"], "w", expiry, mobile_device=stale_key) is None
    assert pairing.reserve_paired_client_web_session("pcl_missing", "r", expiry, mobile_device=device) is False


def test_web_reservation_refused_for_expired_key_or_missing_scope(mock_db):
    client, device = _paired_phone(key_expiry=timedelta(days=-1))
    expiry = _now() + timedelta(minutes=5)
    assert pairing.reserve_paired_client_web_session(client["id"], "r", expiry, mobile_device=device) is False

    client2, device2 = _paired_phone("g2", device_id="phone-2", key="key-2")
    with get_db() as conn:
        conn.cursor().execute("UPDATE mobile_devices SET scopes=%s WHERE device_id=%s",
                              ('["controller", "mobile:actions"]', "phone-2"))
    assert pairing.reserve_paired_client_web_session(client2["id"], "r2", expiry, mobile_device=device2) is False
