from datetime import datetime, timedelta, timezone

from app.db import database


def _grant(token_hash: str, *, expires_delta: timedelta = timedelta(minutes=5)):
    return database.create_pairing_grant(
        token_hash=token_hash,
        purpose="controller",
        scopes=["controller", "web:session"],
        target_install_id="runner-a",
        endpoint_url="https://shuffle.example",
        device_label=None,
        created_by="test",
        expires_at=datetime.now(timezone.utc) + expires_delta,
    )


def test_browser_exchange_is_atomic_and_stores_only_hashes(test_db):
    _grant("grant-hash")
    expires = datetime.now(timezone.utc) + timedelta(days=30)

    result = database.exchange_grant_for_client(
        token_hash="grant-hash",
        device_id="device-browser-123",
        device_name="Test Browser",
        platform="linux-browser",
        client_type="browser",
        session_token_hash="session-hash",
        session_expires_at=expires,
    )

    assert result is not None
    assert result["client"]["device_name"] == "Test Browser"
    assert result["client"]["scopes"] == ["controller"]
    assert database.get_paired_client_by_session_hash("session-hash") is not None
    assert database.exchange_grant_for_client(
        token_hash="grant-hash",
        device_id="another-device",
        device_name="Second Browser",
        platform="web",
        client_type="browser",
        session_token_hash="second-session-hash",
        session_expires_at=expires,
    ) is None

    row = test_db.execute(
        "SELECT token_hash, consumed_at FROM pairing_grants WHERE token_hash = ?",
        ("grant-hash",),
    ).fetchone()
    assert row["token_hash"] == "grant-hash"
    assert row["consumed_at"] is not None


def test_expired_or_revoked_grants_cannot_be_consumed():
    # A full day avoids the SQLite shim's space-vs-T timestamp lexical edge;
    # production Postgres compares these as real timestamptz values.
    expired = _grant("expired-hash", expires_delta=timedelta(days=-1))
    _grant("revoked-hash")
    assert database.revoke_pairing_grant(
        next(grant["id"] for grant in database.list_pairing_grants() if grant["token_hash"] == "revoked-hash")
    )

    future = datetime.now(timezone.utc) + timedelta(days=1)
    for token_hash in ("expired-hash", "revoked-hash"):
        assert database.exchange_grant_for_client(
            token_hash=token_hash,
            device_id=f"device-{token_hash}",
            device_name="Blocked",
            platform="web",
            client_type="browser",
            session_token_hash=f"session-{token_hash}",
            session_expires_at=future,
        ) is None
    assert expired["consumed_at"] is None


def test_revoking_client_invalidates_session_and_linked_mobile():
    _grant("mobile-grant")
    result = database.exchange_grant_for_mobile(
        token_hash="mobile-grant",
        device_id="phone-12345678",
        device_name="Phone",
        platform="android",
        capabilities={"supports_push": True},
        api_key_hash="mobile-key-hash",
        api_key_expires_at=datetime.now(timezone.utc) + timedelta(days=365),
    )
    assert result is not None
    client = result["client"]
    session_expiry = datetime.now(timezone.utc) + timedelta(days=7)
    assert database.set_paired_client_session(client["id"], "web-hash", session_expiry)
    assert database.get_paired_client_by_session_hash("web-hash") is not None

    assert database.revoke_paired_client(client["id"])
    assert database.get_paired_client_by_session_hash("web-hash") is None
    mobile = database.get_mobile_device("phone-12345678")
    assert mobile is not None
    assert mobile["revoked_at"] is not None


def test_runner_grant_approves_only_the_pending_install():
    database.register_install(
        install_id="new-runner",
        device_name="New runner",
        base_url=None,
        platform="test",
        version="1",
        capabilities={},
        approve_new=False,
    )
    database.create_pairing_grant(
        token_hash="runner-grant-hash",
        purpose="runner",
        scopes=["runner:enroll"],
        target_install_id="admin-runner",
        endpoint_url=None,
        device_label=None,
        created_by="test",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )

    grant = database.consume_grant_for_runner(
        token_hash="runner-grant-hash",
        install_id="new-runner",
    )
    assert grant is not None
    assert database.is_install_approved("new-runner") is True
    assert database.consume_grant_for_runner(
        token_hash="runner-grant-hash",
        install_id="new-runner",
    ) is None


def test_touch_paired_client_only_writes_when_last_seen_is_stale(test_db):
    from app.db.base import get_db

    _grant("touch-grant")
    result = database.exchange_grant_for_client(
        token_hash="touch-grant",
        device_id="device-touch",
        device_name="Touch Browser",
        platform="linux-browser",
        client_type="browser",
        session_token_hash="touch-session",
        session_expires_at=datetime.now(timezone.utc) + timedelta(days=30),
    )
    client_id = result["client"]["id"]

    database.touch_paired_client(client_id)
    fresh = database.get_paired_client(client_id)["last_seen"]
    assert fresh is not None

    # A second request inside the interval must not rewrite the row.
    database.touch_paired_client(client_id)
    assert database.get_paired_client(client_id)["last_seen"] == fresh

    # Once the stored value ages past the interval the write happens again.
    stale = datetime.now(timezone.utc) - timedelta(hours=1)
    with get_db() as conn:
        conn.cursor().execute(
            "UPDATE paired_clients SET last_seen = %s WHERE id = %s", (stale, client_id)
        )
    database.touch_paired_client(client_id)
    assert database.get_paired_client(client_id)["last_seen"] != fresh
