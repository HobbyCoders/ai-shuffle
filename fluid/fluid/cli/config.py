"""Shared CLI plumbing: store location, engine construction, session listing.

The one job worth calling out is :func:`make_engine`: it builds the resolved
instruction stack (``build_instruction_stack``) and injects it as the system-
prompt *prefix*, prepended before any per-run system text, then hands off to
:func:`fluid.config.build_engine`. Every CLI subcommand goes through here so the
instruction-file ecosystem is wired in exactly once. It also attaches a
:class:`~fluid.cli.host.CliHost` so the ``Task`` and ``Monitor`` builtins have
a dispatcher; the host rides along as ``engine.ctx.extra["cli_host"]``.
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
    observable ordering. Native agent definitions are always included when
    instruction discovery is enabled. The CLI keeps env injection off — its REPL banner
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
            include_agents=enabled,
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
    permission_responder: Any | None = None,
    background_host: bool = True,
):
    """Construct the engine for a CLI run with the instruction stack injected.

    ``permission_responder`` answers permission prompts raised by child
    subagents (the REPL passes its own y/N prompt; one-shot runs deny).
    ``background_host=False`` skips the host, leaving Task/Monitor unavailable.
    """
    compiled = resolved_prompt(config, extra_system=extra_system)
    engine = build_engine(
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
    if background_host:
        from fluid.cli.host import CliHost

        CliHost(
            config, store, provider=provider, provider_name=provider_name,
            permission_responder=permission_responder,
        ).attach(engine)
    return engine


def host_of(engine: Any):
    """The :class:`~fluid.cli.host.CliHost` attached to ``engine``, or None."""
    ctx = getattr(engine, "ctx", None)
    return ctx.extra.get("cli_host") if ctx is not None else None


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
