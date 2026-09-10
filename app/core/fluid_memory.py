"""Shared user-memory routing for native, delegated and network consumers."""
from __future__ import annotations

import hashlib
from pathlib import Path


def skills_root() -> Path:
    from app.core.config import settings
    return Path(settings.fluid_skills_dir) if settings.fluid_skills_dir else settings.effective_data_dir / "fluid" / "skills"


def owner_root(owner: str) -> Path:
    if owner == "local":
        return skills_root()  # existing installation-owner store; never an API user's store
    if not owner.startswith("api:") or not owner[4:]:
        raise ValueError("Unknown memory owner")
    return skills_root() / ".users" / hashlib.sha256(owner.encode()).hexdigest()


def store_for_owner(owner: str):
    from fluid.codemode.skills import SkillStore
    return SkillStore(owner_root(owner), owner=owner)


def session_memory(session_id: str | None, cwd: str | None = None):
    from app.core.config import settings
    from fluid.codemode.skills import project_key_for
    if not settings.managed_database_available():
        # Standalone/isolated tests have no authenticated multi-user database.
        return store_for_owner("local"), project_key_for(cwd)
    from app.db.sessions import get_session, get_session_by_sdk_id
    if not session_id:
        raise ValueError("Cannot resolve memory ownership without a session")
    session = get_session(session_id) or get_session_by_sdk_id(session_id)
    if not session:
        raise ValueError("Memory session does not exist")
    owner = f"api:{session['api_user_id']}" if session.get("api_user_id") else "local"
    project = session.get("project_v2_id") or session.get("project_id")
    return store_for_owner(owner), f"project:{project}" if project else project_key_for(cwd)


def stores():
    """Existing stores only; no user-controlled path is accepted."""
    from fluid.codemode.skills import SkillStore
    import json
    root = skills_root()
    if (root / "memories.json").exists():
        yield store_for_owner("local")
    for path in (root / ".users").glob("*/memories.json"):
        try:
            owner = json.loads(path.read_text(encoding="utf-8"))["owner"]
            if owner_root(owner) == path.parent:
                yield SkillStore(path.parent, owner=owner)
        except (OSError, ValueError, KeyError):
            continue


async def maintain_memories():
    """Finite maintenance batches, independent of chat or a learning model."""
    import asyncio
    import logging
    while True:
        try:
            def sweep():
                for store in stores():
                    try:
                        store.reap()
                        store.purge_forgotten_evidence()
                    except Exception:
                        logging.getLogger(__name__).exception("One memory store could not be maintained")
            await asyncio.to_thread(sweep)
        except Exception:
            logging.getLogger(__name__).exception("Memory maintenance failed")
        await asyncio.sleep(300)
