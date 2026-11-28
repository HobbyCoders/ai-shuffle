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
opening together import once. A database without that marker is never
served. ``memories.json`` is never written or removed here: it stays as the
backup. If the import fails (a document of a newer schema or another owner,
an unreadable file, a full disk) the database file the attempt created is
removed again, the store reads from the JSON file and refuses every write
(``FactDB.failure``, shown by memory health); a JSON file that does not parse
fails the read instead of serving an empty store.

Downgrade: ``python -m fluid.codemode.fact_db export <store> <file>`` writes a
``memories.json`` document an older build reads; ``downgrade <store>`` moves
the database aside, then writes it in place, keeping the previous
``memories.json`` as ``memories.json.pre-downgrade-<time>`` (stop the app
first).
"""
from __future__ import annotations

import json
import logging
import os
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
# Usage events older than this are pruned. They feed only a fact's value
# (deliveries plus Recalls in the last 60 days) and a session's re-arm
# bookkeeping; a fact's latest delivery and Recall are columns on its row
# (``last_delivered_at``, ``last_recalled_at``), so pruning never shortens
# its life.
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
    last_delivered_at REAL NOT NULL DEFAULT 0,
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

# One transaction: one fsync instead of one per statement. IMMEDIATE takes
# the write lock up front (waiting out the busy timeout) instead of failing
# at once when another process holds it while this one upgrades a read lock.
_DDL_ONCE = "BEGIN IMMEDIATE;" + _DDL + "COMMIT;"

# database path -> why its import failed in this process (FactDB.failure).
_failures: dict[str, str] = {}
_ready_guard = threading.Lock()
# database path -> (file identity, journal mode) once per process; a file
# replaced under the same name (a restore, a downgrade and re-upgrade) has
# another identity and is prepared again.
_ready: dict[str, tuple[tuple[int, int] | None, str]] = {}
# Connections a forked child inherited: kept referenced so the child's
# garbage collector never closes the parent's SQLite handles.
_inherited: list = []

# Open connections, one per store per thread, reused across operations.
# Opening a connection rereads the schema and maps the WAL index, and closing
# the last one checkpoints and deletes the WAL with fsyncs: per operation that
# cost more than the old whole-document rewrite. Bounded so a process that
# touches many stores (tests) does not hold their files open.
_POOL_SIZE = 8
_pool = threading.local()


class _Pooled:
    __slots__ = ("con", "ident", "busy", "sync")

    def __init__(self, con: sqlite3.Connection, ident: tuple[int, int] | None):
        self.con, self.ident, self.busy, self.sync = con, ident, False, ""


def _file_ident(path: Path) -> tuple[int, int] | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_dev, st.st_ino


def _thread_pool() -> dict[str, _Pooled]:
    # A forked child must not use its parent's connections (SQLite forbids
    # it); it abandons them rather than closing them, and keeps them
    # referenced so collecting them cannot close the parent's handles.
    if getattr(_pool, "pid", None) != os.getpid():
        if getattr(_pool, "cons", None):
            _inherited.append(_pool.cons)
        _pool.pid, _pool.cons = os.getpid(), {}
    return _pool.cons


def _busy(exc: BaseException) -> bool:
    """Whether a SQLite error is lock contention (worth waiting out), not a
    property of the file or the filesystem."""
    code = getattr(exc, "sqlite_errorcode", None)
    if isinstance(code, int):
        return code & 0xFF in (5, 6)  # SQLITE_BUSY, SQLITE_LOCKED
    text = str(exc).lower()
    return "locked" in text or "busy" in text


def release_connections() -> None:
    """Close this thread's cached connections (before moving a database file
    aside: Windows cannot rename a file that is open)."""
    cons = _thread_pool()
    for entry in list(cons.values()):
        if not entry.busy:
            entry.con.close()
    cons.clear()


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
    """The SQLite file of one store. Each thread reuses one cached
    connection to it (see ``_checkout``).

    ``owner``, when given, is checked against the document before an
    import: a ``memories.json`` of another owner is never imported."""

    def __init__(self, root: Path, owner: str | None = None):
        self.root = Path(root)
        self.path = self.root / DB_NAME
        self.legacy = self.root / LEGACY_NAME
        self.owner = owner

    @property
    def failure(self) -> str | None:
        """Why this store's import last failed in this process (None: it did
        not, or it has succeeded since). Kept per file, not per instance, so
        every store object in the process (health, the Settings API) sees it."""
        return _failures.get(str(self.path))

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

    def _ready_mode(self) -> str | None:
        """The journal mode this process prepared the file in, if the file
        on disk is still the one it prepared."""
        cached = _ready.get(str(self.path))
        if cached is None:
            return None
        ident = _file_ident(self.path)
        return cached[1] if ident is not None and cached[0] == ident else None

    @staticmethod
    def _imported(con: sqlite3.Connection) -> bool | None:
        """Whether the file holds the import marker; None if unreadable."""
        try:
            return FactDB._meta(con, "sqlite.imported") is not None
        except sqlite3.OperationalError as exc:
            return False if "no such table" in str(exc) else None
        except sqlite3.Error:
            return None

    def _schema(self, con: sqlite3.Connection) -> str:
        """Pick the journal mode and create the tables; returns the mode."""
        try:
            mode = str(con.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower()
            if mode != "wal":
                raise sqlite3.OperationalError(f"journal mode stayed {mode}")
            con.executescript(_DDL_ONCE)
        except sqlite3.OperationalError as exc:
            if _busy(exc):
                raise  # another process holds the lock: not a WAL problem
            if con.in_transaction:
                con.execute("ROLLBACK")
            # Network and some FUSE filesystems cannot share WAL's memory
            # index. The rollback journal needs only locks. (A failure WAL is
            # not the cause of, a full disk say, fails again below.)
            _log.warning("Memory store %s cannot use WAL (%s); using the rollback journal",
                         self.path, exc)
            mode = str(con.execute("PRAGMA journal_mode=DELETE").fetchone()[0]).lower()
            con.executescript(_DDL_ONCE)
        self._upgrade(con)
        return mode

    @staticmethod
    def _upgrade(con: sqlite3.Connection) -> None:
        """Add columns a database created before them lacks."""
        columns = {row[1] for row in con.execute("PRAGMA table_info(facts)")}
        if "last_delivered_at" not in columns:
            with FactDB._transaction(con):
                con.execute("ALTER TABLE facts ADD COLUMN last_delivered_at REAL NOT NULL DEFAULT 0")
                con.execute("UPDATE facts SET last_delivered_at=COALESCE((SELECT MAX(at) FROM deliveries "
                            "WHERE deliveries.fact_id=facts.id AND source='jit'), 0)")

    def _remove_files(self, ident: tuple[int, int] | None) -> None:
        """Remove a database this process created for an import that failed,
        so no empty store is left to pass for an imported one. Only the file
        it created (same identity): another process may have made a new one.
        On Windows an open file cannot be removed; it stays without the
        import marker and the next open imports again."""
        if ident is None or _file_ident(self.path) != ident:
            return
        for suffix in ("", "-wal", "-shm", "-journal"):
            try:
                Path(f"{self.path}{suffix}").unlink()
            except FileNotFoundError:
                pass
            except OSError:
                _log.warning("Could not remove %s%s after a failed import", self.path, suffix)
                return

    def _prepare_file(self, *, create: bool) -> bool:
        """Create the tables, pick a journal mode and import the legacy
        document, once per process per file. False when the database is
        absent and ``create`` is False.

        A store whose import has not committed is never served from the
        database: any failure on the way (the document, a full disk, a
        read-only folder) raises MigrationError, and a database file this
        call created is removed again."""
        if self._ready_mode() is not None:
            return True
        with _ready_guard:
            if self._ready_mode() is not None:
                return True
            existed = self.path.exists()
            if not existed and not create:
                return False
            # An existing file is "unknown" until its marker is read: a store
            # that may hold imported facts never falls back to the (older)
            # memories.json; its errors are storage errors.
            imported: bool | None = None if existed else False
            ident = None
            con = None
            try:
                try:
                    self.root.mkdir(parents=True, exist_ok=True)
                    con = self._connect()
                    ident = None if existed else _file_ident(self.path)
                    if existed:
                        imported = self._imported(con)
                        if imported is None:
                            raise sqlite3.DatabaseError("memory.db cannot be read")
                    mode = self._schema(con)
                    if not imported:
                        self._migrate(con)
                finally:
                    if con is not None:
                        con.close()
            except MigrationError:
                self._remove_files(ident)
                raise
            except (sqlite3.Error, OSError) as exc:
                if imported is not False or _busy(exc):
                    # Imported or unreadable: an ordinary storage error. Busy:
                    # another process holds the file (it may be importing
                    # it), so it is neither removed nor called failed.
                    raise
                self._remove_files(ident)
                raise MigrationError(f"memory.db could not be set up ({exc})") from exc
            _ready[str(self.path)] = (_file_ident(self.path), mode)
            return True

    @contextmanager
    def connection(self, *, create: bool,
                   usage: bool = False) -> Iterator[sqlite3.Connection | None]:
        """A ready connection, or None when the database is absent and
        ``create`` is False (observing a store must not create or import
        it; ``FactStore`` reads an unimported ``memories.json`` directly)."""
        try:
            ready = self._prepare_file(create=create)
        except MigrationError as exc:
            _failures[str(self.path)] = str(exc)
            raise
        if ready:
            _failures.pop(str(self.path), None)
        if not ready:
            yield None
            return
        entry, cached = self._checkout()
        con = entry.con
        try:
            con.execute(f"PRAGMA busy_timeout={USAGE_TIMEOUT_MS if usage else BUSY_TIMEOUT_MS}")
            # Usage rows are best effort: in WAL their commit skips the fsync
            # (a power cut may lose the last few, never corrupts), so the
            # write lock is held for microseconds. Fact writes keep FULL.
            sync = "NORMAL" if usage and self._ready_mode() == "wal" else "FULL"
            if entry.sync != sync:
                con.execute(f"PRAGMA synchronous={sync}")
                entry.sync = sync
            yield con
        except sqlite3.Error:
            cached = self._discard(entry, cached)
            raise
        finally:
            entry.busy = False
            if cached and con.in_transaction:  # never hand on an open transaction
                try:
                    con.execute("ROLLBACK")
                except sqlite3.Error:
                    cached = self._discard(entry, cached)
            if not cached:
                con.close()

    def _checkout(self) -> tuple[_Pooled, bool]:
        """This thread's connection to the store and whether it is cached.
        A nested use on the same thread gets a private one."""
        cons, key = _thread_pool(), str(self.path)
        ident = _file_ident(self.path)
        entry = cons.get(key)
        if entry is not None and entry.busy:
            return _Pooled(self._connect(), ident), False
        if entry is not None and (entry.ident != ident or ident is None):
            entry.con.close()  # the file was replaced or removed under us
            del cons[key]
            entry = None
        if entry is None:
            entry = _Pooled(self._connect(), ident)
            if ident is None:
                return entry, False
            while len(cons) >= _POOL_SIZE:
                oldest = next((k for k, e in cons.items() if not e.busy), None)
                if oldest is None:
                    break
                cons.pop(oldest).con.close()
            cons[key] = entry
        else:
            cons[key] = cons.pop(key)  # most recently used last
        entry.busy = True
        return entry, True

    def _discard(self, entry: _Pooled, cached: bool) -> bool:
        if cached and _thread_pool().get(str(self.path)) is entry:
            del _thread_pool()[str(self.path)]
        return False

    @staticmethod
    @contextmanager
    def _transaction(con: sqlite3.Connection, immediate: bool = True) -> Iterator[None]:
        con.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield
        except BaseException:
            # SQLite may already have rolled back (SQLITE_FULL, an I/O
            # error); a second ROLLBACK must not mask the original error.
            if con.in_transaction:
                try:
                    con.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
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
        from fluid.codemode.facts import SCHEMA
        try:
            schema = int(doc.get("schema", 1))
        except (TypeError, ValueError):
            raise MigrationError("memories.json has an unreadable schema") from None
        if schema > SCHEMA:
            raise MigrationError(f"memories.json was written by a newer application (schema {schema})")
        if self.owner is not None and doc.get("owner", self.owner) != self.owner:
            raise MigrationError("memories.json belongs to another owner")
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
                self._note_delivered(con, [(at, fact) for at, fact, *_ in deliveries if fact])
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
        delivered: dict[str, float] = {}
        for fact_id, state, uses, recalled, last_delivered, body in con.execute(
                "SELECT id, state, uses, last_recalled_at, last_delivered_at, body FROM facts ORDER BY pos"):
            entry = json.loads(body)
            if uses:
                entry["uses"] = uses
            if recalled:
                entry["last_recalled_at"] = recalled
            if last_delivered:
                delivered[fact_id] = last_delivered
            entry["history"] = history.get(fact_id, [])
            doc["entries" if state == "live" else "archived"].append(entry)
        doc["tombstones"] = [json.loads(b) for (b,) in con.execute("SELECT body FROM tombstones ORDER BY pos")]
        # Read-only: the store checks the frozen barrier but never grows it.
        doc["forgotten"] = str(cls._meta(con, "sqlite.legacy_forgotten") or "")
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
                                "journal_mode": self._ready_mode()}
        if self.failure:
            info.update(state="migration_failed", reason=self.failure)
            return info
        try:
            with self.connection(create=False) as con:
                if con is None:
                    # No database yet; the first write imports the document.
                    # One another process failed to import (it leaves no
                    # database behind) is checked here, so health in every
                    # process names it.
                    if self.legacy.exists():
                        self._read_legacy()
                    info["state"] = "pending" if self.legacy.exists() else "empty"
                    return info
                info["imported"] = self._meta(con, "sqlite.imported")
                info["journal_mode"] = str(con.execute("PRAGMA journal_mode").fetchone()[0]).lower()
                for table in ("facts", "revisions", "tombstones", "deliveries"):
                    info[table] = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except MigrationError as exc:
            info.update(state="migration_failed", reason=str(exc))
            return info
        except (sqlite3.Error, OSError) as exc:
            # An imported store that cannot be opened: no fallback (the JSON
            # backup is older than its facts); every read fails until fixed.
            info.update(state="unavailable", reason=f"{type(exc).__name__}: {exc}")
            return info
        info["bytes"] = sum(p.stat().st_size for p in (self.path, Path(f"{self.path}-wal"))
                            if p.exists())
        info["state"] = "ok"
        return info

    # ─── usage (local, never replicated) ────────────────────────────────
    # Usage writes reach the database only through ``connection``: a store
    # whose import failed (or is not this owner's, or is newer) raises
    # MigrationError there and is never written. A Recall never creates a
    # store (there is nothing to count); a delivery may (it is the log).
    def note_recalled(self, ids, now: float) -> None:
        """One row update per recalled live fact, plus its usage event."""
        ids = [str(i) for i in ids]
        with self.connection(create=self.exists(), usage=True) as con:
            if con is None:
                return
            with self._transaction(con):
                con.executemany("UPDATE facts SET uses=MIN(1000000, uses + 1), "
                                "last_recalled_at=MAX(last_recalled_at, ?) WHERE id=? AND state='live'",
                                [(now, i) for i in ids])
                con.executemany("INSERT INTO deliveries(at, fact_id, session, source, record) "
                                "SELECT ?, id, NULL, 'recall', '{}' FROM facts WHERE id=? AND state='live'",
                                [(now, i) for i in ids])
                self._bump(con)

    def record_deliveries(self, rows: list[dict]) -> None:
        """Append just-in-time delivery rows (``jit_delivery.record``) and
        raise each delivered fact's ``last_delivered_at``."""
        values = [(_float(r.get("at")), r.get("fact") if isinstance(r.get("fact"), str) else None,
                   r.get("session"), _canonical(r)) for r in rows]
        with self.connection(create=True, usage=True) as con:
            if con is None:
                return
            with self._transaction(con):
                con.executemany(
                    "INSERT INTO deliveries(at, fact_id, session, source, record) VALUES (?, ?, ?, 'jit', ?)",
                    values)
                self._note_delivered(con, [(at, fact) for at, fact, *_ in values if fact])
                self._prune_deliveries(con)

    @staticmethod
    def _note_delivered(con: sqlite3.Connection, pairs: list[tuple[float, str]]) -> None:
        """Raise ``last_delivered_at`` (only ever raised, like the usage
        columns): the delivery renews the fact's life however long its
        delivery row is kept."""
        con.executemany("UPDATE facts SET last_delivered_at=MAX(last_delivered_at, ?) WHERE id=?", pairs)

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
    return _downgrade(db, doc)


