"""The upload retry destination upgrades existing databases without losing blobs."""

import pytest

from app.db import database
from tests.db.migration_helpers import column_names


@pytest.mark.parametrize("legacy", [True, False])
def test_upload_destination_migration_preserves_blobs_and_is_idempotent(mock_db, legacy):
    conn = mock_db
    if legacy:
        conn.execute("ALTER TABLE upload_blobs DROP COLUMN materialized_path")
    conn.execute(
        "INSERT INTO upload_blobs (id, source_path, filename, size, data) "
        "VALUES (?, ?, ?, ?, ?)",
        ("upload", "/source/photo.png", "photo.png", 3, b"new"),
    )
    if not legacy:
        conn.execute(
            "UPDATE upload_blobs SET materialized_path = ? WHERE id = ?",
            ("/destination/photo_1.png", "upload"),
        )
    conn.commit()

    for _ in range(2):
        database.init_database()
        assert "materialized_path" in column_names(conn, "upload_blobs")
        row = conn.execute(
            "SELECT data, materialized_path FROM upload_blobs WHERE id = 'upload'"
        ).fetchone()
        assert row[0] == b"new"
        assert row[1] == (None if legacy else "/destination/photo_1.png")


def test_upload_destination_migration_bootstraps_missing_table(mock_db):
    conn = mock_db
    conn.execute("DROP TABLE upload_blobs")
    conn.commit()

    database.init_database()
    database.init_database()

    assert "materialized_path" in column_names(conn, "upload_blobs")
