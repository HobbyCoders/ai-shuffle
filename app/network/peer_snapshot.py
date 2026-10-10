"""Immutable, signed state transfers held temporarily on the serving worker.

No cloud storage is involved. One repeatable-read view binds all pages to a
frontier; the receiver advances that frontier only after every page commits.
The receiver keeps downloaded pages under its data dir until it applies them,
so a copy interrupted by a restart resumes where it stopped.

Formats (the manifest's ``format``, asked for with ``?format=``)
    1. Every collection, page bodies as plain JSON. What a worker on an older
       build asks for and reads.
    2. Every collection except transcript events (``deferred``), each page
       body zlib-compressed before it is encrypted (``encoding``). The
       recipient is ready once this small copy applies and fetches the
       transcripts afterwards (``app.network.transcripts``). An older
       publisher ignores the query and answers in format 1, which this build
       still applies.

Pages apply in bounded transactions, not one: a progress file next to the
pages lets an interrupted apply continue, and re-applying a page is a no-op
(last writer wins by clock). Watermarks are adopted, and the transcript
backfill registered, only in the last transaction.
"""
from __future__ import annotations

import contextvars
import copy
import functools
import json
import re
import shutil
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

from app.core.config import settings
from app.db import network as store
from app.network import crypto, engine, keyring, policy, transcripts

TTL = 900
MAX_PAGE = 8 * 1024 * 1024
FORMAT_FULL = 1
FORMAT_DEFERRED = 2
FORMATS = (FORMAT_FULL, FORMAT_DEFERRED)
#: Items per page: format 1 keeps what older recipients were sized for.
PAGE_ITEMS = {FORMAT_FULL: 100, FORMAT_DEFERRED: 500}
# A receiving transaction applies pages until it holds this many items (or
# the copy ends), then commits.
APPLY_BATCH_ITEMS = 5000
# A transfer stays downloadable for TTL plus a budget for its size. The signed
# expires_at is checked by this worker's page endpoint and by the recipient's
# apply, so a fixed TTL failed every large copy: a worker with millions of
# transcript events serves tens of thousands of pages (gigabytes), and a
# joiner restarted from page 0 each time the copy expired mid-download.
# The budget assumes a slow link: PAGE_SECONDS of latency per page plus
# BYTES_PER_SECOND of throughput. Reuse for a NEW download stays within TTL
# of capture (``_reusable``) so the extra time is left for downloading.
PAGE_SECONDS = 0.25
BYTES_PER_SECOND = 256 * 1024
MAX_LIFETIME = 24 * 3600
_lock = threading.Lock()
_jobs_lock = threading.Lock()
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="peer-capture")
_jobs: dict[tuple, Future] = {}


def _download_allowance(pages: list) -> int:
    """Seconds beyond TTL a finished transfer of these pages stays valid."""
    total = sum(int(page["bytes"]) for page in pages)
    return int(min(MAX_LIFETIME - TTL, len(pages) * PAGE_SECONDS + total / BYTES_PER_SECOND))


def _reusable(manifest: dict) -> bool:
    """A finished capture may start a new download only within TTL of capture."""
    return manifest["expires_at"] - _download_allowance(manifest["pages"]) > time.time()


def requested_format(value) -> int:
    """The copy format a recipient asked for: the newest this build serves, at most."""
    try:
        wanted = int(value or FORMAT_FULL)
    except (TypeError, ValueError):
        return FORMAT_FULL
    return max(f for f in FORMATS if f <= max(FORMAT_FULL, wanted))


def _refuse_partial() -> None:
    """A worker still filling in its own copy must not hand on a copy with gaps."""
    if engine.require_context().bootstrap_state != engine.BOOTSTRAP_READY:
        raise ValueError("This worker is still receiving its initial state")
    if transcripts.pending():
        raise ValueError("This worker is still receiving its transcripts")