MOVE_ATTEMPTS = 6


def _move(src: Path, dst: Path) -> bool:
    """Rename ``src`` to ``dst``; False if ``src`` is gone (SQLite deletes
    ``-wal``/``-shm`` when its last connection closes). Windows refuses to
    rename a file another process briefly holds (an antivirus scan, the
    indexer: WinError 5 or 32), so that is retried a few times."""
    for attempt in range(MOVE_ATTEMPTS):
        try:
            src.replace(dst)
            return True
        except FileNotFoundError:
            return False
        except PermissionError:
            if attempt == MOVE_ATTEMPTS - 1:
                raise
            time.sleep(0.1 * (attempt + 1))
    return False


def _downgrade(db: FactDB, doc: dict) -> int:
    """Move the database aside, then write ``doc`` as ``memories.json``.

    In that order, so a database still in use (the app running) fails the
    downgrade before anything changes. The existing ``memories.json`` (the
    pre-SQLite backup) is copied first, and a failed move or write puts the
    database back: either the store is downgraded or it is as it was."""
    import shutil
    release_connections()
    stamp = int(time.time())
    aside = db.path.with_name(f"{DB_NAME}.downgraded-{stamp}")
    backup = None
    if db.legacy.exists():
        backup = db.legacy.with_name(f"{LEGACY_NAME}.pre-downgrade-{stamp}")
        shutil.copy2(db.legacy, backup)
    moved: list[tuple[Path, Path]] = []
    try:
        for suffix in ("", "-wal", "-shm"):
            src, dst = Path(f"{db.path}{suffix}"), Path(f"{aside}{suffix}")
            if _move(src, dst):
                moved.append((src, dst))
        if not moved:
            raise FileNotFoundError(f"{db.path} is gone")
        _write_json(db.legacy, doc)
    except OSError as exc:
        for src, dst in reversed(moved):
            try:
                _move(dst, src)
            except OSError:
                print(f"Could not move {dst.name} back to {src.name}; move it back by hand.")
        print(f"Downgrade failed ({exc}); the store is unchanged. Stop the app and try again.")
        return 1
    note = f" (the previous one is {backup.name})" if backup else ""
    print(f"Wrote {db.legacy}{note} and moved the database to {aside.name}; "
          "an older build reads memories.json, and a newer one imports it again.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
