"""C12: runtime/builds and tests must remain independent of the removed SDK."""
import ast
import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SDK = "claude_agent_sdk"


def test_clean_environment_has_no_agent_sdk():
    assert importlib.util.find_spec(SDK) is None, "Run these tests in a clean SDK-free environment"


def test_no_runtime_or_test_sdk_imports():
    violations = []
    for folder in ("app", "fluid/fluid", "tests", "fluid/tests", "tools", "scripts"):
        for path in (ROOT / folder).rglob("*.py"):
            if "experiments" in path.parts:
                continue  # explicitly archived research, never shipped as runtime
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                imported = []
                if isinstance(node, ast.Import):
                    imported = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    imported = [node.module or ""]
                elif isinstance(node, ast.Call) and isinstance(node.func, (ast.Name, ast.Attribute)):
                    name = node.func.id if isinstance(node.func, ast.Name) else node.func.attr
                    if name in ("__import__", "import_module") and node.args and isinstance(node.args[0], ast.Constant):
                        imported = [node.args[0].value]
                if any(isinstance(name, str) and (name == SDK or name.startswith(SDK + ".")) for name in imported):
                    violations.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert violations == []


def test_no_sdk_or_bundled_cli_dependency():
    manifests = list(ROOT.glob("requirements*.txt")) + list(ROOT.glob("requirements*.lock"))
    manifests += [ROOT / "fluid/pyproject.toml", ROOT / "desktop/package.json", ROOT / "desktop/package-lock.json"]
    for path in manifests:
        assert not re.search(r"claude[-_]agent[-_]sdk|@anthropic-ai/claude-code", path.read_text(encoding="utf-8"), re.I), path
    assert not list((ROOT / "app").rglob("_bundled/claude*"))
    # Packaging must fail even if a future transitive package pulls the SDK back in.
    for name in ("Dockerfile", "desktop/scripts/build-worker.mjs"):
        assert 'find_spec' in (ROOT / name).read_text(encoding="utf-8")
