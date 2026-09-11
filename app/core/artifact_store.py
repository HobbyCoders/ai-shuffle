"""Content-addressed artifact byte store (v2 core).

Blobs are addressed by the sha256 hexdigest of their bytes and stored on disk
under a two-char shard directory. Writes are atomic (temp file + ``os.replace``)
and idempotent: storing identical bytes twice is a no-op that returns the same
``artifacts`` row. Metadata lives in the ``artifacts`` table (see
``app.db.artifacts``); the bytes live on disk here.
"""

from __future__ import annotations

import hashlib
import os
import re
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from app.core.config import settings
from app.db.artifacts import upsert_artifact

ARTIFACTS_DIR: Path = settings.effective_data_dir / "artifacts"

_HASH_RE = re.compile(r"^[0-9a-f]{64}\Z")


def store_bytes(
    data: bytes,
    mime: Optional[str] = None,
    filename: Optional[str] = None,
    created_by_session: Optional[str] = None,
    runner_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Store ``data`` content-addressed and register its metadata row.

    Idempotent for identical bytes. Sync function — callers wrap in
    ``asyncio.to_thread``. Returns the stored ``artifacts`` row.

    ``runner_id`` records which install holds the bytes on disk (v61 holder
    attribution) so a non-holder install can 307-redirect a download to the
    holder. First-writer-wins with the hash primary key.
    """
    digest = hashlib.sha256(data).hexdigest()
    path = ARTIFACTS_DIR / digest[:2] / digest
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.tmp-{uuid.uuid4().hex}")
        try:
            with open(tmp, "wb") as fh:
                fh.write(data)
            os.replace(tmp, path)
        except OSError:
            # Lost a concurrent race storing the SAME content hash (two identical
            # screenshots in one turn each fire a store): on Windows the losing
            # os.replace can raise while the winner holds the destination. If the
            # blob now exists the store is a benign idempotent no-op; otherwise
            # it is a genuine failure. Either way never orphan our temp file.
            if not path.exists():
                raise
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
    row = upsert_artifact(
        hash=digest,
        size=len(data),
        mime=mime,
        filename=filename,
        created_by_session=created_by_session,
        runner_id=runner_id,
    )
    from app.network import blobs, engine
    if engine.context() is not None:
        blobs.register_present(digest, len(data))
        engine.kick()
    return row


def artifact_path(hash: str) -> Optional[Path]:
    """Return the on-disk path for ``hash`` iff it is well-formed and present."""
    if not isinstance(hash, str) or not _HASH_RE.match(hash):
        return None
    path = ARTIFACTS_DIR / hash[:2] / hash
    return path if path.exists() else None
