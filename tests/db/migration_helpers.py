"""SQLite introspection helpers for the migration tests. Import explicitly."""


def column_names(conn, table):
    """The column names of ``table``."""
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def schema_version(conn):
    """The single row of ``schema_version``."""
    return conn.execute("SELECT version FROM schema_version").fetchone()[0]
