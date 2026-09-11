"""Immutable, signed state transfers held temporarily on the serving worker.

No cloud storage is involved. One repeatable-read view binds all pages to a
frontier; the receiver advances that frontier only after every page commits.
"""
from __future__ import annotations

import copy
import json
import re
import shutil
import threading
import time
import uuid
from pathlib import Path

from app.core.config import settings
from app.db import network as store
from app.network import crypto, engine, keyring, policy

TTL = 900
MAX_PAGE = 8 * 1024 * 1024
_lock = threading.Lock()


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


def cleanup() -> None:
    with _lock:
        for entry in _root().iterdir():
            if (entry.is_dir() and not entry.is_symlink() and re.fullmatch(r"[a-f0-9]{32}", entry.name)
                    and time.time() - entry.stat().st_mtime > TTL):
                _remove(entry)


def capture(recipient: str) -> dict:
    with _lock:
        ctx = engine.require_context()
        if ctx.bootstrap_state != engine.BOOTSTRAP_READY:
            raise ValueError("This worker is still receiving its initial state")
        member = ctx.member(recipient)
        if member is None or member.status != "active":
            raise ValueError("Peer is no longer a member")
        # Expired transfers contain only encrypted pages. Never touch user files.
        for entry in _root().iterdir():
            if entry.is_dir() and not entry.is_symlink() and re.fullmatch(r"[a-f0-9]{32}", entry.name):
                if time.time() - entry.stat().st_mtime > TTL:
                    _remove(entry)
                else:
                    saved = entry / "manifest.json"
                    if saved.exists():
                        manifest = json.loads(saved.read_text())
                        if (manifest["recipient"] == recipient and manifest["network_id"] == ctx.network_id
                                and manifest["generation"] == ctx.generation
                                and manifest["watermarks"] == store.local_watermarks()):
                            return manifest
        engine.scan_all()
        transfer = uuid.uuid4().hex
        directory = _directory(transfer)
        directory.mkdir()
        try:
            with store.get_db() as conn:
                cursor = conn.cursor()
                cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                store.lock_state(cursor)
                ctx = copy.deepcopy(engine.load_context(conn))
                member = ctx.member(recipient)
                if member is None or member.status != "active":
                    raise ValueError("Peer is no longer a member")
                manifest = {"format": 1, "transfer": transfer, "network_id": ctx.network_id,
                            "publisher": ctx.device_id, "recipient": recipient, "generation": ctx.generation,
                            "expires_at": int(time.time()) + TTL, "watermarks": store.local_watermarks(conn),
                            "keyring": keyring.for_recipient(ctx, member.encryption_public), "pages": []}
                for coll in policy.ordered_collections():
                    offset, limit = 0, 100
                    while True:
                        chunk = engine.snapshot_chunk(coll.name, offset, ctx=ctx, limit=limit, _conn=conn)
                        raw = crypto.canonical_json(chunk)
                        if len(raw) > MAX_PAGE:
                            if limit == 1:
                                raise ValueError("A sync record exceeds the peer transfer limit")
                            limit = max(1, limit // 2)
                            continue
                        index = len(manifest["pages"])
                        (directory / f"{index}.json").write_bytes(raw)
                        manifest["pages"].append({"sha256": crypto.sha256_hex(raw), "bytes": len(raw),
                                                  "collection": coll.name, "offset": offset,
                                                  "count": chunk["count"], "more": chunk["more"]})
                        offset += chunk["count"]
                        if not chunk["more"]:
                            break
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
    if (member is None or member.status != "active" or manifest.get("format") != 1
            or manifest.get("network_id") != ctx.network_id or manifest.get("recipient") != ctx.device_id
            or manifest.get("publisher") != publisher or manifest.get("expires_at", 0) <= time.time()
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


def apply(manifest: dict, directory: Path, publisher: str) -> int:
    trusted = verify_manifest(manifest, publisher)
    # Preflight every immutable page before touching records or watermarks.
    offsets, finished = {}, set()
    for index, expected in enumerate(manifest["pages"]):
        raw = (directory / f"{index}.json").read_bytes()
        if len(raw) != expected["bytes"] or crypto.sha256_hex(raw) != expected["sha256"]:
            raise ValueError("Peer state page did not verify")
        chunk = json.loads(raw)
        body = engine.open_snapshot_chunk(chunk, ctx=trusted)
        collection = body["collection"]
        if (body["device_id"] != publisher or collection in finished
                or body["offset"] != offsets.get(collection, 0)
                or any(chunk[key] != expected[key] for key in ("collection", "offset", "count", "more"))
                or len(body["items"]) != expected["count"]):
            raise ValueError("Peer state pages are incomplete")
        offsets[collection] = body["offset"] + len(body["items"])
        if not body["more"]:
            finished.add(collection)
    if finished != set(policy.COLLECTIONS):
        raise ValueError("Peer state does not include every collection")
    applied = 0
    with store.get_db() as conn:
        state = store.lock_state(conn.cursor())
        current = engine.load_context(conn)
        if state["network_id"] != trusted.network_id or current.member(publisher).status != "active":
            raise ValueError("Membership changed during peer transfer")
        # Context mutations stay private until this database transaction commits.
        trusted.members = copy.deepcopy(current.members)
        trusted.generation = current.generation
        for index in range(len(manifest["pages"])):
            chunk = json.loads((directory / f"{index}.json").read_bytes())
            applied += engine.apply_snapshot_chunk(chunk, ctx=trusted, _conn=conn)
        engine.adopt_watermarks(manifest["watermarks"], ctx=trusted, _conn=conn)
        if applied:
            updated = store.get_state(conn)
            hub_state = dict(updated.get("hub") or {})
            hub_state["peer_state_revision"] = int(hub_state.get("peer_state_revision", 0)) + 1
            store.update_state(_conn=conn, hub=hub_state)
    engine.load_context()
    engine._notify([(c, "", "upsert") for c in finished])
    return applied
