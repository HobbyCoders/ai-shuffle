"""``fluid`` console entry — chat, run, sessions, config, and migration.

  fluid chat [--tui]        interactive REPL (or Textual TUI)
  fluid run "<prompt>"      one-shot: stream the reply, then exit
  fluid sessions [--limit]  list persisted sessions from the JSONL store

``chat`` and ``run`` accept ``--resume <session-id>`` to pick up an existing
session, or ``--continue`` for the most recently active one. Both build the
engine against the stored session, so the engine's own resume path (deferred
catalog, compaction, background tasks) rehydrates on the first turn.
  fluid config              print the resolved layered config
  fluid import-settings     preview an explicit provider-to-Fluid migration

The heavy lifting lives in ``fluid.cli.config`` (engine construction with the
instruction stack injected) and ``fluid.cli.repl`` (rendering/permissions). The
one-shot path (:func:`run_once`) is the offline-testable seam — a fake provider
can be injected without touching argparse or the network.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _dist_version
from typing import Any

from fluid.cli.config import (
    format_sessions,
    host_of,
    latest_session_id,
    list_sessions,
    make_engine,
    make_store,
)
from fluid.cli.repl import (
    SIGINT_EXIT_CODE,
    Repl,
    abandon_stdin_read,
    auto_deny,
    request_interrupt,
    sigint_scope,
    stream_turn,
    tty_permission_responder,
)
from fluid.config.loader import load_config
from fluid.config.model import HarnessConfig
from fluid.store.base import SessionLocked


def _cli_overrides(args: argparse.Namespace) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    if getattr(args, "provider", None):
        overrides["default_provider"] = args.provider
    if getattr(args, "model", None):
        overrides["model_override"] = args.model
    if getattr(args, "code_mode", False):
        overrides["code_mode"] = True
    if getattr(args, "continual", False):
        overrides["code_mode"] = True  # continual implies code-mode
        overrides["continual"] = True
    if getattr(args, "skills_dir", None):
        overrides["skills_dir"] = args.skills_dir
    if getattr(args, "approval_policy", None):
        overrides["permission"] = {"approval_policy": args.approval_policy}
    return overrides


def _resolve_config(args: argparse.Namespace) -> HarnessConfig:
    return load_config(cwd=getattr(args, "cwd", None), overrides=_cli_overrides(args))


# ─── one-shot run (testable seam) ────────────────────────────────────────────


async def run_once(
    config: HarnessConfig,
    store,
    prompt: str,
    *,
    provider: Any | None = None,
    provider_name: str | None = None,
    out=None,
    on_permission=auto_deny,
    on_question=None,
    session_id: str | None = None,
) -> int:
    """Run a single turn and stream it to ``out``. Returns a process exit code.

    ``provider`` injects a ProviderClient (offline test hook); when omitted the
    engine is built from ``config`` (real provider). ``session_id`` resumes an
    existing session instead of creating a fresh one.
    """
    out = out or sys.stdout
    meta = await open_session(store, session_id, cwd=config.cwd)
    engine = make_engine(
        config, store, provider=provider, provider_name=provider_name,
        session_id=meta.session_id, permission_responder=on_permission,
    )
    host = host_of(engine)
    sigints = 0

    def on_sigint() -> None:
        # Ctrl-C ends the turn through the engine (the session stays intact
        # and is flushed below), releases a stdin prompt the turn may be
        # parked on, and makes the run exit 130.
        nonlocal sigints
        sigints += 1
        if sigints == 1:
            out.write("\n[interrupting]\n")
            out.flush()
        request_interrupt(engine)
        abandon_stdin_read()

    try:
        # Background work (Task/Monitor) parks the session instead of ending
        # it; the one-shot run keeps streaming through park and wake until
        # nothing is in flight, so it never exits with workers running.
        from fluid.cli.repl import auto_answer, tty_question_responder

        with sigint_scope(on_sigint):
            stop = await stream_turn(
                engine, meta.session_id, prompt, out=out, on_permission=on_permission,
                on_question=on_question or tty_question_responder() or auto_answer,
                wait_background=True,
            )
    finally:
        if host is not None:
            await host.aclose()
        await _close_and_flush_learning(engine, meta.session_id, out)
    if sigints:
        return SIGINT_EXIT_CODE
    return 0 if stop != "error" else 1


async def _close_and_flush_learning(engine, session_id: str, out) -> None:
    """Close the engine, then render learning that landed during close.

    Session close drains the continual synthesizer — a final pass can run
    AFTER the turn's event stream ended, so its ``ContinualUpdate`` reaches
    the bus with nobody consuming. Learning that lands invisibly might as
    well not have happened; print those late events here. Everything
    published before close was already rendered by the stream loop.
    """
    from fluid.events.model import ContinualUpdate

    before = engine.bus.last_seq(session_id)
    await engine.aclose()
    for event in engine.bus.log(session_id):
        if event.seq > before and isinstance(event, ContinualUpdate):
            out.write(f"\n  [learned] {', '.join(event.changes)}\n")
            out.flush()


class SessionNotFound(LookupError):
    """``--resume`` named a session the store does not have."""


async def open_session(store, session_id: str | None, *, cwd: str | None = None):
    """The session meta to run against: the existing one, or a fresh one.

    The engine is always handed the session id of a session the store already
    holds, so ``NativeLoop._ensure_session`` finds it through ``get_session``
    and its first turn runs the rehydration path (deferred catalog, compaction
    events, background tasks) instead of starting from an empty tree. That also
    means the engine never mints the session, so ``cwd`` has to be recorded
    here or the session ends up with no project for recall to scope on.
    """
    if session_id is None:
        return await store.create_session(cwd=cwd)
    meta = await store.get_session(session_id)
    if meta is None:
        raise SessionNotFound(session_id)
    # Refuse a session another process has open before the first turn, not
    # halfway through it. Stores without locking just skip this.
    lock = getattr(store, "lock_session", None)
    if lock is not None:
        lock(meta.session_id)
    return meta


async def chat_session(
    config: HarnessConfig,
    store,
    *,
    provider_name: str | None = None,
    use_tui: bool = False,
    session_id: str | None = None,
) -> int:
    meta = await open_session(store, session_id, cwd=config.cwd)
    engine = make_engine(config, store, provider_name=provider_name, session_id=meta.session_id)
    try:
        if use_tui:
            from fluid.cli.tui import run_tui

            return await run_tui(engine, meta.session_id)
        verb = "resumed" if session_id else "session"
        print(f"fluid chat — {verb} {meta.session_id} (/help, /exit)", file=sys.stderr)
        return await Repl(engine, meta.session_id).run()
    finally:
        host = host_of(engine)
        if host is not None:
            await host.aclose()
        await _close_and_flush_learning(engine, meta.session_id, sys.stderr)


# ─── argparse wiring ─────────────────────────────────────────────────────────


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--cwd", default=None, help="working directory for config/instruction discovery")
    p.add_argument("--provider", default=None, help="provider name from config")
    p.add_argument("--model", default=None, help="override the default model")
    p.add_argument("--store", default=None, help="JSONL session store directory")
    p.add_argument("--code-mode", action="store_true", dest="code_mode", help="enable code-mode")
    p.add_argument("--continual", action="store_true",
                   help="enable the continual harness (implies --code-mode): journal cells "
                        "and auto-synthesize reusable skills")
    p.add_argument("--skills-dir", default=None, dest="skills_dir",
                   help="persistent skills root (default ~/.fluid/skills)")
    p.add_argument("--approval-policy", default=None,
                   choices=["never_ask", "on_write", "always_ask"])
    p.add_argument("--resume", default=None, metavar="SESSION_ID",
                   help="pick up an existing session from the store")
    p.add_argument("--continue", action="store_true", dest="continue_last",
                   help="pick up the most recently active session in the store")


def _version() -> str:
    try:
        return _dist_version("fluid")
    except PackageNotFoundError:
        return "0.1.0"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fluid", description="fluid agent harness CLI")
    parser.add_argument("--version", action="version", version=f"fluid {_version()}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_chat = sub.add_parser("chat", help="interactive REPL")
    _add_common(p_chat)
    p_chat.add_argument("--tui", action="store_true", help="use the Textual TUI")

    p_run = sub.add_parser("run", help="one-shot: run a single prompt and exit")
    _add_common(p_run)
    p_run.add_argument("prompt", help="the prompt to run")

    p_sessions = sub.add_parser("sessions", help="list persisted sessions")
    p_sessions.add_argument("--store", default=None, help="JSONL session store directory")
    p_sessions.add_argument("--limit", type=int, default=None, help="show only the newest N sessions")

    p_config = sub.add_parser("config", help="print the resolved config")
    _add_common(p_config)

    p_anchors = sub.add_parser(
        "anchors", help="manage replay anchors (guarded-evolution retention set)"
    )
    anchors_sub = p_anchors.add_subparsers(dest="anchors_command", required=True)
    a_list = anchors_sub.add_parser("list", help="show the anchor set")
    _add_common(a_list)
    a_add = anchors_sub.add_parser("add", help="add one anchor manually")
    _add_common(a_add)
    a_add.add_argument("prompt", help="the task prompt to replay (natural phrasing; never name a tool)")
    a_add.add_argument("--title", default=None, help="short behavior description")
    a_add.add_argument("--expect", action="append", metavar="REGEX",
                       help="final reply must match; repeatable")
    a_add.add_argument("--forbid", action="append", metavar="REGEX",
                       help="final reply must NOT match; repeatable")
    a_add.add_argument("--tool", action="append", metavar="NAME",
                       help="this tool must have been called; repeatable")
    a_add.add_argument("--check-file", action="append", dest="check_file",
                       metavar="PATH[:REGEX]",
                       help="workspace file must exist (and match); repeatable")
    a_add.add_argument("--tools", default=None,
                       help="comma list overriding the replay tool surface")
    a_add.add_argument("--anchor-model", default=None, dest="anchor_model",
                       help="pin the replay model (per-family anchors)")
    a_rm = anchors_sub.add_parser("rm", help="remove an anchor")
    _add_common(a_rm)
    a_rm.add_argument("id", help="anchor id (a1, a2, …)")
    a_run = anchors_sub.add_parser(
        "run", help="replay anchors against the live model and record baselines"
    )
    _add_common(a_run)
    a_run.add_argument("id", nargs="?", default=None, help="replay only this anchor")
    a_harvest = anchors_sub.add_parser(
        "harvest", help="draft anchor candidates from recent project sessions"
    )
    _add_common(a_harvest)
    a_harvest.add_argument("--limit", type=int, default=None,
                           help="sessions to consider (default 10)")
    a_harvest.add_argument("--yes", action="store_true",
                           help="accept every valid draft without confirming")

    p_evolve = sub.add_parser(
        "evolve", help="guarded harness evolution: propose, parked, log"
    )
    evolve_sub = p_evolve.add_subparsers(dest="evolve_command", required=True)
    e_propose = evolve_sub.add_parser(
        "propose", help="propose a guidance edit through the commit gate"
    )
    _add_common(e_propose)
    e_propose.add_argument("--stem", required=True,
                           help="guidance file stem matched against model names (e.g. glm)")
    e_propose.add_argument("--file", default=None, help="markdown file with the proposed content")
    e_propose.add_argument("--content", default=None, help="inline proposed content")
    e_propose.add_argument("--why", default=None, help="rationale, recorded in the log")
    e_propose.add_argument("--motivation-prompt", default=None, dest="motivation_prompt",
                           help="prompt reproducing the failure this edit fixes")
    e_propose.add_argument("--motivation-expect", action="append", dest="motivation_expect",
                           metavar="REGEX", help="motivating case: reply must match; repeatable")
    e_propose.add_argument("--motivation-tool", action="append", dest="motivation_tool",
                           metavar="NAME", help="motivating case: tool must fire; repeatable")
    e_propose.add_argument("--no-motivation", action="store_true", dest="no_motivation",
                           help="waive the improvement stage (deliberate human proposals only)")
    e_propose.add_argument("--budget", type=int, default=None,
                           help="anchor regression budget for this evaluation (default: config anchor_budget)")
    e_run = evolve_sub.add_parser(
        "run", help="evaluate synthesizer-queued proposals through the gate (live replays)"
    )
    _add_common(e_run)
    e_run.add_argument("--budget", type=int, default=None,
                       help="anchor regression budget (default: config anchor_budget)")
    for name, help_text in (
        ("pending", "list synthesizer-drafted proposals awaiting the gate"),
        ("parked", "list proposals the gate refused, with reasons"),
        ("log", "list committed evolutions"),
    ):
        e_sub = evolve_sub.add_parser(name, help=help_text)
        _add_common(e_sub)

    p_import = sub.add_parser(
        "import-settings",
        aliases=["import-claude", "import-codex"],
        help="preview or apply a non-destructive Claude/Codex-to-Fluid import",
    )
    p_import.add_argument(
        "--source",
        action="append",
        choices=["claude", "codex"],
        dest="sources",
        help="provider source to inspect; repeat to select both (default: both)",
    )
    p_import.add_argument(
        "--source-home",
        default=None,
        help="home containing provider settings (default: current home)",
    )
    p_import.add_argument(
        "--destination-home",
        default=None,
        help="home receiving .fluid content (default: current home)",
    )
    p_import.add_argument(
        "--select",
        action="append",
        dest="selected_ids",
        help="apply only this preview item ID; repeat to select multiple items",
    )
    p_import.add_argument("--apply", action="store_true", help="copy selected ready items; never overwrite")

    return parser


def _cmd_sessions(args: argparse.Namespace) -> int:
    print(format_sessions(list_sessions(args.store, limit=args.limit)))
    return 0


def _resume_target(args: argparse.Namespace, store_dir) -> str | None:
    """The session id ``--resume``/``--continue`` asks for, or None for a new one."""
    if getattr(args, "resume", None):
        return args.resume
    if getattr(args, "continue_last", False):
        latest = latest_session_id(store_dir)
        if latest is None:
            raise SessionNotFound("(no sessions to continue)")
        return latest
    return None


def _cmd_config(args: argparse.Namespace) -> int:
    config = _resolve_config(args)
    print(json.dumps(config.to_dict(), indent=2, default=str))
    return 0


def _cmd_import_settings(args: argparse.Namespace) -> int:
    from fluid.context.settings_import import (
        apply_settings_import,
        preview_settings_import,
    )

    command_defaults = {
        "import-claude": ("claude",),
        "import-codex": ("codex",),
    }
    sources = tuple(args.sources or command_defaults.get(args.command, ("codex", "claude")))
    preview = preview_settings_import(
        sources=sources,
        source_home=args.source_home,
        destination_home=args.destination_home,
    )
    result = (
        apply_settings_import(preview, selected_ids=args.selected_ids)
        if args.apply
        else preview
    )
    print(json.dumps(result.to_dict(), indent=2, default=str))
    return 0


def main(argv: list[str] | None = None) -> int:
    # Windows consoles default to a legacy code page; model output is Unicode.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass
    args = build_parser().parse_args(argv)
    if args.command == "sessions":
        return _cmd_sessions(args)
    if args.command == "config":
        return _cmd_config(args)
    if args.command in {"import-settings", "import-claude", "import-codex"}:
        return _cmd_import_settings(args)
    if args.command == "anchors":
        from fluid.cli.evolve import cmd_anchors

        return cmd_anchors(args, _resolve_config(args))
    if args.command == "evolve":
        from fluid.cli.evolve import cmd_evolve

        return cmd_evolve(args, _resolve_config(args))

    config = _resolve_config(args)
    store_dir = getattr(args, "store", None)
    store = make_store(store_dir)
    try:
        session_id = _resume_target(args, store_dir)
        if args.command == "run":
            return asyncio.run(run_once(
                config, store, args.prompt, session_id=session_id,
                on_permission=tty_permission_responder(),
            ))
        if args.command == "chat":
            return asyncio.run(
                chat_session(config, store, use_tui=args.tui, session_id=session_id)
            )
    except SessionNotFound as exc:
        print(f"fluid: no such session: {exc}", file=sys.stderr)
        return 2
    except SessionLocked as exc:
        print(f"fluid: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        # Ctrl-C outside the REPL's own handler (engine construction, MCP
        # connect, shutdown): no traceback, the conventional exit code.
        print(file=sys.stderr)
        return SIGINT_EXIT_CODE
    return 2  # unreachable: subparsers are required


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
