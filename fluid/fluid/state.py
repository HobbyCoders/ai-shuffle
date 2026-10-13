"""Harness-owned state, separate from the project's load-only .fluid tree.

Hosts may supply ``ctx.extra["harness_state_root"]`` (a Path or a callable).
Standalone Fluid uses its global projects directory and memory project identity.
Legacy state is copied exclusively, never moved or overwritten.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import uuid
from pathlib import Path

from fluid.tools.registry import ToolCtx

_log = logging.getLogger(__name__)


def safe_key(value: str | None) -> str:
    """Keep ordinary IDs readable; encode unsafe IDs without collisions/traversal."""
    value = value or "default"
    if re.fullmatch(r"[A-Za-z0-9_-]{1,160}", value):
        return value
    return "key~" + hashlib.sha256(value.encode()).hexdigest()


def project_state_root(ctx: ToolCtx) -> Path:
    supplied = ctx.extra.get("harness_state_root")
    if callable(supplied):
        supplied = supplied()
    if supplied is not None:
        return Path(supplied).expanduser().resolve()
    from fluid.codemode.skills import project_key_for

    identity = ctx.extra.get("memory_project") or project_key_for(ctx.cwd)
    return Path.home() / ".fluid" / "projects" / safe_key(identity)


def state_path(ctx: ToolCtx, kind: str, session_id: str | None = None) -> Path:
    """Resolve state and copy a legacy file once, leaving the original intact.

    An empty canonical file is a tombstone: cleared state must not resurrect
    from the legacy file. Exclusive creation makes a newer canonical copy win.
    Unsafe legacy IDs are never used as paths. Symlinks escaping the legacy
    directory are not followed.
    """
    session = session_id or ctx.session_id or "default"
    path = project_state_root(ctx) / kind / f"{safe_key(session)}.md"
    legacy = _legacy_path(ctx, kind, session)
    if not path.exists() and legacy is not None:
        try:
            text = legacy.read_text(encoding="utf-8")
        except OSError:
            return path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Publish a complete file exclusively: neither a concurrent new
            # writer nor a crash during the copy can expose half a ledger.
            tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
            try:
                tmp.write_text(text, encoding="utf-8")
                try:
                    os.link(tmp, path)
                except FileExistsError:
                    pass
            finally:
                tmp.unlink(missing_ok=True)
        except OSError:
            # Read fallback keeps resumed state available even on a read-only
            # volume or a filesystem without hard links. Mutations still write
            # ONLY the canonical path and report their own I/O errors.
            _log.debug("legacy harness state copy failed", exc_info=True)
    return path


def _legacy_path(ctx: ToolCtx, kind: str, session: str) -> Path | None:
    if safe_key(session) != session:
        return None
    directory = Path(ctx.cwd or ".") / ".fluid" / kind
    path = directory / f"{session}.md"
    return path if path.resolve().parent == directory.resolve() else None


def read_state(ctx: ToolCtx, kind: str, session_id: str | None = None) -> str:
    path = state_path(ctx, kind, session_id)
    # Presence, not non-empty content, decides precedence. Never substitute an
    # old copy for a canonical file that is empty or currently unreadable.
    if not path.exists():
        path = _legacy_path(ctx, kind, session_id or ctx.session_id or "default")
    if path is None:
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def scratch_dir(ctx: ToolCtx) -> Path:
    return project_state_root(ctx) / "scratch" / safe_key(ctx.session_id)


def scratch_prompt(ctx: ToolCtx) -> str:
    """Render harness context without making it part of the owner's prompt.

    Use the same reminder envelope as the ledger so journal, recall, export
    and compaction consumers can strip it through their existing paths.
    """
    return (
        "<system-reminder>\n"
        "# Harness scratch\n"
        f"Use {scratch_dir(ctx)} (absolute, per-session) for temporary briefs, "
        "artifacts and review evidence; create it only when needed. Never put "
        "harness state or scratch in the working folder's .fluid/. Project "
        ".fluid is for loading skills, agents, commands, plugins and settings "
        "(and host-managed worktrees); explicit skill/plugin authoring is unchanged."
        "\n</system-reminder>"
    )
