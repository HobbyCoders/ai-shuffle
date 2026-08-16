"""Shared CLI plumbing: store location, engine construction, session listing.

The one job worth calling out is :func:`make_engine`: it builds the resolved
instruction stack (``build_instruction_stack``) and injects it as the system-
prompt *prefix*, prepended before any per-run system text, then hands off to
:func:`fluid.config.build_engine`. Every CLI subcommand goes through here so the
instruction-file ecosystem is wired in exactly once.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from fluid.config.loader import build_engine
from fluid.config.model import HarnessConfig


def default_store_dir() -> Path:
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "fluid" / "sessions"


def make_store(directory: str | os.PathLike[str] | None = None):
    from fluid.store.jsonl import JsonlStore

    return JsonlStore(directory or default_store_dir())


def resolved_prompt(config: HarnessConfig, *, extra_system: str | None = None):
    """CLI prompt assembly, delegated to the shared compiler.

    The CLI's historical layering (instruction stack first, then config/per-run
    system text) predates the compiler's base-first order; ``config.system`` and
    ``extra_system`` therefore ride as ``extra_blocks``, preserving the CLI's
    observable ordering. The Claude-ecosystem toggles (CE9) ride through as
    ``include_agents`` / ``include_plugins``: turning plugins off in config must
    drop plugin skills and plugin agents from the compiled indexes too, not just
    from the engine. The CLI keeps env injection off — its REPL banner
    already shows cwd/model, and pre-compiler behavior carried no env block.

    Returns the :class:`~fluid.context.compiler.CompiledPrompt` (text + labeled
    sections); :func:`resolved_system` is its ``.text``.
    """
    from fluid.context.compiler import PromptInputs, compile_prompt

    enabled = config.instruction_files.enabled
    return compile_prompt(
        PromptInputs(
            base=None,
            extra_blocks=[b for b in (config.system, extra_system) if b],
            cwd=config.cwd,
            include_env=False,
            include_global_instructions=enabled and config.instruction_files.discover_global,
            include_project_instructions=enabled and config.instruction_files.discover_project,
            include_skills=enabled and config.instruction_files.discover_skills,
            include_agents=config.claude_ecosystem.agents,
            include_plugins=config.claude_ecosystem.plugins,
        )
    )


def resolved_system(config: HarnessConfig, *, extra_system: str | None = None) -> str | None:
    """The compiled CLI system prompt as a plain string."""
    return resolved_prompt(config, extra_system=extra_system).text


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
    compiled = resolved_prompt(config, extra_system=extra_system)
    return build_engine(
        config,
        store,
        provider_name=provider_name,
        provider=provider,
        bus=bus,
        session_id=session_id,
        system=compiled.text,
        system_sections=compiled,
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