def state(recipient: str, *, prepare: bool = False, fmt: int = FORMAT_FULL) -> dict:
    """Share a capture across requests, including ones whose client went away.

    New joiners opt in with ?prepare=true and poll without holding an HTTP
    request open. Old joiners get the original synchronous manifest shape,
    but wait on the SAME job; their timed-out retries do not restart capture.
    The executor is independent of the request's cancellation/asyncio loop.
    ``fmt`` is the format the recipient asked for (``requested_format``).
    """
    fmt = requested_format(fmt)
    while True:
        ctx = engine.require_context()
        member = ctx.member(recipient)
        _refuse_partial()
        if member is None or member.status != "active":
            raise ValueError("Peer is no longer a member")
        key = (str(settings.data_dir), recipient, ctx.network_id, ctx.generation, fmt)
        with _jobs_lock:
            for saved_key, job in list(_jobs.items()):
                if job.done() and (job.exception() is not None or not _reusable(job.result())):
                    # Preserve this request's error, then allow a later retry.
                    if saved_key == key and job.exception() is not None:
                        del _jobs[saved_key]
                        return job.result()
                    del _jobs[saved_key]
            job = _jobs.get(key)
            if job is None:
                context = contextvars.copy_context()
                work = capture if fmt == FORMAT_FULL else functools.partial(capture, fmt=fmt)
                job = _executor.submit(context.run, work, recipient)
                _jobs[key] = job
        if prepare and not job.done():
            return {"status": "preparing"}
        manifest = job.result()
        needs_snapshot = store.requires_snapshot(manifest["watermarks"], exclude_origin=recipient)
        # Both capture and the tail query can wait while membership changes.
        # Reacquire the live context AFTER those waits, immediately before
        # returning a wrapped keyring; do not hold membership/job locks over DB.
        current = engine.require_context()
        member = current.member(recipient)
        _refuse_partial()
        if member is None or member.status != "active":
            raise ValueError("Peer is no longer a member")
        if (manifest["network_id"] != current.network_id or manifest["generation"] != current.generation
                or manifest["recipient"] != recipient):
            raise ValueError("Membership changed during peer capture; retry")
        if not needs_snapshot:
            return manifest
        # Pruning or adopting another worker's snapshot can advance our head
        # without retaining its ops. In that case this frontier cannot catch up
        # by exchange: discard it and share one fresh capture, even within TTL.
        with _jobs_lock:
            if _jobs.get(key) is job:
                del _jobs[key]


