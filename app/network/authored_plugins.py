"""Agent-authored plugin drafts and their approvals, shared between devices.

``save_plugin`` writes a draft into the plugin folder of the device that ran
the session, while the owner reviews drafts in Settings on whichever device
they have open. Saved skills have the same split and travel as records in the
``memory`` collection (see :mod:`app.network.memory_records`); drafts do too,
so every device lists every draft and the owner can approve it anywhere.

Records in ``network_memory_records``:

``authored-plugin:<name>@<version>`` (kind ``authored_plugin``)
    The draft's files and the content hash they make. Published from the
    files on disk. A device that receives one writes it only after the same
    checks ``save_plugin`` applies, never over an existing version, and only
    when the files hash to the recorded value. A tombstone (``deleted``) is
    written only by an uninstall, never because a device lacks the folder.

``authored-approval:<name>`` (kind ``authored_approval``)
    The owner's approval: the version and content hash they reviewed. Written
    when the owner enables the plugin, tombstoned when they disable or
    uninstall it. A device turns the plugin on only when its own copy of that
    version hashes the same, so an approval never covers content the owner
    did not see. A tombstone turns the plugin off everywhere.
"""
from __future__ import annotations

import hashlib
import json
import logging
import shutil
from typing import Any, Dict, Iterable, List, Mapping, Optional

from app.db.base import get_db

log = logging.getLogger(__name__)

DRAFT_PREFIX = "authored-plugin:"
APPROVAL_PREFIX = "authored-approval:"
DRAFT_KIND = "authored_plugin"
APPROVAL_KIND = "authored_approval"


def _service():
    from app.core.plugin_service import get_plugin_service
    return get_plugin_service()


