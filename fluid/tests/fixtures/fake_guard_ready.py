"""Guard readiness for unrelated scripted CLI tests, not marker filtering.

The stock-CLI/provider contract tests exercise the actual shipped function hook.
"""
import os
import sys
from pathlib import Path


def announce_tool_output_guard():
    receipt = os.environ.pop("FLUID_TOOL_OUTPUT_GUARD_RECEIPT", "")
    nonce = os.environ.pop("FLUID_TOOL_OUTPUT_GUARD_NONCE", "")
    staged = any(arg == "--plugin-dir" and i + 1 < len(sys.argv)
                 and Path(sys.argv[i + 1]).name == "tool-output-guard"
                 and (Path(sys.argv[i + 1]) / "hooks" / "register.ts").is_file()
                 for i, arg in enumerate(sys.argv))
    if staged and receipt and nonce:
        Path(receipt).write_text(nonce, encoding="utf-8")
