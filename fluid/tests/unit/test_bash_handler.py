"""Bash handler: no stdin, bounded capture, a clamped timeout.

Real subprocesses, all short: a command that reads stdin gets EOF instead of
the user's keystrokes, a command that prints without end cannot grow the host,
and the timeout the model asks for is held inside sane bounds.
"""

from __future__ import annotations

import sys

from fluid.tools.builtins.bash import (
    DEFAULT_TIMEOUT_S,
    MAX_CAPTURE_BYTES,
    MAX_TIMEOUT_S,
    MIN_TIMEOUT_S,
    clamp_timeout,
)
from fluid.tools.builtins.bash import handler as bash_handler
from fluid.tools.registry import ToolCtx


def _py(code: str) -> str:
    return f'"{sys.executable}" -c "{code}"'


async def test_stdin_is_closed_not_inherited(tmp_path) -> None:
    out = await bash_handler(
        {"command": _py("import sys; print(repr(sys.stdin.read()))")},
        ToolCtx(cwd=str(tmp_path)),
    )
    assert not out.is_error, out.text
    assert out.text.strip() == "''"


async def test_runaway_output_is_capped_while_streaming() -> None:
    # Four times the capture cap flows through; the reader keeps head and
    # tail and says how much fell out in between, without ever holding more
    # than the cap.
    import asyncio

    from fluid.tools.builtins.bash import _read_capped

    reader = asyncio.StreamReader()
    total = MAX_CAPTURE_BYTES * 4
    for offset in range(0, total, 65536):
        reader.feed_data(b"x" * min(65536, total - offset))
    reader.feed_eof()
    captured = await _read_capped(reader)
    assert b"bytes dropped while streaming" in captured
    assert len(captured) <= MAX_CAPTURE_BYTES + 100


async def test_runaway_command_output_is_bounded(tmp_path) -> None:
    lines = MAX_CAPTURE_BYTES // 100 * 4
    out = await bash_handler(
        {"command": _py(f"[print('x' * 99) for _ in range({lines})]")},
        ToolCtx(cwd=str(tmp_path)),
    )
    assert not out.is_error, out.text[:200]
    assert len(out.text) < MAX_CAPTURE_BYTES // 4


async def test_bounded_output_is_untouched(tmp_path) -> None:
    out = await bash_handler(
        {"command": _py("print('hello')")}, ToolCtx(cwd=str(tmp_path))
    )
    assert out.text.strip() == "hello"
    assert "dropped" not in out.text


async def test_timeout_points_to_monitor_not_detached_polling(tmp_path) -> None:
    out = await bash_handler(
        {"command": _py("import time; time.sleep(10)"), "timeout": 1},
        ToolCtx(cwd=str(tmp_path)),
    )
    assert out.is_error and not out.soft_error
    assert "was killed" in out.text
    assert "use Monitor" in out.text
    assert "Do not append & or poll" in out.text
    assert "Inspect any partial effects" in out.text


def test_timeout_is_clamped_in_both_directions() -> None:
    assert clamp_timeout(0) == MIN_TIMEOUT_S
    assert clamp_timeout(-5) == MIN_TIMEOUT_S
    assert clamp_timeout(10**9) == MAX_TIMEOUT_S
    assert clamp_timeout(30) == 30.0
    assert clamp_timeout("not a number") == DEFAULT_TIMEOUT_S
    assert clamp_timeout(None) == DEFAULT_TIMEOUT_S
    assert clamp_timeout(float("nan")) == DEFAULT_TIMEOUT_S
