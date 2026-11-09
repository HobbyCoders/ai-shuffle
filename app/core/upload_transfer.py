"""Cross-install upload delivery — lift at send, materialize at spawn.

An upload is staged on the disk of whichever install served the upload endpoint.
When the turn that references it runs on ANOTHER install, that path means
nothing there. This module is the whole transport, and it is DB-mediated by
design: there is no device-to-device HTTP in this cluster (``base_url`` is
optional and guessed; the legacy relay was deleted and is not to be rebuilt).

Two halves:

* :func:`lift_paths_to_blobs` runs on the SENDING install, at send time. It
  validates each path resolves under the project's own directory (so a prompt
  can never make us read arbitrary files off disk), enforces the existing 50 MB
  upload cap, and writes the bytes into ``upload_blobs`` rows. Any failure
  raises :class:`UploadLiftError` — the caller FAILS THE SEND rather than
  letting an attachment silently vanish.

* :func:`materialize_blobs_for_turn` runs on the EXECUTING install, at spawn
  time. It writes each blob into ``<working_dir>/uploads/<filename>`` (with the
  same ``_1``/``_2`` collision suffixes ``_perform_project_upload`` uses), NULLs
  the bytes, and returns a ``{source_path: local_path}`` mapping that
  :func:`rewrite_prompt` applies to the prompt as exact-string replacement.
  A blob whose bytes are already gone AND whose file is missing raises rather
  than letting the turn run with a dead path.
"""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# The uploads sub-directory materialized blobs land in, matching where
# ``_perform_project_upload`` stages them on the serving install.
UPLOADS_SUBDIR = "uploads"


class UploadLiftError(Exception):
    """A staged upload could not be carried to the executing install.

    Raised by :func:`lift_paths_to_blobs` (send side) and
    :func:`materialize_blobs_for_turn` (spawn side). Both callers surface it —
    the send is refused, or the turn fails — because the alternative is a
    prompt referencing a file that does not exist where the agent will look.
    """


def _max_upload_size() -> int:
    """The existing 50 MB project-upload cap, imported rather than duplicated."""
    from app.api.projects import MAX_UPLOAD_SIZE

    return MAX_UPLOAD_SIZE


def _project_dir(project_row: Dict[str, Any]) -> Path:
    """The legacy project's working directory, resolved."""
    from app.core.project_utils import get_project_working_dir

    return Path(get_project_working_dir(project_row)).resolve()


