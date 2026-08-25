"""Shared CLI plumbing: store location, engine construction, session listing.

The one job worth calling out is :func:`make_engine`: it compiles the CLI's
system prompt (:func:`resolved_prompt` — the shipped base prompt, the
instruction stack, the skills/agents indexes, the environment block, then any
per-run text) and hands off to :func:`fluid.config.build_engine`. Every CLI
subcommand goes through here so the prompt is assembled in exactly one place.
It also attaches a :class:`~fluid.cli.host.CliHost` so the ``Task`` and
``Monitor`` builtins have a dispatcher; the host rides along as
``engine.ctx.extra["cli_host"]``.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
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


def resolved_prompt(
    config: HarnessConfig,
    *,
    extra_system: str | None = None,
    provider_name: str | None = None,
):
    """CLI prompt assembly, delegated to the shared compiler.

    The CLI is an application in the compiler's sense, so it supplies the
    ``base`` section itself (ruling A12 forbids only a *hidden* base):

    * ``config.system`` set → it IS the base. It replaces the default rather
      than being appended — ``system`` in config.toml is "your own base
      prompt".
    * otherwise ``base_prompt = true`` (the default) → the shipped
      :data:`~fluid.cli.prompt.DEFAULT_BASE_PROMPT`; ``false`` → no base.

    The base comes first, so it is the byte-stable prefix providers cache.
    Environment injection is on: the base prompt and the tool descriptions
    both point the model at the environment block for shell syntax, cwd, and
    date. ``extra_system`` (per-run text) and, under code mode, the code-mode
    addendum ride as ``extra_blocks`` after everything else. Native agent
    definitions are always included when instruction discovery is enabled.

    The compiler places the environment block after the instruction stack and
    the skills/agents indexes, so the daily date change invalidates only the
    cached suffix from that point on, never the base prompt in front of it.

    Returns the :class:`~fluid.context.compiler.CompiledPrompt` (text + labeled
    sections); :func:`resolved_system` is its ``.text``.
    """
    from fluid.cli.prompt import code_mode_addendum, default_base_prompt
    from fluid.codemode.skills import DEFAULT_SKILLS_ROOT
    from fluid.context.compiler import PromptInputs, compile_prompt

    if config.system:
        base: str | None = config.system
    elif config.base_prompt:
        base = default_base_prompt(config, provider_name=provider_name)
    else:
        base = None
    enabled = config.instruction_files.enabled
    return compile_prompt(
        PromptInputs(
            base=base,
            extra_blocks=[b for b in (code_mode_addendum(config), extra_system) if b],
            cwd=config.cwd,
            include_env=True,
            include_global_instructions=enabled and config.instruction_files.discover_global,
            include_project_instructions=enabled and config.instruction_files.discover_project,
            include_skills=enabled and config.instruction_files.discover_skills,
            include_agents=enabled,
            include_plugins=enabled and config.instruction_files.discover_plugins,
            memories_dir=(
                (config.skills_dir or str(DEFAULT_SKILLS_ROOT))
                if config.continual
                else None
            ),
            setting_sources=config.setting_sources,
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
    compiled = resolved_prompt(
        config, extra_system=extra_system, provider_name=provider_name
    )
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


def list_sessions(
    directory: str | os.PathLike[str] | None = None, *, limit: int | None = None
) -> list[dict[str, Any]]:
    """List persisted sessions in a JSONL store directory (newest first).

    Each row carries ``session_id``, ``created_at``, ``last_active`` (the newest
    node's timestamp, or ``created_at`` for an empty session), ``nodes``,
    ``first_message`` (the first user turn's text, untruncated) and the
    ``model``/``provider`` of the most recent turn when the event log recorded
    one. Reads each ``<sid>.jsonl`` line by line without building the tree, and
    only scans the event log for ``TurnStarted`` records — cheap enough to list
    hundreds of sessions. ``limit`` keeps the newest N rows.
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
        last_active = 0.0
        nodes = 0
        first_message: str | None = None
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
                        node = rec.get("node") or {}
                        last_active = max(last_active, float(node.get("ts") or 0.0))
                        if first_message is None:
                            first_message = _first_user_text(node.get("message") or {})
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        model, provider = _last_turn_model(d / f"{session_id}.events.jsonl")
        out.append({
            "session_id": session_id,
            "created_at": created_at,
            "last_active": last_active or created_at,
            "nodes": nodes,
            "first_message": first_message,
            "model": model,
            "provider": provider,
        })
    out.sort(key=lambda s: (s["last_active"], s["created_at"]), reverse=True)
    if limit is not None:
        out = out[: max(0, limit)]
    return out


def _first_user_text(message: dict[str, Any]) -> str | None:
    """Text of a stored user message, or None when it is not a user turn."""
    if message.get("role") != "user":
        return None
    parts = [
        b.get("text", "") for b in message.get("content", [])
        if isinstance(b, dict) and b.get("kind") == "text"
    ]
    text = " ".join(p for p in parts if p).strip()
    return text or None


def _last_turn_model(events_path: Path) -> tuple[str | None, str | None]:
    """(model, provider) from the newest ``TurnStarted`` event, if any."""
    if not events_path.exists():
        return None, None
    model = provider = None
    try:
        with events_path.open(encoding="utf-8") as fh:
            for line in fh:
                if '"TurnStarted"' not in line:
                    continue
                rec = json.loads(line)
                if rec.get("_cls") == "TurnStarted":
                    model = rec.get("model") or model
                    provider = rec.get("provider") or provider
    except (OSError, json.JSONDecodeError):
        pass
    return model, provider


def latest_session_id(directory: str | os.PathLike[str] | None = None) -> str | None:
    """The most recently active session in the store, for ``--continue``."""
    rows = list_sessions(directory, limit=1)
    return rows[0]["session_id"] if rows else None


def _snippet(text: str | None, width: int) -> str:
    if not text:
        return "-"
    flat = " ".join(text.split())
    return flat if len(flat) <= width else flat[: width - 1] + "…"


def _stamp(ts: float) -> str:
    if not ts:
        return "-"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def format_sessions(rows: list[dict[str, Any]], *, snippet_width: int = 48) -> str:
    """Render :func:`list_sessions` rows as an aligned table (newest first).

    Columns: id, created, last active, node count, model (``provider/model``
    when both are known), first user message truncated to ``snippet_width``.
    """
    if not rows:
        return "(no sessions)"
    header = ("SESSION", "CREATED", "LAST ACTIVE", "NODES", "MODEL", "FIRST MESSAGE")
    table = [header]
    for r in rows:
        model = r.get("model") or "-"
        if r.get("provider") and r.get("model"):
            model = f"{r['provider']}/{r['model']}"
        table.append((
            str(r["session_id"]),
            _stamp(r.get("created_at", 0.0)),
            _stamp(r.get("last_active", 0.0)),
            str(r.get("nodes", 0)),
            model,
            _snippet(r.get("first_message"), snippet_width),
        ))
    widths = [max(len(row[i]) for row in table) for i in range(len(header) - 1)]
    lines = []
    for row in table:
        cells = [row[i].ljust(widths[i]) for i in range(len(widths))] + [row[-1]]
        lines.append("  ".join(cells).rstrip())
    return "\n".join(lines)