def _root() -> Path:
    root = Path(settings.data_dir) / "peer-transfers"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _directory(transfer: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{32}", transfer):
        raise ValueError("Invalid peer transfer")
    return _root() / transfer


def _remove(directory: Path) -> None:
    root = _root().resolve()
    if directory.is_symlink() or directory.resolve().parent != root or not re.fullmatch(r"[a-f0-9]{32}", directory.name):
        raise ValueError("Invalid transfer cleanup path")
    shutil.rmtree(directory)


def _transfer_dirs():
    for entry in _root().iterdir():
        if entry.is_dir() and not entry.is_symlink() and re.fullmatch(r"[a-f0-9]{32}", entry.name):
            yield entry


def _saved_manifest(directory: Path) -> dict | None:
    try:
        return json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _expired(directory: Path, manifest: dict | None) -> bool:
    """A finished transfer lives until its signed expiry, not TTL after it was written.

    A directory without a readable manifest is an abandoned capture: callers
    hold ``_lock``, so no capture is writing it now.
    """
    try:
        return manifest["expires_at"] <= time.time()
    except (KeyError, TypeError):
        return time.time() - directory.stat().st_mtime > TTL


def cleanup() -> None:
    with _lock:
        for entry in _transfer_dirs():
            if _expired(entry, _saved_manifest(entry)):
                _remove(entry)
    # A partial copy this worker received can no longer finish past expiry.
    _cleanup_received()


def capture(recipient: str, fmt: int = FORMAT_FULL) -> dict:
    fmt = requested_format(fmt)
    deferred = sorted(transcripts.DEFERRED_COLLECTIONS) if fmt >= FORMAT_DEFERRED else []
    encoding = "zlib" if fmt >= FORMAT_DEFERRED else None
    with _lock:
        ctx = engine.require_context()
        _refuse_partial()
        member = ctx.member(recipient)
        if member is None or member.status != "active":
            raise ValueError("Peer is no longer a member")
        # Expired transfers contain only encrypted pages. Never touch user files.
        for entry in _transfer_dirs():
            manifest = _saved_manifest(entry)
            if _expired(entry, manifest):
                _remove(entry)
            elif (manifest is not None and manifest["recipient"] == recipient
                    and manifest.get("format", FORMAT_FULL) == fmt
                    and manifest["network_id"] == ctx.network_id
                    and manifest["generation"] == ctx.generation and _reusable(manifest)
                    and not store.requires_snapshot(manifest["watermarks"], exclude_origin=recipient)):
                # Ordinary writes may advance the frontier: reuse it while its
                # tail is retained. Snapshot adoption or pruning can leave gaps
                # that require recapture.
                return manifest
        engine.scan_all()
        transfer = uuid.uuid4().hex
        directory = _directory(transfer)
        directory.mkdir()
        try:
            with store.get_db() as conn:
                cursor = conn.cursor()
                # MVCC binds rows and watermarks to one view without holding the
                # network's writer lock while serializing a large transcript.
                cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                # In this view, not from the cache: a copy registered a moment
                # ago may still be committing its transcript backfill.
                if transcripts.has_backfill(cursor):
                    raise ValueError("This worker is still receiving its transcripts")
                ctx = engine.load_context(conn, publish=False)
                member = ctx.member(recipient)
                if member is None or member.status != "active":
                    raise ValueError("Peer is no longer a member")
                manifest = {"format": fmt, "transfer": transfer, "network_id": ctx.network_id,
                            "publisher": ctx.device_id, "recipient": recipient, "generation": ctx.generation,
                            "expires_at": int(time.time()) + TTL, "watermarks": store.local_watermarks(conn),
                            "keyring": keyring.for_recipient(ctx, member.encryption_public), "pages": []}
                if fmt >= FORMAT_DEFERRED:
                    # Left out here; the recipient fetches them once it is ready.
                    manifest["deferred"] = deferred
                    manifest["encoding"] = encoding
                # Retired collections go last as one empty page each: a recipient
                # on an older build requires every collection it knows.
                names = [c.name for c in policy.ordered_collections() if c.name not in deferred]
                names += sorted(policy.RETIRED_COLLECTIONS)
                for name in names:
                    offset, limit, after_record_id = 0, PAGE_ITEMS[fmt], None
                    while True:
                        try:
                            chunk, next_record_id = engine.snapshot_page(
                                name, offset, after_record_id=after_record_id, ctx=ctx, limit=limit, _conn=conn,
                                encoding=encoding)
                        except engine.SnapshotBodyTooLarge:
                            # Compresses under MAX_PAGE, but the recipient also
                            # caps what a page expands to.
                            if limit == 1:
                                raise ValueError("A sync record exceeds the peer transfer limit") from None
                            limit = max(1, limit // 2)
                            continue
                        raw = crypto.canonical_json(chunk)
                        if len(raw) > MAX_PAGE:
                            if limit == 1:
                                raise ValueError("A sync record exceeds the peer transfer limit")
                            limit = max(1, limit // 2)
                            continue
                        index = len(manifest["pages"])
                        (directory / f"{index}.json").write_bytes(raw)
                        manifest["pages"].append({"sha256": crypto.sha256_hex(raw), "bytes": len(raw),
                                                  "collection": name, "offset": offset,
                                                  "count": chunk["count"], "more": chunk["more"]})
                        offset += chunk["count"]
                        after_record_id = next_record_id
                        if not chunk["more"]:
                            break
                # Give the recipient the full download window after capture,
                # sized to the transfer so a large copy can finish.
                manifest["expires_at"] = int(time.time()) + TTL + _download_allowance(manifest["pages"])
                manifest["signature"] = crypto.b64u_encode(crypto.sign(ctx.identity.signing_private,
                                                                       crypto.canonical_json(manifest)))
                (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            return manifest
        except BaseException:
            _remove(directory)
            raise


def page(recipient: str, transfer: str, index: int) -> bytes:
    directory = _directory(transfer)
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    if (manifest["recipient"] != recipient or manifest["expires_at"] <= time.time()
            or manifest["network_id"] != engine.require_context().network_id
            or manifest["generation"] != engine.require_context().generation
            or not 0 <= index < len(manifest["pages"])):
        raise ValueError("Peer transfer expired or does not belong to this device")
    return (directory / f"{index}.json").read_bytes()


def verify_manifest(manifest: dict, publisher: str):
    ctx = copy.deepcopy(engine.require_context())
    member = ctx.member(publisher)
    unsigned = {k: v for k, v in manifest.items() if k != "signature"}
    if manifest.get("network_id") != ctx.network_id:
        raise ValueError("Peer state belongs to a different network")
    if manifest.get("recipient") != ctx.device_id or manifest.get("publisher") != publisher:
        raise ValueError("Peer state belongs to a different device")
    fmt = manifest.get("format")
    if fmt == FORMAT_DEFERRED and (manifest.get("encoding") != "zlib" or not isinstance(manifest.get("deferred"), list)
                                   or not set(manifest["deferred"]) <= set(transcripts.DEFERRED_COLLECTIONS)):
        raise ValueError("Peer state manifest did not verify")
    if (member is None or member.status != "active" or fmt not in FORMATS
            or manifest.get("expires_at", 0) <= time.time()
            or not re.fullmatch(r"[a-f0-9]{32}", manifest.get("transfer", ""))
            or not crypto.verify(member.signing_public, crypto.canonical_json(unsigned),
                                 crypto.b64u_decode(manifest.get("signature", "")))):
        raise ValueError("Peer state manifest did not verify")
    for label, wrapped in manifest["keyring"].items():
        if label.startswith("@"):
            continue
        key = crypto.unwrap_key(ctx.identity.encryption_private, wrapped)
        version = int(label.split("@")[0])
        digest = crypto.sha256_hex(key)
        if "@" in label and digest != label.split("@", 1)[1]:
            raise ValueError("Peer state key did not verify")
        ctx.key_candidates.setdefault(version, {})[digest] = key
        ctx.keyring.setdefault(version, key)
    return ctx


# ---------------------------------------------------------------------------
# Receiving side: a copy that survives restarts
# ---------------------------------------------------------------------------
# A large first copy runs for many minutes; a restart used to throw away every
# downloaded page and start over, usually against a new capture. Pages now go
# to one directory per transfer under the data dir. A retry offers the saved
# transfer to its publisher again: the publisher serves it until its signed
# expires_at (TTL plus the size allowance), and pages already on disk are
# kept after their hash is checked against the signed manifest. Only one
# partial copy is kept: starting another transfer deletes the rest.
_receive_lock = threading.Lock()
# Pages whose hash was checked in this process, by transfer.
_verified: dict[str, set[int]] = {}


def _received_root() -> Path:
    return Path(settings.data_dir) / "peer-receive"


def _received_directory(transfer: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{32}", transfer):
        raise ValueError("Invalid peer transfer")
    return _received_root() / transfer


def _received_dirs():
    root = _received_root()
    if not root.is_dir():
        return
    for entry in root.iterdir():
        if entry.is_dir() and not entry.is_symlink() and re.fullmatch(r"[a-f0-9]{32}", entry.name):
            yield entry


def _remove_received(directory: Path) -> None:
    root = _received_root().resolve()
    if directory.is_symlink() or directory.resolve().parent != root or not re.fullmatch(r"[a-f0-9]{32}", directory.name):
        raise ValueError("Invalid transfer cleanup path")
    _verified.pop(directory.name, None)
    shutil.rmtree(directory, ignore_errors=True)


def saved_publishers() -> list[str]:
    """Peers this worker holds a partial copy from; a retry asks them first."""
    with _receive_lock:
        return [m["publisher"] for m in map(_saved_manifest, _received_dirs())
                if isinstance(m, dict) and isinstance(m.get("publisher"), str)]


def saved_transfer(publisher: str) -> dict | None:
    """A partial copy from ``publisher`` that can still finish, or None.

    Copies that can no longer finish (expired, another network or member
    change, unreadable) are deleted here.
    """
    with _receive_lock:
        for entry in list(_received_dirs()):
            manifest = _saved_manifest(entry)
            if manifest is not None and manifest.get("publisher") != publisher:
                continue
            try:
                if manifest is None or manifest.get("transfer") != entry.name:
                    raise ValueError("Unreadable saved transfer")
                verify_manifest(manifest, publisher)
            except Exception:  # noqa: BLE001 — anything that does not verify is discarded
                _remove_received(entry)
                continue
            return manifest
    return None


def begin_received(manifest: dict) -> Path:
    """The directory for a verified manifest's pages, keeping pages of the same transfer."""
    with _receive_lock:
        directory = _received_directory(manifest["transfer"])
        for entry in list(_received_dirs()):
            if entry.name != directory.name or _saved_manifest(entry) != manifest:
                # Another transfer, or the same id with other content: not
                # this copy. One partial copy is enough disk (gigabytes).
                _remove_received(entry)
        if not (directory / "manifest.json").exists():
            directory.mkdir(parents=True, exist_ok=True)
            part = directory / f".manifest.{uuid.uuid4().hex}.part"
            part.write_text(json.dumps(manifest), encoding="utf-8")
            part.replace(directory / "manifest.json")
        return directory


def has_page(directory: Path, index: int, expected: dict) -> bool:
    """True when page ``index`` is on disk and matches the signed manifest's size and hash."""
    verified = _verified.setdefault(directory.name, set())
    path = directory / f"{index}.json"
    if index in verified and path.exists():
        return True
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return False
    if len(raw) == expected["bytes"] and crypto.sha256_hex(raw) == expected["sha256"]:
        verified.add(index)
        return True
    path.unlink(missing_ok=True)
    return False


def save_page(directory: Path, index: int, raw: bytes, expected: dict) -> None:
    """Store a downloaded page once it matches the signed manifest; a partial write never counts."""
    if len(raw) != expected["bytes"] or crypto.sha256_hex(raw) != expected["sha256"]:
        raise ValueError("Peer state page did not verify")
    part = directory / f".{index}.{uuid.uuid4().hex}.part"
    part.write_bytes(raw)
    part.replace(directory / f"{index}.json")
    _verified.setdefault(directory.name, set()).add(index)


def apply_received(manifest: dict, publisher: str, *, on_progress=None) -> int:
    """Apply a fully downloaded copy, then delete it. A copy that fails to verify is deleted too."""
    with _receive_lock:
        directory = _received_directory(manifest["transfer"])
        if not (directory / "manifest.json").exists():
            raise ValueError("Peer state copy is no longer on disk")
        try:
            applied = apply(manifest, directory, publisher, on_progress=on_progress)
        except ValueError:
            _remove_received(directory)
            raise
        _remove_received(directory)
        return applied


def discard_received(transfer: str | None = None) -> None:
    """Delete one saved partial copy, or all of them (``None``: the worker left its network)."""
    with _receive_lock:
        for entry in list(_received_dirs()):
            if transfer is None or entry.name == transfer:
                _remove_received(entry)


def _cleanup_received() -> None:
    with _receive_lock:
        for entry in list(_received_dirs()):
            if _expired(entry, _saved_manifest(entry)):
                _remove_received(entry)


def _progress_path(directory: Path) -> Path:
    return directory / "applied.json"


def _applied_pages(directory: Path, manifest: dict) -> int:
    """Pages of this transfer already committed by an earlier, interrupted apply."""
    try:
        saved = json.loads(_progress_path(directory).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    if not isinstance(saved, dict) or saved.get("transfer") != manifest.get("transfer"):
        return 0
    pages = saved.get("pages")
    return pages if type(pages) is int and 0 <= pages <= len(manifest["pages"]) else 0


def _save_applied(directory: Path, manifest: dict, pages: int) -> None:
    try:
        part = directory / f".applied.{uuid.uuid4().hex}.part"
        part.write_text(json.dumps({"transfer": manifest["transfer"], "pages": pages}), encoding="utf-8")
        part.replace(_progress_path(directory))
    except OSError:
        # Only costs re-applying committed pages after a restart (a no-op).
        pass


def _read_page(directory: Path, index: int, expected: dict) -> dict:
    raw = (directory / f"{index}.json").read_bytes()
    if len(raw) != expected["bytes"] or crypto.sha256_hex(raw) != expected["sha256"]:
        raise ValueError("Peer state page did not verify")
    return json.loads(raw)


def apply(manifest: dict, directory: Path, publisher: str, *, on_progress=None) -> int:
    """Apply a downloaded copy; ``on_progress(pages applied, pages in all)`` after each commit."""
    trusted = verify_manifest(manifest, publisher)
    deferred = set(manifest.get("deferred") or ()) if manifest.get("format") == FORMAT_DEFERRED else set()
    pages = manifest["pages"]
    # Preflight every immutable page before touching records or watermarks.
    offsets, finished = {}, set()
    for index, expected in enumerate(pages):
        chunk = _read_page(directory, index, expected)
        body = engine.open_snapshot_chunk(chunk, ctx=trusted)
        collection = body["collection"]
        if (body["device_id"] != publisher or collection in finished or collection in deferred
                or body["offset"] != offsets.get(collection, 0)
                or any(chunk[key] != expected[key] for key in ("collection", "offset", "count", "more"))
                or len(body["items"]) != expected["count"]):
            raise ValueError("Peer state pages are incomplete")
        offsets[collection] = body["offset"] + len(body["items"])
        if not body["more"]:
            finished.add(collection)
    # Pages for collections this build does not know (retired here, or newer)
    # are verified above and applied as nothing. Deferred ones come later.
    if not set(policy.COLLECTIONS) - policy.SNAPSHOT_OPTIONAL_COLLECTIONS - deferred <= finished:
        raise ValueError("Peer state does not include every collection")
    applied = 0
    index = _applied_pages(directory, manifest)
    first = True
    queued = 0
    while first or index < len(pages):
        with store.get_db() as conn:
            cursor = conn.cursor()
            state = store.lock_state(cursor)
            current = engine.load_context(conn, publish=False)
            member = current.member(publisher) if current is not None else None
            if (state is None or state["network_id"] != trusted.network_id
                    or member is None or member.status != "active"):
                raise ValueError("Membership changed during peer transfer")
            if first:
                # Context mutations stay private until the copy is applied.
                trusted.members = copy.deepcopy(current.members)
                trusted.generation = current.generation
            items = 0
            while index < len(pages) and (items == 0 or items + int(pages[index]["count"]) <= APPLY_BATCH_ITEMS):
                chunk = _read_page(directory, index, pages[index])
                applied += engine.apply_snapshot_chunk(chunk, ctx=trusted, _conn=conn)
                items += int(pages[index]["count"]) or 1
                index += 1
            if index >= len(pages):
                # Only once every page is in: the frontier, and what is still owed.
                engine.adopt_watermarks(manifest["watermarks"], ctx=trusted, _conn=conn)
                if deferred:
                    queued = transcripts.register(cursor, publisher=publisher, floor=manifest["watermarks"],
                                                  ctx=trusted)
        first = False
        if queued:
            # Committed: let the cache see it before anything else runs.
            transcripts.after_commit()
        _save_applied(directory, manifest, index)
        if on_progress is not None:
            on_progress(index, len(pages))
    engine.load_context()
    engine._notify([(c, "", "upsert") for c in finished if c in policy.COLLECTIONS])
    return applied