def _is_under(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def session_upload_dir(session_id: str) -> Path:
    """Stable, path-safe staging root, isolated from every other session."""
    from app.core.config import settings
    key = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    return settings.effective_workspace_dir / "session-uploads" / key


def stage_session_upload(session_id: str, filename: str, content: bytes) -> Dict[str, Any]:
    """Write bytes into the session's staging area; the send lifts them from there."""
    from app.core.config import settings
    from app.core.confined_files import upload

    workspace = settings.effective_workspace_dir
    stage = session_upload_dir(session_id).relative_to(workspace).as_posix()
    destination = upload(workspace, stage, filename, content)
    return {
        "filename": destination.name,
        "path": destination.relative_to(workspace).as_posix(),
        "full_path": destination.as_posix(),
        "size": len(content),
    }


def resolve_upload_target(session: Dict[str, Any]) -> tuple[Optional[str], bool]:
    """Read-only admission shared by both upload routes and direct writers.

    Return (existing local cwd, needs per-session staging). A remote or new
    checkout may stage; an expected, registered local checkout must be usable,
    with the same diagnostic as Send. Never create a checkout while uploading.
    """
    from app.core import project_resolver
    from app.core.config import settings
    from app.core.placement import resolve_placement
    from app.core.query_options import worktree_unavailable_error
    from app.network.control import owner_of

    decision = resolve_placement(session)
    remote = owner_of(session) or (
        decision.kind == "runner" and decision.runner_id != settings.effective_instance_id
    )
    cwd = project_resolver.resolve_existing_cwd(session) if not remote else None
    bound = bool(session.get("branch") or session.get("worktree_branch")
                 or session.get("worktree_id") or session.get("worktree_path"))
    staging = bound and bool(remote or decision.kind == "none")
    if not remote and not cwd:
        error = worktree_unavailable_error(session)
        if error and project_resolver.resolve_local_checkout(session):
            from app.db import git_ops

            project = project_resolver.resolve_project_v2(session)
            branch = session.get("branch") or session.get("worktree_branch")
            # Send can still claim/wait for this durable checkout intent. Keep
            # staging until then, without running or changing the operation.
            if not branch or not git_ops.find_matching(
                settings.effective_instance_id, project["id"], "ensure_worktree", branch
            ):
                raise error
        staging = staging or bool(error)
    return cwd, staging


def stage_admitted_session_upload(session: Dict[str, Any], filename: str, data: bytes) -> Dict[str, Any]:
    """Recheck the read-only admission immediately before a staging write."""
    resolve_upload_target(session)
    return stage_session_upload(session["id"], filename, data)


def split_session_staged_paths(paths: List[str], session_id: str) -> tuple[List[str], List[str]]:
    """Separate this session's staged files from files in a project checkout."""
    root = session_upload_dir(session_id).resolve()
    staged: List[str] = []
    others: List[str] = []
    for raw in paths:
        (staged if _is_under(Path(raw).resolve(), root) else others).append(raw)
    return staged, others


def validate_upload_paths(paths: List[str], session: Dict[str, Any]) -> None:
    """Admit provenance only from this session's attachment staging areas.

    Reuse the lift path's resolved containment check, even for local sends that
    never lift bytes. A symlink or traversal must not turn arbitrary client text
    into a trusted attachment when the message is later restored.
    """
    from app.core.config import settings
    from app.core import project_resolver
    from app.core.session_modes import chat_uploads_dir, owner_chat_dir, runs_in_owner_chat_folder

    if not paths:
        return
    if not session or not session.get("id"):
        raise UploadLiftError("Attachment session is unavailable.")
    workspace = settings.effective_workspace_dir.resolve()
    roots = [(session_upload_dir(session["id"]), workspace)]
    if runs_in_owner_chat_folder(session):
        roots.append((owner_chat_dir() / UPLOADS_SUBDIR, workspace))
    if session.get("mode") == "chat":
        roots.append((chat_uploads_dir(), workspace))
    elif session.get("project_id") or session.get("project_v2_id"):
        cwd = project_resolver.resolve_existing_cwd(session)
        if cwd:
            base = Path(cwd).resolve()
            roots.append((base / UPLOADS_SUBDIR, base))
        # New Chat stages uploads in this project's base checkout before the
        # explicit branch exists. The subsequent WS send still names that copy.
        if session.get("project_id"):
            from app.db import database
            project = database.get_project(session["project_id"])
            if project:
                base = _project_dir(project)
                roots.append((base / UPLOADS_SUBDIR, base))
    allowed = [root.resolve() for root, base in roots
               if _is_under(root.resolve(), base)]
    for raw in paths:
        path = Path(raw)
        if not path.is_absolute() or not any(_is_under(path.resolve(), root) for root in allowed):
            raise UploadLiftError("Attachment path is outside this session's upload area.")


def upload_owner_id(owner: Optional[str] = None) -> str:
    """Canonical worker identity across legacy instance ids and network aliases."""
    from app.core.config import settings
    from app.network import runners
    owner = owner or settings.effective_instance_id
    return runners.resolve_runner_id(owner) or owner


def _recorded_upload_events(session_id: str, event_id: Optional[str] = None) -> List[Dict[str, Any]]:
    from app.db.base import get_db, rows_to_list
    from app.db.session_events import _parse_payload
    with get_db() as conn:
        cursor = conn.cursor()
        sql = "SELECT id, payload FROM session_events WHERE session_id=%s AND event_type='message.user'"
        args = [session_id]
        if event_id is not None:
            sql += " AND id=%s"
            args.append(event_id)
        cursor.execute(sql + " ORDER BY seq DESC", args)
        rows = rows_to_list(cursor.fetchall())
    for row in rows:
        row["payload"] = _parse_payload(row.get("payload"))
    return rows


def recorded_upload_references(session_id: str, event: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Mint explicit restore identities from a recorded user event, not a path guess."""
    from app.db import database
    metadata = event.get("payload") or {}
    owner = upload_owner_id(metadata.get("upload_owner_id") or (database.get_session(session_id) or {}).get("instance_id"))
    paths = metadata.get("upload_paths") or []
    if not isinstance(paths, list):
        return []
    return [{"owner_id": owner, "event_id": event["id"], "index": index, "path": path}
            for index, path in enumerate(paths) if isinstance(path, str) and path]


def partition_upload_paths(paths: List[str], session: Dict[str, Any], *,
                           references: Optional[List[Dict[str, Any]]] = None) -> tuple[List[str], List[Dict[str, Any]]]:
    """Fresh paths stay fresh; only explicit owner/event/index chips are restores."""
    if not session or not session.get("id"):
        raise UploadLiftError("Attachment session is unavailable.")
    validate_upload_paths(paths, session)
    owner = upload_owner_id(session.get("instance_id"))
    checked = []
    if not isinstance(references or [], list):
        raise UploadLiftError("Invalid attachment references.")
    for ref in references or []:
        if not isinstance(ref, dict) or not isinstance(ref.get("owner_id"), str) or upload_owner_id(ref["owner_id"]) != owner:
            raise UploadLiftError("Attachment reference belongs to another owner.")
        events = _recorded_upload_events(session["id"], str(ref.get("event_id") or ""))
        allowed = recorded_upload_references(session["id"], events[0]) if events else []
        if type(ref.get("index")) is not int or ref not in allowed:
            raise UploadLiftError("Attachment reference is not recorded in this session.")
        if ref["path"] in paths:
            raise UploadLiftError("Fresh and restored attachments share a path; reattach them separately.")
        checked.append(ref)
    return list(paths), checked


def resolve_upload_references(payload: Dict[str, Any], session: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve an event/index identity on its owner, never trust a remote path."""
    from app.db import upload_blobs
    resolved = dict(payload)
    blob_ids = list(payload.get("upload_blob_ids") or [])
    paths = list(payload.get("upload_paths") or [])
    for ref in payload.get("upload_references") or []:
        if (not isinstance(ref, dict) or not isinstance(ref.get("owner_id"), str)
                or not ref["owner_id"] or upload_owner_id(ref["owner_id"]) != upload_owner_id()):
            raise UploadLiftError("Attachment reference belongs to another owner.")
        events = _recorded_upload_events(session["id"], ref.get("event_id") or "")
        metadata = events[0]["payload"] if events else {}
        index = ref.get("index")
        attachments = metadata.get("upload_paths") or []
        if (upload_owner_id(metadata.get("upload_owner_id", ref["owner_id"])) != upload_owner_id(ref["owner_id"])
                or type(index) is not int or index < 0 or index >= len(attachments)):
            raise UploadLiftError("Attachment reference is not recorded in this session.")
        path = attachments[index]
        if path not in paths or ref.get("path") != path:
            raise UploadLiftError("Attachment reference does not match this message.")
        # A failed, not-yet-materialized send keeps its durable blob identity.
        pending = [blob for blob in upload_blobs.get_blobs(metadata.get("upload_blob_ids") or [])
                   if blob["session_id"] == session["id"] and blob["source_path"] == path]
        if metadata.get("not_sent") and pending:
            blob_ids.extend(blob["id"] for blob in pending)
        else:
            validate_upload_paths([path], session)
            if not Path(path).is_file():
                raise UploadLiftError("Recorded attachment is no longer on this owner; reattach it.")
    resolved.pop("upload_references", None)
    if blob_ids:
        resolved["upload_blob_ids"] = list(dict.fromkeys(blob_ids))
    return resolved


def validate_turn_uploads(payload: Dict[str, Any], session: Dict[str, Any]) -> None:
    """Every queued path is either an owned file or the source of a session blob."""
    from app.db import upload_blobs
    ids = payload.get("upload_blob_ids") or []
    blobs = upload_blobs.get_blobs(ids)
    if len(blobs) != len(ids) or any(blob["session_id"] != session["id"] for blob in blobs):
        raise UploadLiftError("Attachment blob does not belong to this session.")
    sources = {blob["source_path"] for blob in blobs}
    for path in payload.get("upload_paths") or []:
        if path not in sources:
            validate_upload_paths([path], session)
            if not Path(path).is_file():
                raise UploadLiftError("Attachment is no longer on this owner; reattach it.")


def lift_send_uploads(paths: List[str], project_id: Optional[str], session_id: str) -> List[str]:
    """Lift one send's staged uploads so another worker can receive them.

    Raises rather than returning part of them: the caller refuses the send.
    Synchronous (filesystem + DB) — call via ``run_sync`` from async contexts.
    """
    from app.core import project_resolver
    from app.db import database

    # Where each file was staged decides how it lifts. The composer can still
    # name its local project for a chat whose files are session-staged.
    staged, paths = split_session_staged_paths(paths, session_id)
    if not project_id:
        staged, paths = staged + paths, []
    blob_ids: List[str] = []
    if staged:
        session = database.get_session(session_id)
        if not session or not session.get("project_v2_id"):
            raise UploadLiftError("Attachments need a project session.")
        blob_ids = lift_session_paths_to_blobs(staged, session)
    if not paths:
        return blob_ids
    project = database.get_project(project_id)
    if not project:
        raise UploadLiftError(f"Project not found: {project_id}")
    project_v2_id = None
    try:
        resolved = project_resolver.resolve_project_v2({"project_id": project_id, "id": session_id})
        project_v2_id = (resolved or {}).get("id")
    except Exception:  # noqa: BLE001 — the v2 id is bookkeeping, not a gate
        logger.debug("Upload lift: project_v2 resolve failed for %s", project_id, exc_info=True)
    # A send can mix session staging, a first-message source in main, and an
    # established checkout outside the base project. Select each file's root,
    # rather than losing another attachment by replacing the batch's root.
    session = database.get_session(session_id)
    cwd = project_resolver.resolve_existing_cwd(session) if session else None
    checkout = Path(cwd).resolve() if cwd else None
    for source in paths:
        source_project = project
        if checkout and _is_under(Path(source).resolve(), checkout):
            source_project = {**project, "path": str(checkout)}
        blob_ids += lift_paths_to_blobs([source], source_project, session_id, project_v2_id)
    return blob_ids


def lift_session_paths_to_blobs(paths: List[str], session: Dict[str, Any]) -> List[str]:
    """Lift only this session's staged files, never arbitrary workspace paths."""
    session_id = session["id"]
    return lift_paths_to_blobs(
        paths, {"path": str(session_upload_dir(session_id))}, session_id,
        session.get("project_v2_id"),
    )


def lift_paths_to_blobs(
    paths: List[str],
    project_row: Dict[str, Any],
    session_id: Optional[str],
    project_v2_id: Optional[str] = None,
) -> List[str]:
    """Read each staged upload under the project dir into an ``upload_blobs``
    row and return the new blob ids, in the order given.

    Raises :class:`UploadLiftError` on ANY problem — a path outside the project
    directory, a missing file, an oversized file, or a read error. Partial
    delivery is worse than a refused send: the user would get a prompt half of
    whose attachments exist.

    Synchronous (filesystem + DB) — call via ``run_sync`` from async contexts.
    """
    from app.db import upload_blobs as upload_blobs_db

    if not paths:
        return []

    try:
        project_dir = _project_dir(project_row)
    except Exception as exc:  # noqa: BLE001
        raise UploadLiftError(
            f"Could not resolve the project directory for this upload: {exc}"
        ) from exc

    limit = _max_upload_size()
    blob_ids: List[str] = []
    for raw_path in paths:
        src = Path(raw_path)
        try:
            resolved = src.resolve()
        except Exception as exc:  # noqa: BLE001
            raise UploadLiftError(f"Invalid upload path {raw_path!r}: {exc}") from exc
        # Containment check FIRST — before any stat/read — so a prompt can never
        # coax us into touching a file outside the project.
        if not _is_under(resolved, project_dir):
            raise UploadLiftError(
                f"Attachment {os.path.basename(raw_path)} is outside the project "
                f"directory and cannot be sent to another device."
            )
        if not resolved.is_file():
            raise UploadLiftError(f"Attachment not found: {raw_path}")
        size = resolved.stat().st_size
        if size > limit:
            raise UploadLiftError(
                f"Attachment {resolved.name} is too large to send to another "
                f"device (limit {limit // (1024 * 1024)}MB)."
            )
        try:
            data = resolved.read_bytes()
        except Exception as exc:  # noqa: BLE001
            raise UploadLiftError(f"Could not read {resolved.name}: {exc}") from exc
        blob_ids.append(
            upload_blobs_db.create_blob(
                project_v2_id=project_v2_id,
                session_id=session_id,
                # The UPLOADER-absolute path exactly as embedded in the prompt —
                # the raw string, not the resolved one, so the spawn-side rewrite
                # is an exact match.
                source_path=raw_path,
                filename=resolved.name,
                size=size,
                data=data,
            )
        )
    logger.info(
        "Lifted %d upload(s) to blobs for session %s", len(blob_ids), session_id
    )
    return blob_ids



def materialize_blobs_for_turn(
    blob_ids: List[str], working_dir: str
) -> Dict[str, str]:
    """Write each blob into ``<working_dir>/uploads/`` and return
    ``{source_path: local_path}`` for the prompt rewrite.

    Already-materialized rows (``data`` NULL) are skipped IF their recorded
    local file is still present — the turn is a retry and the file is already on
    disk. When the bytes are gone AND the file is missing, this raises
    :class:`UploadLiftError`: running with a dead path would send the agent
    looking for a file that does not exist, which is exactly the silent loss
    this module exists to prevent.

    Synchronous (filesystem + DB) — call via ``run_sync``/``to_thread``.
    """
    from app.db import upload_blobs as upload_blobs_db

    if not blob_ids:
        return {}

    target_dir = Path(working_dir) / UPLOADS_SUBDIR

    mapping: Dict[str, str] = {}
    for blob_id in blob_ids:
        meta = upload_blobs_db.get_blob(blob_id)
        if meta is None:
            raise UploadLiftError(
                f"An attachment for this message is no longer available "
                f"(blob {blob_id} was purged)."
            )
        source_path = meta["source_path"]
        filename = meta["filename"]
        data = upload_blobs_db.get_blob_data(blob_id)
        if data is None:
            # Never reconstruct a consumed destination from the original name:
            # upload() may have collision-renamed it before a later item failed.
            recorded = meta.get("materialized_path")
            existing = Path(recorded) if recorded else None
            if existing is not None and existing.resolve().parent == target_dir.resolve() and existing.is_file():
                mapping[source_path] = str(existing)
                continue
            raise UploadLiftError(
                f"Attachment '{filename}' was already consumed and is no longer "
                f"on disk at {existing} — resend the file."
            )
        try:
            from app.core.confined_files import upload
            dest = upload(Path(working_dir), UPLOADS_SUBDIR, Path(filename).name or "upload", data)
        except Exception as exc:  # noqa: BLE001
            raise UploadLiftError(
                f"Could not write attachment '{filename}' to {target_dir}: {exc}"
            ) from exc
        upload_blobs_db.clear_data(blob_id, str(dest))
        mapping[source_path] = str(dest)
    logger.info(
        "Materialized %d upload blob(s) into %s", len(mapping), target_dir
    )
    return mapping


def rewrite_prompt(prompt: str, mapping: Dict[str, str]) -> str:
    """Replace every ``source_path`` in ``prompt`` with its local path.

    Exact-string replacement, longest source first so a path that is a prefix of
    another (``/p/a.txt`` vs ``/p/a.txt.bak``) can never be partially rewritten.
    """
    if not prompt or not mapping:
        return prompt
    out = prompt
    for source_path in sorted(mapping, key=len, reverse=True):
        out = out.replace(source_path, mapping[source_path])
    return out
