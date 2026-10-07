"""Host routing for Fluid's project-global harness state (not checkout files)."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from fluid.state import safe_key
from fluid.tools.registry import ToolCtx


def project_state_root(row: Mapping[str, Any] | None = None, cwd: str | None = None) -> Path:
    from app.core.config import settings
    from app.core.memory_sync import _resolve_target, memory_dir_for_working_dir

    row = row or {}
    project_id = row.get("project_v2_id") or row.get("project_id")
    if row.get("project_id"):
        _, memory_dir = _resolve_target(row["project_id"], cwd, "native")
        if memory_dir:
            return memory_dir.parent.resolve()
    if row.get("project_v2_id"):
        # Use the registered BASE checkout, not the session's branch checkout.
        # This is a pure DB read: no lazy materialization or session self-heal.
        from app.core.checkout_manager import _resolve_checkout_path
        from app.core.project_resolver import _local_checkout

        try:
            checkout = _local_checkout(row["project_v2_id"], settings.effective_instance_id)
        except Exception:  # noqa: BLE001 — unavailable DB retains cwd-based routing
            checkout = None
        if checkout and checkout.get("path"):
            cwd = _resolve_checkout_path(checkout["path"])
    if cwd:
        memory_dir = memory_dir_for_working_dir(str(Path(cwd).resolve()), "native")
        if memory_dir:
            return memory_dir.parent.resolve()
    # No checkout: retain project isolation rather than sharing one global file.
    return (settings.get_fluid_projects_dir / safe_key(project_id or "no-project")).resolve()


def session_state_root(session_id: str | None, cwd: str | None = None) -> Path:
    from app.core.config import settings

    row = None
    if session_id and settings.managed_database_available():
        from app.db.sessions import get_session, get_session_by_sdk_id
        row = get_session(session_id) or get_session_by_sdk_id(session_id)
    return project_state_root(row, cwd)


def bind_state_context(ctx: ToolCtx, session_id: Callable[[], str | None]) -> ToolCtx:
    """Late-bind app identity: fresh engines acquire their session ID after boot."""
    ctx.extra["harness_state_root"] = lambda: session_state_root(session_id(), ctx.cwd)
    return ctx
