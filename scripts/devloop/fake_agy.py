"""A fake ``agy`` (Google Antigravity CLI) for the dev loop.

``--fake-antigravity`` points the instance's ``AI_SHUFFLE_ANTIGRAVITY_BIN`` at
a shim running fluid's scripted ACP agent (fluid/tests/fixtures/fake_acp_agent.py),
so Settings > Providers > Google Antigravity and chats on that leg work with
no Google account: ``--version``, ``acp`` (tool calls and permission prompts
when a prompt says "tool"), ``acp --login`` (prints a sign-in link, takes any
code), ``-p /usage`` and ``models``. The agent's message log and session
state stay under the instance's scratch directory.
"""
from __future__ import annotations

import json
import os
import shlex
import sys
from datetime import UTC, datetime, timedelta
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


def seed_usage() -> None:
    """Seed a signed-in row only when this disposable instance installed the fake."""
    from app.core import antigravity
    from app.db import database

    seed_file = os.environ.get("SHUFFLE_DEV_SEED_FILE")
    if not seed_file:
        return
    directory = Path(seed_file).resolve().parent
    shim = directory / "fake-agy-bin" / "agy"
    if os.environ.get(antigravity.BINARY_ENV) != str(shim) or not shim.is_file():
        return

    now = datetime.now(UTC)
    groups = []
    for name, quotas in (
        ("Gemini Models", (("gemini-weekly", "weekly", 0.8), ("gemini-5h", "5h", 0.8))),
        ("Claude and GPT models", (("3p-weekly", "weekly", 0.75), ("3p-5h", "5h", 0.75))),
        ("Gemini 4 Argon", (("gemini-argon-wtu-weekly", "weekly", 0.0),)),
    ):
        groups.append({"name": name, "buckets": [
            {"id": bucket_id, "window": period, "remaining_fraction": remaining,
             "reset_time": (now + (timedelta(days=6) if period == "weekly" else timedelta(hours=4))).isoformat()}
            for bucket_id, period, remaining in quotas
        ]})
    reading = {"status": "SUCCESS", "usage": {"input_tokens": 0},
               "command": {"name": "usage", "data": {"groups": groups}}}
    row_id = "dev-antigravity-usage"
    database.create_model_provider(
        provider_id=row_id, name="Fixture Antigravity", provider_type=antigravity.PROVIDER_TYPE,
        base_url="", auth_mode="oauth-personal", models=list(antigravity.DEFAULT_MODELS),
        extra_env={"FAKE_ACP_SIGNED_IN": "1", "FAKE_ACP_USAGE": json.dumps(reading)},
    )
    antigravity._note_signin(row_id, "signed_in")
