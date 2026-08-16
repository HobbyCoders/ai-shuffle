"""Cross-install upload delivery — lift at send, materialize at spawn (D8).

An upload is staged on the disk of whichever install served the upload endpoint.
When the turn that references it runs on ANOTHER install, that path means
nothing there. This module is the whole transport, and it is DB-mediated by
ruling: there is no device-to-device HTTP in this cluster (``base_url`` is
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


def _unique_target(target_dir: Path, filename: str) -> Path:
    """``<dir>/<name>`` with ``_1``/``_2`` suffixes on collision — the same
    de-duplication ``_perform_project_upload`` applies."""
    safe = Path(filename).name or "upload"
    path = target_dir / safe
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    counter = 1
    while path.exists():
        path = target_dir / f"{stem}_{counter}{suffix}"
        counter += 1
    return path


def materialize_blobs_for_turn(
    blob_ids: List[str], working_dir: str
) -> Dict[str, str]:
    """Write each blob into ``<working_dir>/uploads/`` and return
    ``{source_path: local_path}`` for the prompt rewrite.

    Already-materialized rows (``data`` NULL) are skipped IF their recorded
    local file is still present — the turn is a retry and the file is already on
    disk. When the bytes are gone AND the file is missing, this raises
    :class:`UploadLiftError`: running with a dead path would send the agent
    looking for a file that does not exist, which is exactly the silent loss D8
    exists to prevent.

    Synchronous (filesystem + DB) — call via ``run_sync``/``to_thread``.
    """
    from app.db import upload_blobs as upload_blobs_db

    if not blob_ids:
        return {}

    target_dir = Path(working_dir) / UPLOADS_SUBDIR
    target_dir.mkdir(parents=True, exist_ok=True)

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
            # Already delivered on an earlier attempt — accept it only if the
            # file is genuinely still there.
            existing = target_dir / Path(filename).name
            if existing.is_file():
                mapping[source_path] = str(existing)
                continue
            raise UploadLiftError(
                f"Attachment '{filename}' was already consumed and is no longer "
                f"on disk at {existing} — resend the file."
            )
        dest = _unique_target(target_dir, filename)
        try:
            dest.write_bytes(data)
        except Exception as exc:  # noqa: BLE001
            raise UploadLiftError(
                f"Could not write attachment '{filename}' to {target_dir}: {exc}"
            ) from exc
        upload_blobs_db.clear_data(blob_id)
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
