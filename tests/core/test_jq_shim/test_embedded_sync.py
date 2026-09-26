"""
bin/jq and bin/jq.bat embed a copy of parser.py, evaluator.py and
__main__.py (hooks run them without importing the app). These tests fail
when a copy drifts from the modules, and run the copies end to end.
"""

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SHIM_DIR = Path(__file__).parent.parent.parent.parent / "app" / "core" / "jq_shim"
MODULES = ("parser.py", "evaluator.py", "__main__.py")


def _embedded_from_sh() -> str:
    text = (SHIM_DIR / "bin" / "jq").read_text()
    start = text.index("exec 3<<'PYTHON_JQ_SHIM_EOF'\n") + len("exec 3<<'PYTHON_JQ_SHIM_EOF'\n")
    end = text.index("\nPYTHON_JQ_SHIM_EOF\n", start)
    return text[start:end]


def _embedded_from_bat() -> str:
    # python -x skips the first (batch launcher) line
    return (SHIM_DIR / "bin" / "jq.bat").read_text().split("\n", 1)[1]


def _definitions(source: str) -> dict:
    """Top-level classes, functions and simple assignments, as AST dumps."""
    defs = {}
    for node in ast.parse(source).body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            defs[node.name] = ast.dump(node)
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            defs[node.targets[0].id] = ast.dump(node)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            defs[node.target.id] = ast.dump(node)
    return defs


@pytest.mark.parametrize("loader", [_embedded_from_sh, _embedded_from_bat], ids=["jq", "jq.bat"])
def test_embedded_copy_matches_modules(loader):
    embedded = _definitions(loader())
    for module in MODULES:
        for name, dump in _definitions((SHIM_DIR / module).read_text()).items():
            assert name in embedded, f"{name} from {module} is missing from the embedded shim"
            assert embedded[name] == dump, (
                f"{name} in the embedded shim differs from {module}; regenerate bin/jq and bin/jq.bat with scripts/generate_jq_shim_embedded.py"
            )


def test_embedded_copies_are_identical():
    assert _embedded_from_sh() == _embedded_from_bat().rstrip("\n")


def test_bat_launcher_line():
    """Line 1 must be the cmd launcher (python -x skips it); the rest must be Python."""
    first, rest = (SHIM_DIR / "bin" / "jq.bat").read_text().split("\n", 1)
    assert first.startswith("@") and ' -x "%~f0" %* & goto :eof' in first
    compile(rest, "jq.bat", "exec")


def test_bat_launcher_honours_jq_shim_python():
    """hook_service exports JQ_SHIM_PYTHON because bare ``python`` on Windows is
    often the Microsoft Store stub; the .bat must prefer it, as the bash shim
    does, and fall back to ``python`` when it is unset.

    The launcher is ``for /f "tokens=1 delims=|" %%P in ("%JQ_SHIM_PYTHON%|python")``:
    cmd skips leading delimiters, so the first token is the variable when set
    and ``python`` when it expands to nothing. It stays on one line (python -x
    skips only line 1), and ``%*`` is never inside a parenthesised block, where
    an unquoted ``)`` in a jq argument would end the block early.
    """
    import re

    first = (SHIM_DIR / "bin" / "jq.bat").read_text().split("\n", 1)[0]
    m = re.fullmatch(
        r'@for /f "tokens=1 delims=\|" %%P in \("(%JQ_SHIM_PYTHON%)\|python"\) '
        r'do @"%%~P" -x "%~f0" %\* & goto :eof',
        first,
    )
    assert m, first

    def first_token(value):  # cmd's for /f tokenising of the in(...) string
        return next(t for t in f"{value}|python".split("|") if t)

    assert first_token("") == "python"
    assert first_token(r"C:\Program Files (x86)\Python\python.exe") == (
        r"C:\Program Files (x86)\Python\python.exe"
    )


@pytest.mark.parametrize("name", ["jq", "jq.bat"])
def test_marker_near_top(name):
    """find_real_jq() recognises shim copies by this marker in the first 1 KiB."""
    from app.core.jq_shim.__main__ import SHIM_MARKER

    head = (SHIM_DIR / "bin" / name).read_bytes()[:1024]
    assert SHIM_MARKER.encode() in head


def _run_bat_copy(*args, input_data):
    env = os.environ.copy()
    env["JQ_SHIM_NO_FALLBACK"] = "1"
    return subprocess.run(
        [sys.executable, "-x", str(SHIM_DIR / "bin" / "jq.bat"), *args],
        input=input_data, capture_output=True, text=True, env=env, timeout=60,
    )


def test_bat_copy_runs_under_python_x():
    result = _run_bat_copy("-c", "[.[] | .a]", input_data='[{"a":1},{"a":2}]')
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [1, 2]


@pytest.mark.skipif(sys.platform == "win32", reason="bash shim is exercised via Git Bash elsewhere")
def test_bash_copy_regressions():
    env = os.environ.copy()
    env["JQ_SHIM_NO_FALLBACK"] = "1"
    cases = [
        ("[.[] | .a]", '[{"a":1},{"a":2}]', "[1,2]"),
        (".[] | select(.a > 1) | .a", '[{"a":1},{"a":2}]', "2"),
        (".[] | .a | tostring", '[{"a":1}]', '"1"'),
        (".d | not", '{"d":false}', "true"),
    ]
    for program, data, expected in cases:
        result = subprocess.run(
            ["bash", str(SHIM_DIR / "bin" / "jq"), "-c", program],
            input=data, capture_output=True, text=True, env=env, timeout=60,
        )
        assert result.returncode == 0, (program, result.stderr)
        assert result.stdout.strip() == expected, program
