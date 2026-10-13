"""Several independent workers in one test process.

Each ``Worker`` owns an in-memory SQLite database (through the same shim the
suite uses for the app), a data directory holding its device identity, and
its own in-memory network context. ``with worker.active():`` routes the app's
DB layer, identity cache and engine context to that worker, so the real
engine code runs unmodified for whichever worker is "current".
"""
from __future__ import annotations

import contextlib
import contextvars
from typing import List, Optional
from unittest.mock import patch

from app.core.config import settings
from app.db import base, database
from app.network import engine, identity
from tests.pg_shim import ShimConnection, close_connection, new_sqlite_connection

#: The worker whose ``active()`` block the current context runs in; copied into
#: tasks and ``asyncio.to_thread`` calls, so their database reads stay on it.
_ACTIVE: contextvars.ContextVar[Optional["Worker"]] = contextvars.ContextVar("network_worker", default=None)


class Worker:
    def __init__(self, name: str, tmp_path):
        self.name = name
        self.data_dir = tmp_path / name
        self.data_dir.mkdir()
        self.raw = new_sqlite_connection()
        self.shim = ShimConnection(self.raw)
        self.identity: Optional[identity.DeviceIdentity] = None
        self.context: Optional[engine.NetworkContext] = None
        # Every worker seeds the same built-in profile, like a real boot does.
        with self.active():
            try:
                database.create_profile(profile_id="default", name="Default Profile", description=None,
                                        config={"model": "claude-sonnet-4-20250514"})
            except Exception:  # noqa: BLE001
                pass
            self.identity = identity.load()

    @contextlib.contextmanager
    def active(self):
        def _conn():
            # The patch below is process-wide, but work that ``asyncio.to_thread``
            # started under this worker may still be running in a pool thread
            # when the loop thread switches to the other worker (a mock HTTP
            # call serves its request as the receiving worker, synchronously).
            # Such a thread carries the context it was started in, so it keeps
            # reading its own worker's database instead of the other one's.
            return (_ACTIVE.get() or self).shim
        with patch.object(base, "get_connection", _conn), patch.object(database, "get_connection", _conn), \
                patch.object(settings, "data_dir", self.data_dir), \
                patch.object(settings, "fluid_skills_dir", str(self.data_dir / "fluid" / "skills")):
            outer_identity, outer_context = identity._cached, engine._context
            identity._cached = self.identity
            engine._context = self.context
            outer_active = _ACTIVE.get()
            token = _ACTIVE.set(self)
            try:
                yield self
            finally:
                try:
                    _ACTIVE.reset(token)
                except ValueError:  # exited in another context than it was entered in
                    _ACTIVE.set(outer_active)
                self.identity = identity._cached
                self.context = engine._context
                # Nesting-safe: hand the globals back to whichever worker was active before.
                identity._cached = outer_identity
                engine._context = outer_context

    @property
    def device_id(self) -> str:
        return self.identity.device_id

    def close(self):
        close_connection(self.raw)

    # -- convenience -----------------------------------------------------
    def sql(self, query, params=()):
        cursor = self.raw.cursor()
        cursor.execute(query, params)
        return [dict(r) for r in cursor.fetchall()]


def exchange(sender: Worker, receiver: Worker, *, limit: int = 500) -> engine.ApplyResult:
    """Deliver everything ``receiver`` lacks from ``sender`` (one direction)."""
    from app.db import network as store
    with receiver.active():
        marks = store.local_watermarks()
    with sender.active():
        ops, _, needs_snapshot = store.exchange_batch(marks, limit=limit, exclude_origin=receiver.device_id)
        chunks = []
        if needs_snapshot:
            manifest = engine.snapshot_manifest()
            for collection in manifest["collections"]:
                offset = 0
                while True:
                    chunk = engine.snapshot_chunk(collection, offset)
                    chunks.append(chunk)
                    if not chunk["more"]:
                        break
                    offset += chunk["count"]
    with receiver.active():
        if needs_snapshot:
            # Like PeerLink's ordinary catch-up path: apply state in dependency
            # order and only then adopt the publisher's frontier. No re-enroll.
            for chunk in chunks:
                engine.apply_snapshot_chunk(chunk)
            engine.adopt_watermarks(manifest["watermarks"])
            return engine.ApplyResult()
        return engine.apply_batch(ops)


def converge(workers: List[Worker], rounds: int = 3) -> None:
    for _ in range(rounds):
        for a in workers:
            for b in workers:
                if a is not b:
                    exchange(a, b)


def join(inviter: Worker, joiner: Worker, *, name: str = None, legacy_instance_id: str = None) -> None:
    """Admit ``joiner`` from ``inviter`` and bootstrap it from a snapshot."""
    with joiner.active():
        public = identity.load().public_summary()
    from app.network import crypto
    with inviter.active():
        response = engine.admit_device(device_id=public["device_id"],
                                       signing_public=crypto.b64u_decode(public["signing_public"]),
                                       encryption_public=crypto.b64u_decode(public["encryption_public"]),
                                       name=name or joiner.name, platform="linux",
                                       legacy_instance_id=legacy_instance_id)
        manifest = engine.snapshot_manifest()
        chunks = []
        for collection in manifest["collections"]:
            offset = 0
            while True:
                chunk = engine.snapshot_chunk(collection, offset)
                chunks.append(chunk)
                if not chunk["more"]:
                    break
                offset += chunk["count"]
    with joiner.active():
        engine.enroll_from_response(response, device_name=name or joiner.name, legacy_instance_id=legacy_instance_id)
        for chunk in chunks:
            engine.apply_snapshot_chunk(chunk)
        engine.adopt_watermarks(manifest["watermarks"])
        engine.finish_bootstrap()


def set_owner_pref(worker, key, value):
    """Write an owner preference on ``worker`` and scan it into the outbox."""
    with worker.active():
        from app.core import owner_auth
        database.set_user_preference("admin", owner_auth.preference_identity(), key, value)
        engine.scan_all()


def owner_pref(worker, key):
    """Read an owner preference value on ``worker`` (None when unset)."""
    with worker.active():
        from app.core import owner_auth
        row = database.get_user_preference("admin", owner_auth.preference_identity(), key)
        return row["value"] if row else None
