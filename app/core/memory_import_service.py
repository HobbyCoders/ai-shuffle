"""Explicit, per-project import of an agent provider's memory dir.

Agent-memory sync is native-only (see :mod:`app.core.memory_sync`): the claude
CLI's ``<claude_projects>/<slug>/memory/`` is never read by a session and never
pushed to the shared DB. A project that accumulated memory while running on the
CLI therefore starts empty on the native leg — unless someone deliberately
brings it across. That is what this module is for.

The import is:

* **explicit** — an admin triggers it per project, per provider. Nothing here
  runs on a session lifecycle hook.
* **non-destructive** — a source file is copied only when the destination file
  does not exist. An existing destination is reported as a conflict and left
  untouched, both at preview time and (re-checked) at copy time. The source dir
  is never written to or deleted from.
* **filesystem only** — no DB rows are written. The next native session's
  reconcile/push picks the new files up and shares them like any other.

Providers:

* ``"claude"`` — ``<claude_projects>/<slug>/memory`` (the delegated leg's dir,
  resolved through ``memory_sync.memory_dir_for_working_dir``).
* ``"codex"`` — ``~/.codex/projects/<slug>/memory``, the same shape one root
  over. No codex build writes that dir today; the path simply resolves, a
  missing dir previews as empty, and applying is a no-op. This is a placeholder
  that must not error, not a claim the layout is final.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List, Optional

from app.core import memory_sync
from app.core.jsonl_parser import get_project_dir_name
from app.core.project_utils import get_project_working_dir

logger = logging.getLogger(__name__)

PROVIDERS = ("claude", "codex")


def _codex_memory_dir(working_dir: str) -> Optional[Path]:
    """``~/.codex/projects/<slug>/memory`` for a session's working directory.

    Mirrors ``memory_sync.memory_dir_for_working_dir`` exactly — same worktree
    fold, same slug — but rooted at the codex provider home. Returns the path
    whether or not anything exists there.
    """
    try:
        anchor = memory_sync._repo_root_for_working_dir(working_dir)
        return Path.home() / ".codex" / "projects" / get_project_dir_name(anchor) / "memory"
    except Exception:  # noqa: BLE001 — a bad path is "nothing to import"
        logger.debug("memory_import: could not resolve codex dir for %r", working_dir,
                     exc_info=True)
        return None


def _source_dir(working_dir: str, provider: str) -> Optional[Path]:
    if provider == "claude":
        return memory_sync.memory_dir_for_working_dir(working_dir, "delegated")
    return _codex_memory_dir(working_dir)


def _resolve_dirs(project: dict, provider: str) -> tuple[Optional[Path], Optional[Path]]:
    """(source, destination) memory dirs for a project + provider."""
    if provider not in PROVIDERS:
        raise ValueError(
            f"Unknown memory provider: {provider!r}. Must be one of: {', '.join(PROVIDERS)}"
        )
    anchor = get_project_working_dir(project)
    return (
        _source_dir(anchor, provider),
        memory_sync.memory_dir_for_working_dir(anchor, "native"),
    )


def _source_files(source_dir: Optional[Path]) -> Dict[str, int]:
    """``{relative_path: size}`` for the importable ``*.md`` files in the source.

    Reuses ``memory_sync._scan_local`` so the import sees exactly the set a sync
    would — including its containment guard, which drops a planted ``.md``
    symlink pointing outside the memory tree.
    """
    if source_dir is None or not source_dir.exists():
        return {}
    return {
        rel: len(content.encode("utf-8"))
        for rel, (content, _hash, _mtime) in memory_sync._scan_local(source_dir).items()
    }


def preview_memory_import(project: dict, provider: str) -> dict:
    """What an import from ``provider`` would copy into the project's fluid dir.

    Every source file is listed with a status: ``"ready"`` when the destination
    file does not exist, ``"conflict"`` when it does (identical content still
    counts as a conflict — the import never overwrites and never diffs).
    """
    source_dir, destination_dir = _resolve_dirs(project, provider)
    files = _source_files(source_dir)

    items: List[dict] = []
    for rel in sorted(files):
        target = _safe_destination(destination_dir, rel)
        exists = target is not None and target.exists()
        items.append({
            "relative_path": rel,
            "size": files[rel],
            "status": "conflict" if exists else "ready",
        })

    return {
        "provider": provider,
        "source_dir": str(source_dir) if source_dir else None,
        "destination_dir": str(destination_dir) if destination_dir else None,
        "items": items,
        "ready_count": sum(1 for i in items if i["status"] == "ready"),
        "conflict_count": sum(1 for i in items if i["status"] == "conflict"),
    }


def _safe_destination(destination_dir: Optional[Path], relative_path: str) -> Optional[Path]:
    """Destination path for ``relative_path``, or None if it escapes the dir."""
    if destination_dir is None:
        return None
    return memory_sync._safe_target(destination_dir, relative_path)


def apply_memory_import(
    project: dict, provider: str, relative_paths: Optional[List[str]] = None
) -> dict:
    """Copy the selected source files into the project's native memory dir.

    ``relative_paths=None`` means "everything that previews as ready". A named
    path that is missing at the source, escapes the destination dir, or whose
    destination already exists is skipped with a reason rather than failing the
    whole import — and the existence check is redone here, so a file created
    between preview and apply is still never clobbered.
    """
    source_dir, destination_dir = _resolve_dirs(project, provider)
    files = _source_files(source_dir)

    if relative_paths is None:
        selected = sorted(files)
    else:
        selected = list(dict.fromkeys(relative_paths))  # de-dupe, keep order

    imported: List[str] = []
    skipped: List[dict] = []

    for rel in selected:
        # Path containment first: a ``../evil.md`` must be refused as the escape
        # it is, not merely reported as absent from the source listing.
        target = _safe_destination(destination_dir, rel)
        if target is None:
            skipped.append({"relative_path": rel, "reason": "invalid_path"})
            continue
        if rel not in files:
            skipped.append({"relative_path": rel, "reason": "not_found"})
            continue
        if target.exists():
            skipped.append({"relative_path": rel, "reason": "exists"})
            continue
        source_file = memory_sync._safe_target(source_dir, rel)
        if source_file is None:
            skipped.append({"relative_path": rel, "reason": "invalid_path"})
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(source_file.read_text(encoding="utf-8"), encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            logger.warning("memory_import: could not copy %s", rel, exc_info=True)
            skipped.append({"relative_path": rel, "reason": "copy_failed"})
            continue
        imported.append(rel)

    logger.info(
        "memory_import: provider=%s src=%s dst=%s imported=%d skipped=%d",
        provider, source_dir, destination_dir, len(imported), len(skipped),
    )
    return {
        "imported": imported,
        "skipped": skipped,
        "source_dir": str(source_dir) if source_dir else None,
        "destination_dir": str(destination_dir) if destination_dir else None,
    }
