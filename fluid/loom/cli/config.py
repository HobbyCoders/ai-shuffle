"""Shared CLI plumbing: store location, engine construction, session listing.

The one job worth calling out is :func:`make_engine`: it builds the resolved
instruction stack (``build_instruction_stack``) and injects it as the system-
prompt *prefix*, prepended before any per-run system text, then hands off to
:func:`loom.config.build_engine`. Every CLI subcommand goes through here so the
instruction-file ecosystem is wired in exactly once.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from loom.config.loader import build_engine
from loom.config.model import HarnessConfig
from loom.context.instructions import build_instruction_stack


def default_store_dir() -> Path:
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "loom" / "sessions"


def make_store(directory: str | os.PathLike[str] | None = None):
    from loom.store.jsonl import JsonlStore

    return JsonlStore(directory or default_store_dir())


def resolved_system(config: HarnessConfig, *, extra_system: str | None = None) -> str | None:
    """Instruction stack (prefix) + config/per-run system text, or None."""
    parts: list[str] = []
    if config.instruction_files.enabled:
        stack = build_instruction_stack(
            config.cwd,
            include_global=config.instruction_files.discover_global,
            include_project=config.instruction_files.discover_project,
        )
        if stack.strip():
            parts.append(stack)
    if config.system:
        parts.append(config.system)
    if extra_system:
        parts.append(extra_system)
    return "\n\n".join(parts) if parts else None


def make_engine(
    config: HarnessConfig,
    store,
    *,
    provider_name: str | None = None,
    provider: Any | None = None,
    bus=None,
    session_id: str | None = None,
    extra_system: str | None = None,
    plan_mode: bool = False,
    registry: Any | None = None,
    claude_cmd: list[str] | None = None,
):
    """Construct the engine for a CLI run with the instruction stack injected."""
    system = resolved_system(config, extra_system=extra_system)
    return build_engine(
        config,
        store,
        provider_name=provider_name,
        provider=provider,
        bus=bus,
        session_id=session_id,
        system=system,
        plan_mode=plan_mode,
        registry=registry,
        claude_cmd=claude_cmd,
    )


def list_sessions(directory: str | os.PathLike[str] | None = None) -> list[dict[str, Any]]:
    """List persisted sessions in a JSONL store directory (newest first).

    Reads each ``<sid>.jsonl`` header line (the ``meta`` record) without loading
    the whole tree — cheap enough to list hundreds of sessions.
    """
    d = Path(directory or default_store_dir())
    if not d.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for log in sorted(d.glob("*.jsonl")):
        if log.name.endswith(".events.jsonl"):
            continue
        session_id = log.name[: -len(".jsonl")]
        created_at = 0.0
        nodes = 0
        try:
            with log.open(encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    rec = json.loads(line)
                    if rec.get("t") == "meta":
                        created_at = rec.get("created_at", 0.0)
                    elif rec.get("t") == "node":
                        nodes += 1
        except (OSError, json.JSONDecodeError):
            continue
        out.append({"session_id": session_id, "created_at": created_at, "nodes": nodes})
    out.sort(key=lambda s: s["created_at"], reverse=True)
    return out
