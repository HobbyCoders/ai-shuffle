"""Local, fail-closed provenance for branches retained by a linked placeholder.

Not replicated: these links authorize access to this worker's physical checkouts.
A pending link is created only with a brand-new local identity, its first target
is immutable, and deletion or legacy-key reuse permanently revokes it. Sessions
continue to use their recorded identity independently of this catalog proof.
"""
from app.db.base import get_db


def create_schema(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS project_catalog_aliases (
            original_id TEXT PRIMARY KEY REFERENCES projects_v2(id) ON DELETE CASCADE,
            legacy_id TEXT NOT NULL,
            replacement_id TEXT REFERENCES projects_v2(id) ON DELETE CASCADE,
            revoked BOOLEAN NOT NULL DEFAULT FALSE
        )
    """)


def register_placeholder(cursor, original_id, legacy_id):
    cursor.execute(
        "INSERT INTO project_catalog_aliases (original_id, legacy_id) VALUES (%s, %s) "
        "ON CONFLICT DO NOTHING", (original_id, legacy_id),
    )


def revoke(cursor, legacy_id):
    # No FK to projects: this tombstone must survive deletion and ID reuse.
    cursor.execute(
        "INSERT INTO project_catalog_aliases (original_id, legacy_id, revoked) "
        "SELECT id, %s, TRUE FROM projects_v2 WHERE origin = %s "
        "ON CONFLICT (original_id) DO UPDATE SET revoked = TRUE",
        (legacy_id, "local:" + str(legacy_id)),
    )


def record_first_link(cursor, legacy_id, replacement_id):
    cursor.execute(
        "UPDATE project_catalog_aliases SET replacement_id = %s "
        "WHERE legacy_id = %s AND revoked = FALSE AND replacement_id IS NULL",
        (replacement_id, legacy_id),
    )


_LIVE_LINK = (
    "FROM project_catalog_aliases alias "
    "JOIN projects legacy ON legacy.id = alias.legacy_id "
    "JOIN projects_v2 replacement ON replacement.id = alias.replacement_id "
    "WHERE alias.revoked = FALSE AND legacy.link_group_id = replacement.origin "
)


def replacement_for(original_id):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT alias.replacement_id " + _LIVE_LINK + "AND alias.original_id = %s",
            (original_id,),
        )
        row = cursor.fetchone()
        return row["replacement_id"] if row else None


def originals_for(replacement_id):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT alias.original_id " + _LIVE_LINK +
            "AND alias.replacement_id = %s ORDER BY alias.original_id",
            (replacement_id,),
        )
        return [row["original_id"] for row in cursor.fetchall()]
