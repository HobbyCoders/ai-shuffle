"""Tests for the admin ``DELETE /api/v1/cluster/installs/{id}`` handler.

Deleting a stale install must also prune its per-install ``usage_snapshots`` row
so a departed install's usage does not linger in the shared table forever (it was
previously only filtered out at read time). The prune is best-effort and must not
fail the delete.
"""

import pytest
from fastapi import HTTPException

from app.api.cluster import delete_install
from app.db import database as db


@pytest.mark.asyncio
async def test_delete_install_prunes_usage_snapshot(mock_db):
    db.register_install("worker-1", "Worker", "http://w:1", "docker", "1", {})
    db.upsert_usage_snapshot(
        "worker-1", windows=[], available=True, generation=1, fetched_at=1,
    )
    assert any(r["instance_id"] == "worker-1" for r in db.get_all_usage_snapshots())

    result = await delete_install("worker-1", token="admin")

    assert result == {"ok": True, "deleted": "worker-1"}
    # Registry row gone AND the usage snapshot pruned.
    assert "worker-1" not in db.get_installs_map()
    assert db.get_all_usage_snapshots() == []


@pytest.mark.asyncio
async def test_delete_missing_install_404s_and_leaves_usage_untouched(mock_db):
    # An unrelated install's usage row must survive a failed delete of another id.
    db.upsert_usage_snapshot(
        "keep-me", windows=[], available=True, generation=1, fetched_at=1,
    )
    with pytest.raises(HTTPException) as exc:
        await delete_install("ghost", token="admin")
    assert exc.value.status_code == 404
    assert any(r["instance_id"] == "keep-me" for r in db.get_all_usage_snapshots())
