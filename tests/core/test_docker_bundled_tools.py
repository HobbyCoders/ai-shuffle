"""Bundled tool checks exercise entrypoint shell logic without privileged setup."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ASSETS = [f"{tool}/{asset}" for tool in ("ai-tools", "browser-mcp")
          for asset in ("package.json", "dist/index.js", "node_modules")]


def run_check(tmp_path, missing=(), args=()):
    source = (ROOT / "entrypoint.sh").read_text()
    match = re.search(r"^require_bundled_tools\(\) \{\n.*?^\}", source, re.M | re.S)
    assert match, "entrypoint must validate bundled tools before setup"
    for asset in ASSETS:
        if asset in missing:
            continue
        path = tmp_path / asset
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.name == "node_modules":
            path.mkdir()
        else:
            path.write_text("{}")
    function = match.group().replace("/opt/ai-tools", str(tmp_path / "ai-tools"))
    function = function.replace("/opt/browser-mcp", str(tmp_path / "browser-mcp"))
    script = 'set -e\nnpm() { touch npm-called; return 99; }\n' + function + '\nrequire_bundled_tools "$@"'
    result = subprocess.run([shutil.which("bash"), "-c", script, "--", *args],
                            cwd=tmp_path, capture_output=True, text=True, timeout=10)
    assert not (tmp_path / "npm-called").exists()
    return result


@pytest.mark.parametrize("args", [(), ("run",)])
def test_complete_bundled_tools_start_without_install(tmp_path, args):
    result = run_check(tmp_path, args=args)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("missing", ASSETS)
def test_missing_tool_asset_refuses_with_repair_diagnostic(tmp_path, missing):
    result = run_check(tmp_path, missing=(missing,))
    assert result.returncode != 0
    assert "Bundled tool assets are missing" in result.stderr
    assert "replace or rebuild the image" in result.stderr


@pytest.mark.parametrize("action", ["dump", "restore"])
def test_maintenance_does_not_require_tool_assets(tmp_path, action):
    assert run_check(tmp_path, missing=ASSETS, args=(action,)).returncode == 0


def test_entrypoint_never_installs_or_chowns_bundled_tools():
    source = (ROOT / "entrypoint.sh").read_text()
    assert not re.search(r"\bnpm\s+(?:install|ci)\b", source)
    assert not re.search(r"^.*(?:chown.*?/opt/|/opt/.*?chown).*", source, re.M)
    assert source.index('require_bundled_tools "$@"') < source.index('if [ "$(id -u)" != "0" ]')


def test_docker_tool_assets_are_root_owned_and_not_group_writable():
    source = (ROOT / "Dockerfile").read_text()
    copies = [line for line in source.splitlines()
              if line.startswith("COPY ") and ("/opt/ai-tools" in line or "/opt/browser-mcp" in line)]
    assert len(copies) == 4
    assert all("--chown=root:root" in line for line in copies)
    assert "RUN chmod -R go-w /opt/ai-tools /opt/browser-mcp" in source
