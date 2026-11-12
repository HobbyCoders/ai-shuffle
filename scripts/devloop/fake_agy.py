"""A fake ``agy`` (Google Antigravity CLI) for the dev loop.

``--fake-antigravity`` points the instance's ``AI_SHUFFLE_ANTIGRAVITY_BIN`` at
a shim running fluid's scripted ACP agent (fluid/tests/fixtures/fake_acp_agent.py),
so Settings > Providers > Google Antigravity and chats on that leg work with
no Google account: ``--version``, ``acp`` (tool calls and permission prompts
when a prompt says "tool"), ``acp --login`` (prints a sign-in link, takes any
code) and ``-p /usage``. The agent's message log and session state stay under
the instance's scratch directory.
"""
from __future__ import annotations

import shlex
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
AGENT = ROOT / "fluid" / "tests" / "fixtures" / "fake_acp_agent.py"


def install(directory: Path, *, python: str | None = None) -> Path:
    """Write the ``agy`` shim under ``directory`` and return its path."""
    directory = Path(directory).resolve()
    bin_dir = directory / "fake-agy-bin"
    bin_dir.mkdir(mode=0o700, exist_ok=True)
    state = directory / "fake-agy-state"
    state.mkdir(mode=0o700, exist_ok=True)
    shim = bin_dir / "agy"
    shim.write_text("\n".join([
        "#!/bin/sh",
        f"FAKE_ACP_LOG={shlex.quote(str(log_path(directory)))}",
        f"FAKE_ACP_STATE={shlex.quote(str(state))}",
        "export FAKE_ACP_LOG FAKE_ACP_STATE",
        f'exec {shlex.quote(python or sys.executable)} {shlex.quote(str(AGENT))} "$@"',
        "",
    ]), encoding="utf-8")
    shim.chmod(0o700)
    return shim


def log_path(directory: Path) -> Path:
    return Path(directory).resolve() / "fake-agy.jsonl"