def _hash(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


def _networked() -> bool:
    from app.network import engine
    return engine.context() is not None


def is_record(record_id: str) -> bool:
    return record_id.startswith((DRAFT_PREFIX, APPROVAL_PREFIX))


def _upsert(cursor, rid: str, kind: str, content: str, *, deleted: bool = False) -> None:
    cursor.execute(
        "INSERT INTO network_memory_records (id, kind, scope, content, content_hash, deleted) "
        "VALUES (%s, %s, 'global', %s, %s, %s) ON CONFLICT (id) DO UPDATE SET "
        "kind=EXCLUDED.kind, scope=EXCLUDED.scope, content=EXCLUDED.content, "
        "content_hash=EXCLUDED.content_hash, deleted=EXCLUDED.deleted, updated_at=NOW()",
        (rid, kind, content, _hash(content), deleted),
    )


def _publish(ids: Iterable[str]) -> None:
    from app.network import engine
    for rid in ids:
        engine.record_after_commit("memory", rid)


# ─── publishing ──────────────────────────────────────────────────────────────


def _local_drafts(svc) -> Dict[str, str]:
    """``{record id: content}`` for every draft version on this device."""
    from fluid.context.plugin_authoring import read_draft_files
    from fluid.context.plugin_formats import content_hash
    from app.core import plugin_authored

    base = plugin_authored._base(svc)
    out: Dict[str, str] = {}
    if not base.is_dir():
        return out
    for folder in sorted(base.iterdir()):
        if not folder.is_dir() or folder.is_symlink() or not plugin_authored._plain(folder.name):
            continue
        for version_dir in sorted(folder.iterdir()):
            if not version_dir.is_dir() or version_dir.is_symlink():
                continue
            files = read_draft_files(version_dir)
            digest = content_hash(version_dir)
            if files is None or digest is None:
                continue
            out[f"{DRAFT_PREFIX}{folder.name}@{version_dir.name}"] = json.dumps(
                {"name": folder.name, "version": version_dir.name, "hash": digest, "files": files},
                sort_keys=True,
            )
    return out


def refresh_from_files(cursor) -> int:
    """Write this device's drafts into the memory table; return how many changed.

    A record already holding the same content stays as it is, a tombstone
    included: a draft uninstalled elsewhere is not brought back because this
    device has not removed its copy yet.
    """
    try:
        drafts = _local_drafts(_service())
    except OSError:
        log.warning("Authored plugin drafts could not be read", exc_info=True)
        return 0
    if not drafts:
        return 0
    cursor.execute(
        "SELECT id, content_hash FROM network_memory_records WHERE id LIKE %s", (DRAFT_PREFIX + "%",)
    )
    known = {r["id"]: r["content_hash"] for r in cursor.fetchall()}
    changed = 0
    for rid, content in drafts.items():
        if known.get(rid) == _hash(content):
            continue
        _upsert(cursor, rid, DRAFT_KIND, content)
        changed += 1
    return changed


def share_approval(plugin_id: str, version: str, digest: str) -> None:
    """Record the owner's approval of ``version`` (hash ``digest``) for every device."""
    if not _networked():
        return
    name = plugin_id.rpartition("@")[0]
    rid = f"{APPROVAL_PREFIX}{name}"
    content = json.dumps({"plugin_id": plugin_id, "version": version, "hash": digest}, sort_keys=True)
    with get_db() as conn:
        _upsert(conn.cursor(), rid, APPROVAL_KIND, content)
    _publish([rid])


def withdraw(plugin_id: str, *, uninstall: bool = False) -> None:
    """Withdraw the approval on every device; with ``uninstall``, every draft version too."""
    if not _networked():
        return
    name = plugin_id.rpartition("@")[0]
    ids = [f"{APPROVAL_PREFIX}{name}"]
    with get_db() as conn:
        cursor = conn.cursor()
        _upsert(cursor, ids[0], APPROVAL_KIND, json.dumps({"plugin_id": plugin_id}), deleted=True)
        if uninstall:
            cursor.execute(
                "SELECT id FROM network_memory_records WHERE id LIKE %s AND deleted = FALSE",
                (f"{DRAFT_PREFIX}{name}@%",),
            )
            for row in cursor.fetchall():
                cursor.execute(
                    "UPDATE network_memory_records SET deleted = TRUE, updated_at = NOW() WHERE id = %s",
                    (row["id"],),
                )
                ids.append(row["id"])
    _publish(ids)


# ─── applying ────────────────────────────────────────────────────────────────


def _content(record: Mapping[str, Any]) -> Dict[str, Any]:
    try:
        value = json.loads(record.get("content") or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _apply_draft(svc, record: Mapping[str, Any]) -> Optional[str]:
    """Write or remove one draft version; return the plugin name it touched."""
    from fluid.context.plugin_authoring import PluginDraftError, install_draft_files
    from fluid.context.plugin_formats import content_hash
    from app.core import plugin_authored

    name, _, version = record["id"].removeprefix(DRAFT_PREFIX).rpartition("@")
    body = _content(record)
    if body.get("name") != name or body.get("version") != version or not plugin_authored._plain(name):
        log.warning("Authored plugin record %s does not match its id", record["id"])
        return None
    folder = plugin_authored._base(svc) / name / version
    if record.get("deleted"):
        # Remove only the content that was uninstalled, never a different
        # local draft that happens to share the version number.
        if folder.is_dir() and not folder.is_symlink() and content_hash(folder) == body.get("hash"):
            if len([d for d in folder.parent.iterdir() if d.is_dir()]) <= 1:
                plugin_authored.uninstall(svc, f"{name}@{plugin_authored.AUTHORED_SOURCE}", share=False)
            else:
                shutil.rmtree(folder, ignore_errors=True)
        return name
    try:
        install_draft_files(name, version, body.get("files"), str(body.get("hash") or ""), home=svc.PLUGINS_DIR)
    except PluginDraftError as exc:
        log.warning("Refused authored plugin draft %s: %s", record["id"], exc)
        return None
    return name


def _apply_approval(svc, record: Mapping[str, Any]) -> None:
    from app.core import plugin_authored

    name = record["id"].removeprefix(APPROVAL_PREFIX)
    if not plugin_authored._plain(name):
        return
    plugin_id = f"{name}@{plugin_authored.AUTHORED_SOURCE}"
    if record.get("deleted"):
        if plugin_authored._latest(svc, name) is not None:
            plugin_authored.disable(svc, plugin_id, share=False)
        return
    body = _content(record)
    root = plugin_authored._latest(svc, name)
    if root is None or root.name != body.get("version"):
        return  # not here yet, or this device has another latest version
    try:
        plugin_authored.enable(svc, plugin_id, str(body.get("hash") or ""), share=False)
    except ValueError as exc:
        # This device's copy is not what the owner reviewed: it stays off.
        log.warning("Approval for %s not applied: %s", plugin_id, exc)


def apply_records(records: List[Mapping[str, Any]]) -> int:
    """Apply received draft and approval records; drafts first, then approvals."""
    if not records:
        return 0
    svc = _service()
    applied = 0
    touched = set()
    approvals = {}
    for record in records:
        rid = record["id"]
        try:
            if rid.startswith(DRAFT_PREFIX):
                name = _apply_draft(svc, record)
                if name:
                    touched.add(name)
                    applied += 1
            elif rid.startswith(APPROVAL_PREFIX):
                approvals[rid] = record
        except (ValueError, KeyError, TypeError, OSError):
            log.exception("Authored plugin record rejected: %s", rid)
    # A draft that just arrived may already have an approval waiting for it.
    pending = [f"{APPROVAL_PREFIX}{name}" for name in touched if f"{APPROVAL_PREFIX}{name}" not in approvals]
    if pending:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM network_memory_records WHERE id IN (%s)" % ",".join(["%s"] * len(pending)),
                tuple(pending),
            )
            for row in cursor.fetchall():
                approvals[row["id"]] = dict(row)
    for rid, record in approvals.items():
        try:
            _apply_approval(svc, record)
            applied += 1
        except (ValueError, KeyError, TypeError, OSError):
            log.exception("Authored plugin approval rejected: %s", rid)
    return applied
