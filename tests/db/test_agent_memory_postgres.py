"""#1305 on disposable PostgreSQL: the startup replay's conditional write.

``upsert_memory_file_if_not_newer`` replaces a live row or a tombstone only when
that row's TIMESTAMPTZ ``updated_at`` is not newer than the replayed file's
mtime, so a row's stamp never moves backwards.
"""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from tests.db.postgres_harness import pg_schema, requires_real_pg  # noqa: F401  (pg_schema: autouse fixture)

pytestmark = requires_real_pg

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def key():
    from app.db import base
    memory_key = f"pg-memory-{uuid4().hex[:12]}"
    yield memory_key
    with base.get_db() as conn:
        conn.cursor().execute("DELETE FROM agent_memory_files WHERE memory_key = %s", (memory_key,))


@pytest.mark.parametrize("deleted", [False, True])
def test_replay_write_replaces_only_a_row_that_is_not_newer(key, deleted):
    from app.db import agent_memory
    newer, older = T0 + timedelta(seconds=120), T0 + timedelta(seconds=60)
    if deleted:
        agent_memory.tombstone_memory_file(key, "f.md", "peer", newer.isoformat())
    else:
        agent_memory.upsert_memory_file(key, "f.md", "peer", "h-peer", "peer", newer.isoformat())

    # An older instant written in another offset sorts later as text; the
    # column compares it as time.
    other_zone = older.astimezone(timezone(timedelta(hours=5)))
    assert not agent_memory.upsert_memory_file_if_not_newer(
        key, "f.md", "late", "h-late", "replay", other_zone.isoformat())
    row = agent_memory.get_memory_file(key, "f.md")
    assert row["deleted"] is deleted and row["updated_at"] == newer

    assert agent_memory.upsert_memory_file_if_not_newer(
        key, "f.md", "same time", "h-same", "replay", newer.isoformat())
    assert agent_memory.upsert_memory_file_if_not_newer(
        key, "g.md", "new file", "h-new", "replay", older.isoformat())
    rows = {row["relative_path"]: row for row in agent_memory.get_memory_files(key)}
    assert rows["f.md"]["content"] == "same time" and rows["f.md"]["deleted"] is False
    assert rows["g.md"]["updated_at"] == older
