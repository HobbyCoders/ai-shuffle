"""``fluid`` console entry — chat, run, sessions, config, and migration.

  fluid chat [--tui]        interactive REPL (or Textual TUI)
  fluid run "<prompt>"      one-shot: stream the reply, then exit
  fluid sessions            list persisted sessions from the JSONL store
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
from typing import Any

from fluid.cli.config import host_of, list_sessions, make_engine, make_store
from fluid.cli.repl import Repl, auto_deny, stream_turn
from fluid.config.loader import load_config
from fluid.config.model import HarnessConfig


def _cli_overrides(args: argparse.Namespace) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    if getattr(args, "provider", None):
        overrides["default_provider"] = args.provider
    if getattr(args, "model", None):
        overrides["default_model"] = args.model
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
) -> int:
    """Run a single turn and stream it to ``out``. Returns a process exit code.

    ``provider`` injects a ProviderClient (offline test hook); when omitted the
    engine is built from ``config`` (real provider).
    """
    out = out or sys.stdout
    meta = await store.create_session()
    engine = make_engine(
        config, store, provider=provider, provider_name=provider_name,
        session_id=meta.session_id, permission_responder=on_permission,
    )
    host = host_of(engine)
    try:
        # Background work (Task/Monitor) parks the session instead of ending
        # it; the one-shot run keeps streaming through park and wake until
        # nothing is in flight, so it never exits with workers running.
        stop = await stream_turn(
            engine, meta.session_id, prompt, out=out, on_permission=on_permission,
            wait_background=True,
        )
    finally:
        if host is not None:
            await host.aclose()
        await engine.aclose()
    return 0 if stop != "error" else 1


async def chat_session(
    config: HarnessConfig, store, *, provider_name: str | None = None, use_tui: bool = False
) -> int:
    meta = await store.create_session()
    engine = make_engine(config, store, provider_name=provider_name, session_id=meta.session_id)
    try:
        if use_tui:
            from fluid.cli.tui import run_tui

            return await run_tui(engine, meta.session_id)
        print(f"fluid chat — session {meta.session_id} (/help, /exit)", file=sys.stderr)
        await Repl(engine, meta.session_id).run()
        return 0
    finally:
        host = host_of(engine)
        if host is not None:
            await host.aclose()
        await engine.aclose()


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fluid", description="fluid agent harness CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_chat = sub.add_parser("chat", help="interactive REPL")
    _add_common(p_chat)
    p_chat.add_argument("--tui", action="store_true", help="use the Textual TUI")

    p_run = sub.add_parser("run", help="one-shot: run a single prompt and exit")
    _add_common(p_run)
    p_run.add_argument("prompt", help="the prompt to run")

    p_sessions = sub.add_parser("sessions", help="list persisted sessions")
    p_sessions.add_argument("--store", default=None, help="JSONL session store directory")

    p_config = sub.add_parser("config", help="print the resolved config")
    _add_common(p_config)

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
    rows = list_sessions(args.store)
    if not rows:
        print("(no sessions)")
        return 0
    for row in rows:
        print(f"{row['session_id']}  nodes={row['nodes']}  created={row['created_at']:.0f}")
    return 0


def _cmd_config(args: argparse.Namespace) -> int:
    config = _resolve_config(args)
    print(json.dumps(config.to_dict(), indent=2, default=str))
    return 0


def _cmd_import_settings(args: argparse.Namespace) -> int:
    from fluid.context.settings_import import apply_settings_import, preview_settings_import

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
    args = build_parser().parse_args(argv)
    if args.command == "sessions":
        return _cmd_sessions(args)
    if args.command == "config":
        return _cmd_config(args)
    if args.command in {"import-settings", "import-claude", "import-codex"}:
        return _cmd_import_settings(args)

    config = _resolve_config(args)
    store = make_store(getattr(args, "store", None))
    if args.command == "run":
        return asyncio.run(run_once(config, store, args.prompt))
    if args.command == "chat":
        return asyncio.run(chat_session(config, store, use_tui=args.tui))
    return 2  # unreachable: subparsers are required


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
