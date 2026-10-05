"""sessions.provenance_owner (#1849): added once, copied from existing records.

The column carries a chat's ``mobile_tool_identity`` owner through the
session catalog. An upgraded database copies the owner of every record it
already holds, so chats started before the upgrade keep their account rule
on the workers that mirror them. Runs the full ``_run_column_migrations``
pass on the SQLite shim, like the other migration tests here.
"""
from app.db import database, mobile_tool_identity
from tests.shared_fixtures import foreign_keys_off  # noqa: F401  (autouse fixture)


def test_upgrade_adds_the_column_and_copies_existing_owners(test_db):
    from tests.pg_shim import route_db_to
    from app.db.base import column_exists

    with route_db_to(test_db) as shim:
        mobile_tool_identity.init_schema()
        cur = shim.cursor()
        cur.execute("ALTER TABLE sessions DROP COLUMN provenance_owner")
        for sid in ("with-record", "without-record"):
            cur.execute("INSERT INTO sessions (id, instance_id) VALUES (%s, %s)", (sid, "local"))
        cur.execute("INSERT INTO mobile_tool_identity (session_id, owner, kind, principal_id, account_id, "
                    "credential_hash) VALUES (%s, %s, %s, %s, %s, %s)",
                    ("with-record", "account:acct-1", "paired", "pcl_1", "acct-1", "hash"))

        def owners():
            cur = shim.cursor()
            cur.execute("SELECT id, provenance_owner FROM sessions WHERE id IN (%s, %s) ORDER BY id",
                        ("with-record", "without-record"))
            return {r["id"]: r["provenance_owner"] for r in cur.fetchall()}

        database._run_column_migrations(shim.cursor())
        assert column_exists(shim.cursor(), "sessions", "provenance_owner")
        assert owners() == {"with-record": "account:acct-1", "without-record": None}

        # Guarded: the copy runs only with the ALTER, never again.
        shim.cursor().execute("UPDATE sessions SET provenance_owner = NULL WHERE id = %s", ("with-record",))
        database._run_column_migrations(shim.cursor())
        assert owners() == {"with-record": None, "without-record": None}
