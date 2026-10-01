"""Regenerate the Python embedded in app/core/jq_shim/bin/jq and bin/jq.bat from the package modules."""
import ast, sys
from pathlib import Path

ROOT = Path(sys.argv[1])
PKG = ROOT / "app/core/jq_shim"

HEADER = '''#!/usr/bin/env python3
"""
Self-contained Python jq shim - CLI compatible with jq.

This module provides a command-line interface compatible with jq,
allowing shell scripts to use jq syntax while the processing is
handled by Python. All code is embedded - no external imports needed.

GENERATED from app/core/jq_shim/{parser,evaluator,__main__}.py - edit those
files and regenerate; tests/core/test_jq_shim checks the copies match.
"""

import sys
import json
import argparse
import io
import math
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union
from enum import Enum
'''


def body(path: Path) -> str:
    src = path.read_text()
    tree = ast.parse(src)
    lines = src.splitlines()
    drop = set()
    for i, node in enumerate(tree.body):
        strip = (
            (i == 0 and isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant))
            or isinstance(node, (ast.Import, ast.ImportFrom))
            or (isinstance(node, ast.If) and "sys.platform" in ast.unparse(node.test))
            or (isinstance(node, ast.If) and "__name__" in ast.unparse(node.test))
        )
        if strip:
            start = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])
            drop.update(range(start, node.end_lineno + 1))
    first = min(
        min([n.lineno] + [d.lineno for d in getattr(n, "decorator_list", [])])
        for n in tree.body
        if not any(ln in drop for ln in range(n.lineno, n.end_lineno + 1))
    )
    while first > 1 and lines[first - 2].startswith("#"):
        first -= 1
    drop.update(range(1, first))
    kept = [l for n, l in enumerate(lines, 1) if n not in drop]
    # drop comment lines that only introduced a stripped block, and leading blanks
    text = "\n".join(kept).strip("\n")
    return text


parts = [HEADER.rstrip("\n")]
for name in ("parser.py", "evaluator.py", "__main__.py"):
    parts.append("\n\n# " + "=" * 77 + f"\n# {name}\n# " + "=" * 77 + "\n\n" + body(PKG / name))
parts.append('\n\nif __name__ == "__main__":\n    _configure_console_encoding()\n    sys.exit(main())\n')
python = "\n".join(parts)
python = python.replace("\n\n\n\n", "\n\n\n")
compile(python, "embedded", "exec")

sh = PKG / "bin/jq"
text = sh.read_text()
head, rest = text.split("exec 3<<'PYTHON_JQ_SHIM_EOF'\n", 1)
_, tail = rest.split("\nPYTHON_JQ_SHIM_EOF\n", 1)
sh.write_text(head + "exec 3<<'PYTHON_JQ_SHIM_EOF'\n" + python.rstrip("\n") + "\nPYTHON_JQ_SHIM_EOF\n" + tail)

bat = PKG / "bin/jq.bat"
# python -x skips the first line, so the batch launcher must be line 1 and
# everything after it must be valid Python. It runs JQ_SHIM_PYTHON when set
# (hook_service exports sys.executable: bare python on Windows is often the
# Store stub), else python: for /f skips the leading "|" of an empty variable.
# %* stays outside any parenthesised block, where an unquoted ")" in a jq
# argument would end the block.
BAT_LAUNCHER = (
    '@for /f "tokens=1 delims=|" %%P in ("%JQ_SHIM_PYTHON%|python") '
    'do @"%%~P" -x "%~f0" %* & goto :eof\n'
)
bat.write_text(BAT_LAUNCHER + python)
print("ok", len(python.splitlines()), "lines")
