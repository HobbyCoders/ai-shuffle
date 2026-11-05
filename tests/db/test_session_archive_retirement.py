"""Cutover invariants, including the actual fingerprint-gated PostgreSQL boot."""
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import AsyncMock, patch

import pytest

from app.db import database
from app.db.session_archive_migration import canonical_session


def test_legacy_projection_is_pure():
    signed = {"id": "legacy", "status": "archived", "is_active": True, "updated_at": "old"}
    assert canonical_session(signed) == {**signed, "status": "active", "is_active": False}
    assert signed["status"] == "archived"
    assert canonical_session({"status": "failed"}) == {"status": "failed"}
    from app.network import engine, policy
    coll = policy.collection("sessions")
    assert engine.content_hash(coll, signed) == engine.content_hash(coll, canonical_session(signed))
    scanned = engine.serialize_row(coll, signed)
    assert scanned["status"] == "active" and not scanned["is_active"]


@pytest.mark.parametrize("active", [False, True])
def test_legacy_catalog_cannot_override_newer_shared_membership(mock_db, active):
    from app.db.base import get_db
    from app.network.catalog import prepare_session_mirror
    database.create_session("mirror", "default", is_active=active)
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO network_session_active (session_id,is_active,updated_at) VALUES (%s,%s,%s)",
                    ("mirror", active, "2026-11-01"))
        signed = {"id":"mirror", "status":"archived", "is_active":False, "owner_device_id":"owner"}
        projected = prepare_session_mirror(cur, signed, origin_device_id="owner", device_id="viewer")
        assert projected["status"] == "active" and projected["is_active"] is active
        assert signed["status"] == "archived"  # Original signed evidence is untouched.


@pytest.mark.parametrize("archived,active,expected", [(True, True, False), (False, True, True), (None, False, False)])
async def test_legacy_request_uses_ordinary_settlement(archived, active, expected):
    from app.network import session_active
    row = {"id": "legacy"}
    with patch.object(session_active.engine, "context", return_value=object()), patch.object(
        session_active, "set_shared", new_callable=AsyncMock, return_value=row
    ) as shared, patch.object(session_active, "_apply_archive_change", new_callable=AsyncMock) as stop:
        assert await session_active.update_membership(row, active, archived) == row
    shared.assert_awaited_once_with("legacy", expected)
    stop.assert_not_awaited()


def test_direct_status_and_bulk_writers_cannot_persist_archive(mock_db):
    database.create_session("legacy", "default", is_active=True)
    database.update_session("legacy", status="archived")
    row = database.get_session("legacy")
    assert row["status"] == "active" and not row["is_active"]
    from app.db.sessions import set_sessions_active
    set_sessions_active(["legacy"], True, archived=True)
    row = database.get_session("legacy")
    assert row["status"] == "active" and not row["is_active"]


def test_actual_postgres_boot_migration(tmp_path):
    from tests.db.schema_lifecycle_helpers import private_postgres, pg16_bindir
    bindir = pg16_bindir()
    assert bindir, "PostgreSQL 16 is required for this migration test"
    data = tmp_path / "data"
    home = tmp_path / "home"
    home.mkdir()
    with private_postgres(bindir, data):
        env = {key: os.environ[key] for key in ("PATH", "LANG") if key in os.environ}
        env.update(LOCAL_MODE="true", DATA_DIR=str(data), HOME=str(home),
                   WORKSPACE_DIR=str(tmp_path / "workspace"), PYTHONPATH=str(Path.cwd() / "fluid"))
        code = r'''import asyncio
from app.db import database
from app.db.base import get_db, init_pool
from app.db.session_archive_migration import reconcile

init_pool()
database.init_database()
with get_db() as conn:
    c = conn.cursor()
    # 1000 history rows, including local/mirrored, timestamp ties and hidden identities.
    for i in range(1000):
        c.execute("INSERT INTO sessions (id,title,status,is_active,created_at,updated_at,hidden,is_worker,owner_device_id) "
                  "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                  (f"legacy-{i:04}", "History", "archived", True, "2020-01-01", "2020-01-02", i%10==0, i%11==0,
                   "old-peer" if i%2 else None))
    for status in ("active", "failed", None):
        c.execute("INSERT INTO sessions (id,status,is_active) VALUES (%s,%s,TRUE)", (str(status),status))
    for wid, sid, source in [("old-wake", "legacy-0000", "schedule_wakeup"),
                             ("old-held", "legacy-0000", "held_peer"),
                             ("notice", "legacy-0000", "profile_cancelled"),
                             ("unrelated", "active", "schedule_wakeup")]:
        c.execute("INSERT INTO pending_wakeups (id,session_id,prompt,fire_at,source) VALUES (%s,%s,%s,%s,%s)",
                  (wid,sid,"retain this evidence",2000000000,source))
    c.execute("DELETE FROM system_settings WHERE key=%s", (database.MIGRATIONS_FINGERPRINT_KEY,))
# Real boot path forces the migration once, then fingerprint gates the second pass.
database.init_database()
with get_db() as conn:
    c=conn.cursor()
    c.execute("SELECT COUNT(*) AS n FROM sessions WHERE id LIKE 'legacy-%%' AND status='active' AND NOT is_active AND updated_at='2020-01-02'")
    assert c.fetchone()["n"] == 1000
    c.execute("SELECT id FROM pending_wakeups ORDER BY id")
    assert [r["id"] for r in c.fetchall()] == ["notice", "unrelated"]
    c.execute("SELECT cancelled_wakes FROM session_archive_retirement WHERE session_id='legacy-0000'")
    import json
    assert {r["id"] for r in json.loads(c.fetchone()["cancelled_wakes"])} == {"old-wake", "old-held"}
    c.execute("SELECT SUM(local_revision) AS n FROM network_session_active")
    before=c.fetchone()["n"]
# Deliberately restart the async handoff after conversion; the marker persisted.
asyncio.run(reconcile())
database.init_database()
asyncio.run(reconcile())
with get_db() as conn:
    c=conn.cursor()
    c.execute("SELECT COUNT(*) AS n FROM sessions")
    assert c.fetchone()["n"] == 1003
    c.execute("SELECT SUM(local_revision) AS n FROM network_session_active")
    assert c.fetchone()["n"] == before
    c.execute("SELECT COUNT(*) AS n FROM session_archive_retirement WHERE NOT done")
    assert c.fetchone()["n"] == 0
    c.execute("SELECT status FROM sessions WHERE id='failed'")
    assert c.fetchone()["status"] == "failed"
print("REAL_POSTGRES_BOOT: 1000 converted, timestamps retained, no deletions, second boot stable")
'''
        result = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=50)
        assert result.returncode == 0, result.stderr[-4000:]
        assert "REAL_POSTGRES_BOOT" in result.stdout
