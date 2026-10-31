"""Guard readiness for unrelated scripted CLI tests, not marker filtering.

The stock-CLI/provider contract tests exercise the actual shipped function hook.
"""
import os
from pathlib import Path


def announce_tool_output_guard():
    receipt = os.environ.pop("FLUID_TOOL_OUTPUT_GUARD_RECEIPT", "")
    nonce = os.environ.pop("FLUID_TOOL_OUTPUT_GUARD_NONCE", "")
    if receipt and nonce:
        Path(receipt).write_text(nonce, encoding="utf-8")
