"""The bare fluid arm: ``fluid run``'s own one-shot path on the ChatGPT login.

``fluid run`` itself only takes API keys, so this driver calls the same
``run_once`` the CLI calls, with fluid's Codex client injected through
:class:`metered.MeteredClient`. Nothing from the AI Shuffle app is involved:
no app prompt, memory, learner or tools beyond fluid's built-ins.

    python -m scripts.harness_h2h.fluid_cli_arm --cwd WORKSPACE --prompt-file P \
        --store STORE --calls calls.jsonl

Run it with ``HOME`` pointed at an empty directory (the runner does) so the
user's ``~/.fluid`` config, skills and memory stay out of the bare arm, the
same way the Claude Code arm runs on an empty ``CLAUDE_CONFIG_DIR``.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from fluid.cli.__main__ import run_once
from fluid.cli.config import make_store
from fluid.config.loader import load_config

from scripts.harness_h2h.metered import MeteredClient, make_codex_client


async def run(args: argparse.Namespace) -> int:
    overrides = {
        "default_provider": "openai",
        "model_override": args.model,
        "permission": {"approval_policy": "never_ask"},
    }
    config = load_config(cwd=str(args.cwd), overrides=overrides)
    client = MeteredClient(make_codex_client(args.token), args.calls, effort=args.effort)
    store = make_store(str(args.store))
    prompt = Path(args.prompt_file).read_text(encoding="utf-8")
    # never_ask: no permission prompt is raised, exactly as `fluid run
    # --approval-policy never_ask` behaves.
    return await run_once(config, store, prompt, provider=client, out=sys.stdout)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="harness_h2h.fluid_cli_arm")
    parser.add_argument("--cwd", type=Path, required=True)
    parser.add_argument("--prompt-file", type=Path, required=True)
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--calls", type=Path, required=True)
    parser.add_argument("--model", default="gpt-6.1-sol")
    parser.add_argument("--effort", default="high")
    parser.add_argument("--token", type=Path, required=True,
                        help="bench login file (HOME is isolated, so give it explicitly)")
    args = parser.parse_args(argv)
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
