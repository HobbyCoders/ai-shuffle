"""SQLite storage for one memory store (Memory v3, phase 3b).

One database per store, ``memory.db`` beside the legacy ``memories.json``:

* ``facts``: one row per live or archived fact. ``body`` is the fact's JSON
  without its revisions or usage; ``uses`` and ``last_recalled_at`` are
  columns, so a Recall is a one-row update.
* ``revisions``: a fact's undo history (``history`` on the fact), one row each.
* ``tombstones``: one row per forgotten fact, kept for 365 days (see
  ``facts.TOMBSTONE_RETENTION``) and replicated like facts.
* ``deliveries``: local usage events, never replicated: just-in-time
  deliveries (``source`` ``jit``, the row ``jit_delivery`` records) and
  Recalls (``source`` ``recall``).
* ``meta``: the document's scalar fields (``doc.*``) and this layer's own
  state (``sqlite.*``).

Concurrency. Every connection sets a busy timeout and runs short
transactions. Writers that read, change and write the document hold the
store's lock file (``FactStore.lock``) and then one ``BEGIN IMMEDIATE``
transaction; usage writes (Recalls, deliveries) take only ``BEGIN
IMMEDIATE``. Usage columns are only ever raised (``MAX``), so a document
write that read a row before a Recall cannot lower its count. Readers take
no lock file: one read transaction gives them a consistent view. WAL is
used where the filesystem supports it, else the rollback journal.

Migration. The first open imports ``memories.json`` (and an existing
``jit_deliveries.jsonl``) in one transaction and records ``sqlite.imported``,
so it runs once, a crash leaves nothing half imported, and two processes
opening together import once. ``memories.json`` is never written or removed
here: it stays as the backup. If the import fails the store reads from the
JSON file and refuses writes (``FactDB.failure``, shown by memory health).

Downgrade: ``python -m fluid.codemode.fact_db export <store> <file>`` writes a
``memories.json`` document an older build reads; ``downgrade <store>`` writes
it in place and moves the database aside (stop the app first).
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

_log = logging.getLogger(__name__)

DB_NAME = "memory.db"
LEGACY_NAME = "memories.json"
LEGACY_DELIVERY_LOGS = ("jit_deliveries.1.jsonl", "jit_deliveries.jsonl")  # oldest first
LAYOUT = 1
BUSY_TIMEOUT_MS = 10_000
# Usage writes (a Recall's count, a delivery row) ride a tool call: they wait
# this long for a busy store, then are dropped rather than slow the call.
USAGE_TIMEOUT_MS = 1_000
# Usage events older than this are pruned (the lifecycle looks back 90 days).
DELIVERY_RETENTION = 90 * 86400
MAX_DELIVERY_ROWS = 50_000
USAGE_FIELDS = ("uses", "last_recalled_at")
_LISTS = ("entries", "archived", "tombstones")
# Never stored as a document field. ``forgotten`` was the Bloom barrier; an
# imported one is kept frozen in ``sqlite.legacy_forgotten`` instead.
_NOT_STORED = _LISTS + ("forgotten",)

_DDL = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS facts(
    id TEXT PRIMARY KEY,
    state TEXT NOT NULL CHECK (state IN ('live', 'archived')),
    pos INTEGER NOT NULL,
    scope TEXT NOT NULL,
    protected INTEGER NOT NULL,
    always INTEGER NOT NULL,
    updated_at REAL NOT NULL,
    uses INTEGER NOT NULL DEFAULT 0,
    last_recalled_at REAL NOT NULL DEFAULT 0,
    body TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS facts_order ON facts(state, pos);
CREATE TABLE IF NOT EXISTS revisions(
    fact_id TEXT NOT NULL, seq INTEGER NOT NULL, at REAL NOT NULL, body TEXT NOT NULL,
    PRIMARY KEY (fact_id, seq));
CREATE TABLE IF NOT EXISTS tombstones(
    id TEXT PRIMARY KEY, pos INTEGER NOT NULL, deleted_at REAL NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS deliveries(
    seq INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, fact_id TEXT, session TEXT,
    source TEXT NOT NULL, record TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS deliveries_fact ON deliveries(fact_id, at);
CREATE INDEX IF NOT EXISTS deliveries_session ON deliveries(session, seq);
CREATE INDEX IF NOT EXISTS deliveries_at ON deliveries(at);
"""

