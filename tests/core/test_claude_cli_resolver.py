"""The delegated leg runs only the stock ``claude`` found on PATH.

The copy bundled inside the ``claude_agent_sdk`` wheel is vendored and
version-frozen, so the resolver must never hand it to the engine, and a missing
CLI must be a typed setup error rather than a silent fallback.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.core.provider import harness_adapter as ha
from fluid.engine.delegated import CLAUDE_CLI_MISSING_MESSAGE, ClaudeCliMissingError

_CLI_NAME = "claude.exe" if os.name == "nt" else "claude"


def _fake_cli(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    binary = directory / _CLI_NAME
    binary.write_text("")
    binary.chmod(0o755)
    return binary


@pytest.fixture
def bundled_dir(tmp_path: Path) -> Path:
    """A fake wheel layout: site-packages/claude_agent_sdk/_bundled/claude."""
    directory = tmp_path / "lib" / "site-packages" / "claude_agent_sdk" / "_bundled"
    _fake_cli(directory)
    return directory


def test_resolves_the_cli_on_path(tmp_path, monkeypatch):
    binary = _fake_cli(tmp_path / "bin")
    monkeypatch.setenv("PATH", str(binary.parent))
    assert [Path(p).resolve() for p in ha._resolve_claude_cmd()] == [binary.resolve()]


def test_skips_a_bundled_cli_on_path(tmp_path, bundled_dir, monkeypatch):
    stock = _fake_cli(tmp_path / "bin")
    monkeypatch.setenv("PATH", os.pathsep.join([str(bundled_dir), str(stock.parent)]))
    (resolved,) = ha._resolve_claude_cmd()
    assert Path(resolved).resolve() == stock.resolve()
    assert "site-packages" not in Path(resolved).resolve().parts


def test_missing_cli_raises_typed_setup_error(bundled_dir, monkeypatch):
    # Only the vendored copy is reachable: that counts as missing.
    monkeypatch.setenv("PATH", str(bundled_dir))
    with pytest.raises(ClaudeCliMissingError) as caught:
        ha._resolve_claude_cmd()
    assert str(caught.value) == CLAUDE_CLI_MISSING_MESSAGE


def test_empty_path_raises_typed_setup_error(monkeypatch):
    monkeypatch.setenv("PATH", "")
    with pytest.raises(ClaudeCliMissingError):
        ha._resolve_claude_cmd()


def test_real_environment_never_resolves_under_site_packages():
    try:
        cmd = ha._resolve_claude_cmd()
    except ClaudeCliMissingError:
        return
    parts = {part.lower() for part in Path(cmd[0]).resolve().parts}
    assert "site-packages" not in parts
    assert "dist-packages" not in parts


def test_connection_error_text_is_the_setup_message():
    from app.core.query_persistence import _extract_connection_error

    assert _extract_connection_error(ClaudeCliMissingError(), None) == CLAUDE_CLI_MISSING_MESSAGE
