"""Credential files the app and Fluid write are sensitive to Fluid's tools.

Fluid's Bash risk classifier, its Read/Grep/Glob/Edit/Write ask-rules and the
delegated leg's PreToolUse hook all key off
``fluid.tools.hardening.is_sensitive_path``.

Each credential file the app or Fluid writes is checked below, with its path
built from the writer's own constants. There is no census of writers: a text
scan for owner-only markers also flags readers and misses writers that use
default permissions, so it cannot prove completeness either way.
"""

from __future__ import annotations

import ast
import re
import tempfile
import uuid
from pathlib import Path

import pytest
from fluid.tools.builtins.bash_risk import classify
from fluid.tools.hardening import is_sensitive_path

_REPO = Path(__file__).resolve().parents[2]


def _assert_sensitive(path: Path | str) -> None:
    text = str(path)
    assert is_sensitive_path(text), text
    assert classify({"command": f"cat '{text}'"}) == "write", text


def _module_constant(relative: str, name: str) -> str:
    """A string constant from a module that cannot be imported everywhere
    (the container runtime imports POSIX-only modules)."""
    tree = ast.parse((_REPO / relative).read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name for target in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{relative} no longer defines {name}")


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    return tmp_path


def test_credential_writers_paths_are_sensitive(data_dir: Path) -> None:
    from fluid.providers.openai_responses import codex_oauth

    from app import preview_run
    from app.core import (
        claude_cli_home,
        credential_keys,
        event_listener,
        managed_credentials,
        preview_access,
    )
    from app.network import identity

    checked = {
        "app/core/preview_access.py": [preview_access._key_path()],
        "app/core/credential_keys.py": [credential_keys.key_path(str(uuid.uuid4()), create=True)],
        "app/core/managed_credentials.py": [managed_credentials._key_path()],
        "app/network/identity.py": [data_dir / ".credential-keys" / identity.IDENTITY_FILE],
        "app/preview_run.py": [Path(tempfile.gettempdir()) / "devloop-x" / preview_run.DEV_ACCESS_FILE],
        "app/core/event_listener.py": [
            Path(tempfile.gettempdir()) / f"{event_listener.REQUEST_DIR_PREFIX}ab12cd" / event_listener.REQUEST_FILE
        ],
        "docker/container_runtime.py": [
            data_dir / _module_constant("docker/container_runtime.py", "DESCRIPTOR_FILE"),
            data_dir / "postgres" / _module_constant("docker/container_runtime.py", "PASSWORD_FILE"),
        ],
        "fluid/fluid/providers/openai_responses/codex_oauth.py": [codex_oauth.DEFAULT_TOKEN_PATH],
    }
    for paths in checked.values():
        for path in paths:
            _assert_sensitive(path)
    # The Claude CLI sign-ins the app keeps per account (written by the CLI).
    _assert_sensitive(claude_cli_home.account_dir("0123456789abcdef") / ".credentials.json")
    _assert_sensitive(claude_cli_home.root() / "any" / "settings.json")


def test_identity_key_lives_in_the_credential_key_directory() -> None:
    source = (_REPO / "app/network/identity.py").read_text(encoding="utf-8")
    assert re.search(r"effective_data_dir\s*/\s*[\"']\.credential-keys[\"']", source)


# ─── S2: the app's own sessions apply the shared credential policy ───────────


def _spec(name: str):
    from fluid.tools.builtins import edit, glob, grep, read, write

    return {"Read": read.SPEC, "Grep": grep.SPEC, "Glob": glob.SPEC, "Edit": edit.SPEC, "Write": write.SPEC}[name]


_PATH_FIELD = {"Read": "file_path", "Edit": "file_path", "Write": "file_path", "Grep": "path", "Glob": "path"}


def _app_engine(permission_mode: str = "default", **extra):
    from app.core.provider.harness_adapter import HarnessAdapter
    from app.core.provider.types import SessionOptions

    return HarnessAdapter()._build_permissions(
        SessionOptions(model="gpt-5", permission_mode=permission_mode, **extra)
    )


@pytest.mark.parametrize("tool", ["Read", "Grep", "Glob", "Edit", "Write"])
@pytest.mark.parametrize("permission_mode", ["default", "acceptEdits"])
def test_app_native_session_asks_before_touching_a_credential_file(tmp_path, tool, permission_mode) -> None:
    """Code mode runs on the same engine (its file access goes through these
    tools' permission checks), so this covers it too."""
    engine = _app_engine(permission_mode)
    home = Path.home()
    if tool == "Glob":  # Glob's path is a folder to search
        targets = [str(tmp_path / ".credential-keys"), str(tmp_path / ".managed-connection")]
    else:
        targets = [
            str(home / ".docker" / "config.json"), str(tmp_path / ".env"),
            str(home / ".aws" / "sso" / "cache" / "0a1b2c.json"), str(tmp_path / ".yarnrc.yml"),
        ]
    for target in targets:
        verdict = engine.decide(_spec(tool), {_PATH_FIELD[tool]: target, "pattern": "*"}, cwd=str(tmp_path))
        assert verdict.decision == "ask", (tool, target, verdict)


def test_app_native_session_reads_ordinary_files_without_asking(tmp_path) -> None:
    engine = _app_engine()
    verdict = engine.decide(_spec("Read"), {"file_path": str(tmp_path / "README.md")}, cwd=str(tmp_path))
    assert verdict.decision == "allow"
    verdict = engine.decide(_spec("Read"), {"file_path": ".env.example"}, cwd=str(tmp_path))
    assert verdict.decision == "allow"


def test_app_native_relative_credential_read_asks(tmp_path) -> None:
    engine = _app_engine()
    verdict = engine.decide(_spec("Read"), {"file_path": ".env"}, cwd=str(tmp_path))
    assert verdict.decision == "ask"


def test_accept_edits_still_asks_before_editing_a_credential_file() -> None:
    from app.core.session_modes import mode_approves_without_prompt

    assert mode_approves_without_prompt("acceptEdits", "Edit", {"file_path": "src/app.py"})
    assert not mode_approves_without_prompt("acceptEdits", "Edit", {"file_path": ".env"})
    assert not mode_approves_without_prompt("acceptEdits", "Write", {"file_path": "/home/u/.docker/config.json"})
    assert not mode_approves_without_prompt("acceptEdits", "NotebookEdit", {"notebook_path": "deploy/.env"})
    # Bypass stays silent; the mode table is unchanged for everything else.
    assert mode_approves_without_prompt("bypassPermissions", "Edit", {"file_path": ".env"})
    assert mode_approves_without_prompt("acceptEdits", "Edit")


def test_configured_rules_keep_precedence_over_the_credential_asks(tmp_path) -> None:
    """The credential asks go first, so a later rule still wins (last match
    wins): the chat lane's rules follow them unchanged, and an allow rule
    placed after them lets one credential file through."""
    from fluid.permissions.engine import PermissionEngine
    from fluid.permissions.rules import Rule

    from app.core.provider import harness_adapter
    from app.core.provider.types import SessionOptions

    plain = harness_adapter._native_permission_rules(SessionOptions(model="gpt-5"))
    assert plain[0].decision == "ask"
    chat_options = SessionOptions(model="gpt-5", extra={"session_mode": "chat"})
    chat = harness_adapter._chat_mode_rules(chat_options)
    assert chat
    assert harness_adapter._native_permission_rules(chat_options) == [*plain, *chat]
    env = str(tmp_path / ".env")
    engine = PermissionEngine(
        approval_policy="on_write",
        rules=[*plain, Rule(tool="Read", decision="allow", input_globs={"file_path": env})],
    )
    assert engine.decide(_spec("Read"), {"file_path": env}, cwd=str(tmp_path)).decision == "allow"