_ready_guard = threading.Lock()
_ready: dict[str, str] = {}  # database path -> journal mode, once per process


class MigrationError(RuntimeError):
    """``memories.json`` could not be imported; nothing was changed."""


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _fact_row(entry: dict) -> tuple[str, list[str], int, float]:
    """The stored body, revision bodies and usage of one fact."""
    body = {k: v for k, v in entry.items() if k not in USAGE_FIELDS and k != "history"}
    history = [_canonical(h) for h in entry.get("history") or [] if isinstance(h, dict)]
    return _canonical(body), history, _int(entry.get("uses")), _float(entry.get("last_recalled_at"))


class FactDB:
    """The SQLite file of one store. Opens a connection per operation."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.path = self.root / DB_NAME
        self.legacy = self.root / LEGACY_NAME
        # Why the import failed in this process (None: it did not fail).
        self.failure: str | None = None

    # ─── connections ─────────────────────────────────────────────────────
    def exists(self) -> bool:
        """Whether this store holds anything (database or unmigrated JSON)."""
        return self.path.exists() or self.legacy.exists()

    def _connect(self, timeout_ms: int = BUSY_TIMEOUT_MS) -> sqlite3.Connection:
        con = sqlite3.connect(str(self.path), timeout=timeout_ms / 1000, isolation_level=None)
        con.execute(f"PRAGMA busy_timeout={int(timeout_ms)}")
        # A Forget must not leave the forgotten text in freed pages.
        con.execute("PRAGMA secure_delete=ON")
        return con

    def _prepare_file(self) -> None:
        """Create the tables and pick a journal mode, once per process."""
        key = str(self.path)
        if key in _ready and self.path.exists():
            return
        with _ready_guard:
            if key in _ready and self.path.exists():
                return
            self.root.mkdir(parents=True, exist_ok=True)
            mode = "wal"
            con = self._connect()
            try:
                try:
                    mode = str(con.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower()
                    if mode != "wal":
                        raise sqlite3.OperationalError(f"journal mode stayed {mode}")
                    con.executescript(_DDL)
                except sqlite3.OperationalError as exc:
                    # Network and some FUSE filesystems cannot share WAL's
                    # memory index. The rollback journal needs only locks.
                    _log.warning("Memory store %s cannot use WAL (%s); using the rollback journal",
                                 self.path, exc)
                    con.close()
                    con = self._connect()
                    mode = str(con.execute("PRAGMA journal_mode=DELETE").fetchone()[0]).lower()
                    con.executescript(_DDL)
                self._migrate(con)
            finally:
                con.close()
            _ready[key] = mode

    @contextmanager
    def connection(self, *, create: bool,
                   usage: bool = False) -> Iterator[sqlite3.Connection | None]:
        """A ready connection, or None when the database is absent and
        ``create`` is False (observing a store must not create or import
        it; ``FactStore`` reads an unimported ``memories.json`` directly)."""
        if not create and not self.path.exists():
            yield None
            return
        try:
            self._prepare_file()
            self.failure = None
        except MigrationError as exc:
            self.failure = str(exc)
            raise
        con = self._connect(USAGE_TIMEOUT_MS if usage else BUSY_TIMEOUT_MS)
        try:
            if usage and _ready.get(str(self.path)) == "wal":
                # Usage rows are best effort: in WAL their commit skips the
                # fsync (a power cut may lose the last few, never corrupts),
                # so the write lock is held for microseconds. Fact writes
                # keep the default FULL sync.
                con.execute("PRAGMA synchronous=NORMAL")
            yield con
        finally:
            con.close()

    @staticmethod
    @contextmanager
    def _transaction(con: sqlite3.Connection, immediate: bool = True) -> Iterator[None]:
        con.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield
        except BaseException:
            con.execute("ROLLBACK")
            raise
        con.execute("COMMIT")

    @staticmethod
    def _meta(con: sqlite3.Connection, key: str) -> Any:
        row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def _set_meta(con: sqlite3.Connection, key: str, value: Any) -> None:
        con.execute("INSERT INTO meta(key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, _canonical(value)))

    # ─── migration ───────────────────────────────────────────────────────
    def _read_legacy(self) -> dict:
        try:
            raw = self.legacy.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {"next_id": 1, "entries": []}
        except OSError as exc:
            raise MigrationError(f"memories.json could not be read ({exc})") from exc
        try:
            doc = json.loads(raw)
        except ValueError as exc:
            raise MigrationError(f"memories.json does not parse ({exc})") from exc
        if not isinstance(doc, dict):
            raise MigrationError("memories.json does not hold a memory document")
        for key in _LISTS:
            if key in doc and not isinstance(doc[key], list):
                raise MigrationError(f"memories.json field {key!r} is not a list")
        return doc

    def _legacy_deliveries(self) -> list[tuple]:
        rows = []
        for name in LEGACY_DELIVERY_LOGS:
            try:
                raw = (self.root / name).read_bytes()
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise MigrationError(f"{name} could not be read ({exc})") from exc
            from fluid.codemode.jit_delivery import parse_line
            for line in raw.decode("utf-8", "replace").splitlines():
                for row in parse_line(line):
                    if isinstance(row, dict) and isinstance(row.get("session"), str):
                        fact = row.get("fact") if isinstance(row.get("fact"), str) else None
                        rows.append((_float(row.get("at")), fact, row["session"], "jit", _canonical(row)))
        return rows

    def _migrate(self, con: sqlite3.Connection) -> None:
        if self._meta(con, "sqlite.imported") is not None:
            return
        try:
            with self._transaction(con):
                if self._meta(con, "sqlite.imported") is not None:
                    return  # another process imported while this one waited
                source = self.legacy.exists()
                doc = self._read_legacy()
                deliveries = self._legacy_deliveries()
                self._write(con, doc, prune=False)
                forgotten = doc.get("forgotten")
                if isinstance(forgotten, str) and forgotten.strip("0"):
                    self._set_meta(con, "sqlite.legacy_forgotten", forgotten)
                con.executemany("INSERT INTO deliveries(at, fact_id, session, source, record) "
                                "VALUES (?, ?, ?, ?, ?)", deliveries)
                self._set_meta(con, "sqlite.layout", LAYOUT)
                self._set_meta(con, "sqlite.created", uuid.uuid4().hex)
                self._set_meta(con, "sqlite.imported", {
                    "at": time.time(), "source": LEGACY_NAME if source else None,
                    "entries": len(doc.get("entries") or []), "archived": len(doc.get("archived") or []),
                    "tombstones": len(doc.get("tombstones") or []), "deliveries": len(deliveries)})
        except MigrationError:
            raise
        except (sqlite3.Error, ValueError, TypeError, KeyError) as exc:
            raise MigrationError(f"memories.json could not be imported ({exc})") from exc
        if source:
            _log.info("Memory store %s imported from memories.json (kept as a backup)", self.root)

    # ─── document ────────────────────────────────────────────────────────
    def read(self, *, create: bool) -> tuple[dict, dict[str, float]] | None:
        """The stored document and each fact's last delivery time, or None
        when there is no database and ``create`` is False."""
        with self.connection(create=create) as con:
            if con is None:
                return None
            with self._transaction(con, immediate=False):
                return self._read(con)

    @classmethod
    def _read(cls, con: sqlite3.Connection) -> tuple[dict, dict[str, float]]:
        doc: dict = {}
        for key, value in con.execute("SELECT key, value FROM meta WHERE key LIKE 'doc.%'"):
            doc[key[4:]] = json.loads(value)
        history: dict[str, list] = {}
        for fact_id, body in con.execute("SELECT fact_id, body FROM revisions ORDER BY fact_id, seq"):
            history.setdefault(fact_id, []).append(json.loads(body))
        doc["entries"], doc["archived"] = [], []
        for fact_id, state, uses, recalled, body in con.execute(
                "SELECT id, state, uses, last_recalled_at, body FROM facts ORDER BY pos"):
            entry = json.loads(body)
            if uses:
                entry["uses"] = uses
            if recalled:
                entry["last_recalled_at"] = recalled
            entry["history"] = history.get(fact_id, [])
            doc["entries" if state == "live" else "archived"].append(entry)
        doc["tombstones"] = [json.loads(b) for (b,) in con.execute("SELECT body FROM tombstones ORDER BY pos")]
        # Read-only: the store checks the frozen barrier but never grows it.
        doc["forgotten"] = str(cls._meta(con, "sqlite.legacy_forgotten") or "")
        delivered = dict(con.execute(
            "SELECT fact_id, MAX(at) FROM deliveries WHERE fact_id IS NOT NULL GROUP BY fact_id"))
        return doc, delivered

    def write(self, doc: dict) -> bool:
        """Store ``doc`` (the shape ``FactStore`` uses), changing only the
        rows that differ; False when nothing differed (no generation bump).
        The caller holds the store's lock file."""
        with self.connection(create=True) as con:
            with self._transaction(con):
                changed, removed = self._write(con, doc)
            if removed:
                # The WAL still holds the removed rows' old page images:
                # fold it back and truncate it (best effort; a busy reader
                # leaves it to the next checkpoint).
                try:
                    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except sqlite3.Error:
                    pass
        return changed

    @classmethod
    def _write(cls, con: sqlite3.Connection, doc: dict, *, prune: bool = True) -> tuple[bool, bool]:
        """Store ``doc``. Returns (any row changed, a fact or revision row was
        removed); an unchanged document leaves the generation as it is."""
        removed = changed = False
        old = {fid: (state, body, uses, recalled) for fid, state, body, uses, recalled in con.execute(
            "SELECT id, state, body, uses, last_recalled_at FROM facts")}
        old_history: dict[str, list[str]] = {}
        for fact_id, body in con.execute("SELECT fact_id, body FROM revisions ORDER BY fact_id, seq"):
            old_history.setdefault(fact_id, []).append(body)
        pos = (con.execute("SELECT MAX(pos) FROM facts").fetchone()[0] or 0) + 1
        seen: set[str] = set()
        for state, bucket in (("live", doc.get("entries") or []), ("archived", doc.get("archived") or [])):
            for entry in bucket:
                if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
                    continue
                fact_id = entry["id"]
                if fact_id in seen:
                    continue
                seen.add(fact_id)
                body, history, uses, recalled = _fact_row(entry)
                columns = (entry.get("scope") or "global", int(bool(entry.get("protected"))),
                           int(bool(entry.get("always"))), _float(entry.get("updated_at")))
                before = old.get(fact_id)
                if before is None or before[0] != state:
                    # New here, or moved between live and archive: it goes
                    # to the end of its list, as an append did in the file.
                    con.execute(
                        "INSERT INTO facts(id, state, pos, scope, protected, always, updated_at, uses, "
                        "last_recalled_at, body) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT(id) DO UPDATE SET state=excluded.state, pos=excluded.pos, "
                        "scope=excluded.scope, protected=excluded.protected, always=excluded.always, "
                        "updated_at=excluded.updated_at, uses=MAX(uses, excluded.uses), "
                        "last_recalled_at=MAX(last_recalled_at, excluded.last_recalled_at), "
                        "body=excluded.body",
                        (fact_id, state, pos, *columns, uses, recalled, body))
                    pos += 1
                    changed = True
                elif before[1] != body or uses > before[2] or recalled > before[3]:
                    changed = True
                    con.execute(
                        "UPDATE facts SET scope=?, protected=?, always=?, updated_at=?, "
                        "uses=MAX(uses, ?), last_recalled_at=MAX(last_recalled_at, ?), body=? WHERE id=?",
                        (*columns, uses, recalled, body, fact_id))
                if history != old_history.get(fact_id, []):
                    changed = True
                    removed = removed or bool(set(old_history.get(fact_id, [])) - set(history))
                    con.execute("DELETE FROM revisions WHERE fact_id=?", (fact_id,))
                    con.executemany(
                        "INSERT INTO revisions(fact_id, seq, at, body) VALUES (?, ?, ?, ?)",
                        [(fact_id, n, _float(json.loads(h).get("at")), h) for n, h in enumerate(history)])
        gone = [(fid,) for fid in old if fid not in seen]
        con.executemany("DELETE FROM facts WHERE id=?", gone)
        con.executemany("DELETE FROM revisions WHERE fact_id=?", gone)
        changed = cls._write_tombstones(con, doc.get("tombstones") or []) or changed or bool(gone)
        stored = {k[4:]: v for k, v in con.execute("SELECT key, value FROM meta WHERE key LIKE 'doc.%'")}
        fields = {k: _canonical(v) for k, v in doc.items() if k not in _NOT_STORED}
        for key, value in fields.items():
            if stored.get(key) != value:
                changed = True
                con.execute("INSERT INTO meta(key, value) VALUES (?, ?) "
                            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", ("doc." + key, value))
        dropped = [("doc." + k,) for k in stored if k not in fields]
        con.executemany("DELETE FROM meta WHERE key=?", dropped)
        changed = changed or bool(dropped)
        if changed:
            cls._bump(con)
        if prune:
            cls._prune_deliveries(con)
        return changed, removed or bool(gone)

    @staticmethod
    def _write_tombstones(con: sqlite3.Connection, tombstones: list) -> bool:
        """Store the tombstone rows; True when any row changed."""
        changed = False
        old = dict(con.execute("SELECT id, body FROM tombstones"))
        pos = (con.execute("SELECT MAX(pos) FROM tombstones").fetchone()[0] or 0) + 1
        latest: dict[str, dict] = {}  # a repeated id keeps its newest body
        for t in tombstones:
            if isinstance(t, dict) and isinstance(t.get("id"), str):
                latest[t["id"]] = t
        seen = set(latest)
        for t in latest.values():
            body = _canonical(t)
            if t["id"] not in old:
                con.execute("INSERT INTO tombstones(id, pos, deleted_at, body) VALUES (?, ?, ?, ?)",
                            (t["id"], pos, _float(t.get("deleted_at")), body))
                pos += 1
                changed = True
            elif old[t["id"]] != body:
                changed = True
                con.execute("UPDATE tombstones SET deleted_at=?, body=? WHERE id=?",
                            (_float(t.get("deleted_at")), body, t["id"]))
        gone = [(i,) for i in old if i not in seen]
        con.executemany("DELETE FROM tombstones WHERE id=?", gone)
        return changed or bool(gone)

    @classmethod
    def _bump(cls, con: sqlite3.Connection) -> None:
        cls._set_meta(con, "sqlite.generation", int(cls._meta(con, "sqlite.generation") or 0) + 1)

    @staticmethod
    def _prune_deliveries(con: sqlite3.Connection) -> None:
        con.execute("DELETE FROM deliveries WHERE at < ?", (time.time() - DELIVERY_RETENTION,))
        con.execute("DELETE FROM deliveries WHERE seq <= (SELECT MAX(seq) FROM deliveries) - ?",
                    (MAX_DELIVERY_ROWS,))

    def stamp(self) -> tuple:
        """Changes whenever the facts change (a write or a Recall), not on a delivery."""
        with self.connection(create=False) as con:
            if con is None:
                return ("", 0)
            rows = dict(con.execute("SELECT key, value FROM meta WHERE key IN "
                                    "('sqlite.created', 'sqlite.generation')"))
        return (rows.get("sqlite.created", ""), rows.get("sqlite.generation", "0"))

    def meta_field(self, name: str) -> Any:
        """One document field (``store_id``, ``owner``) without reading facts."""
        with self.connection(create=False) as con:
            return None if con is None else self._meta(con, "doc." + name)

    def legacy_forgotten(self) -> str:
        """The Bloom barrier imported from ``memories.json``, frozen; '' if none."""
        with self.connection(create=False) as con:
            return "" if con is None else str(self._meta(con, "sqlite.legacy_forgotten") or "")

    def status(self) -> dict:
        """Storage facts for health and the CLI. Never creates the database."""
        info: dict[str, Any] = {"path": str(self.path), "exists": self.path.exists(),
                                "journal_mode": _ready.get(str(self.path))}
        if self.failure:
            info.update(state="migration_failed", reason=self.failure)
            return info
        try:
            with self.connection(create=False) as con:
                if con is None:
                    # No database yet; the first write imports the document.
                    info["state"] = "pending" if self.legacy.exists() else "empty"
                    return info
                info["imported"] = self._meta(con, "sqlite.imported")
                info["journal_mode"] = str(con.execute("PRAGMA journal_mode").fetchone()[0]).lower()
                for table in ("facts", "revisions", "tombstones", "deliveries"):
                    info[table] = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except MigrationError as exc:
            info.update(state="migration_failed", reason=str(exc))
            return info
        info["bytes"] = sum(p.stat().st_size for p in (self.path, Path(f"{self.path}-wal"))
                            if p.exists())
        info["state"] = "ok"
        return info

    # ─── usage (local, never replicated) ────────────────────────────────
    def note_recalled(self, ids, now: float) -> None:
        """One row update per recalled live fact, plus its usage event."""
        ids = [str(i) for i in ids]
        with self.connection(create=True, usage=True) as con:
            with self._transaction(con):
                con.executemany("UPDATE facts SET uses=MIN(1000000, uses + 1), "
                                "last_recalled_at=MAX(last_recalled_at, ?) WHERE id=? AND state='live'",
                                [(now, i) for i in ids])
                con.executemany("INSERT INTO deliveries(at, fact_id, session, source, record) "
                                "VALUES (?, ?, NULL, 'recall', '{}')", [(now, i) for i in ids])
                self._bump(con)

    def record_deliveries(self, rows: list[dict]) -> None:
        """Append just-in-time delivery rows (``jit_delivery.record``)."""
        with self.connection(create=True, usage=True) as con:
            with self._transaction(con):
                con.executemany(
                    "INSERT INTO deliveries(at, fact_id, session, source, record) VALUES (?, ?, ?, 'jit', ?)",
                    [(_float(r.get("at")), r.get("fact") if isinstance(r.get("fact"), str) else None,
                      r.get("session"), _canonical(r)) for r in rows])
                self._prune_deliveries(con)

    def legacy_delivery_rows(self) -> list[dict]:
        """The pre-SQLite delivery log's rows (read while an import fails)."""
        return [json.loads(record) for _, _, _, _, record in self._legacy_deliveries()]

    def deliveries(self, *, session: str | None = None) -> list[dict]:
        """Just-in-time rows, oldest first, optionally for one session."""
        with self.connection(create=False) as con:
            if con is None:
                # Not imported yet: the old log as it is (no import on a read).
                return [r for r in self.legacy_delivery_rows()
                        if session is None or r.get("session") == session]
            if session is None:
                cur = con.execute("SELECT record FROM deliveries WHERE source='jit' ORDER BY seq")
            else:
                cur = con.execute("SELECT record FROM deliveries WHERE source='jit' AND session=? "
                                  "ORDER BY seq", (session,))
            return [json.loads(r) for (r,) in cur]

    def recent_uses(self, since: float) -> dict[str, int]:
        """Deliveries plus Recalls per fact since ``since`` (its value)."""
        with self.connection(create=False) as con:
            if con is None:
                return {}
            return dict(con.execute("SELECT fact_id, COUNT(*) FROM deliveries WHERE fact_id IS NOT NULL "
                                    "AND at >= ? GROUP BY fact_id", (since,)))


# ─── downgrade / export ─────────────────────────────────────────────────

def export_document(root: Path) -> dict:
    """A ``memories.json`` document of the store an older build can read.

    Everything is kept (live, archive, revisions, tombstones). An older build
    keeps only its newest 256 tombstones, so ``forgotten`` is the Bloom
    barrier it expects, built from every tombstone row and the imported one.
    """
    from fluid.codemode.facts import SCHEMA, FactStore, _wire_barrier
    db = FactDB(root)
    owner = db.meta_field("owner") or "local"
    doc = FactStore(root, owner=owner)._load(read_only=True)
    doc.update(schema=SCHEMA, owner=owner,
               forgotten=_wire_barrier(db.legacy_forgotten(), doc["tombstones"]))
    return doc


def _write_json(path: Path, doc: dict) -> None:
    from fluid.codemode.skills import atomic_write_text
    atomic_write_text(path, json.dumps(doc, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(prog="python -m fluid.codemode.fact_db",
                                     description="Inspect, export or downgrade a Fluid memory store.")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, text in (("status", "show the store's storage state"),
                       ("export", "write the store as a memories.json document"),
                       ("downgrade", "write memories.json in place and move memory.db aside "
                                     "(stop the app first)")):
        cmd = sub.add_parser(name, help=text)
        cmd.add_argument("store", type=Path, help="the store directory (holds memory.db)")
        if name == "export":
            cmd.add_argument("output", type=Path, help="the JSON file to write")
    args = parser.parse_args(argv)
    db = FactDB(args.store)
    if args.command == "status":
        print(json.dumps(db.status(), indent=2, default=str))
        return 0
    if not db.path.exists():
        parser.error(f"no {DB_NAME} in {args.store}")
    doc = export_document(args.store)
    if args.command == "export":
        _write_json(args.output, doc)
        print(f"Wrote {len(doc['entries'])} live and {len(doc['archived'])} archived facts to {args.output}")
        return 0
    _write_json(db.legacy, doc)
    aside = db.path.with_name(f"{DB_NAME}.downgraded-{int(time.time())}")
    for suffix in ("", "-wal", "-shm"):
        part = Path(f"{db.path}{suffix}")
        if part.exists():
            part.replace(Path(f"{aside}{suffix}"))
    print(f"Wrote {db.legacy} and moved the database to {aside.name}; "
          "an older build reads memories.json, and a newer one imports it again.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
