"""Read and seed a fact store's stored document in tests.

The store lives in SQLite (``fluid.codemode.fact_db``); these helpers give
tests the document view ``memories.json`` used to give them, without
writing the database file by hand.
"""
from __future__ import annotations

from pathlib import Path

from fluid.codemode.fact_db import FactDB


def read_doc(root: Path) -> dict:
    """The stored document (entries with usage and history, archive,
    tombstones), or an empty one when the store does not exist yet."""
    got = FactDB(Path(root)).read(create=False)
    return got[0] if got else {"entries": [], "archived": [], "tombstones": []}


def write_doc(root: Path, doc: dict) -> None:
    """Replace the stored document with ``doc``, as a hand edit of the old
    file did. Usage counters only move forward (they are merged with MAX)."""
    FactDB(Path(root)).write(doc)


def stored_text(root: Path) -> str:
    """Every stored row as text, for "this phrase is not stored" checks."""
    import sqlite3

    path = Path(root) / "memory.db"
    if not path.exists():
        return ""
    con = sqlite3.connect(str(path))
    try:
        parts = []
        for table in ("facts", "revisions", "tombstones", "meta"):
            parts.extend(str(row) for row in con.execute(f"SELECT * FROM {table}"))
        return "\n".join(parts)
    finally:
        con.close()
