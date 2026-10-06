"""Bash commands run below the host's CPU priority (FLUID_CHILD_NICE).

Unit tests drive both platform branches from any OS: the POSIX branch with a
stand-in for os.setpriority, the Windows branch by inspecting creationflags.
The last two tests spawn a real command through the Bash handler and have
the Python it starts report its own priority.
"""

from __future__ import annotations

import os
import sys
import types

import pytest

from fluid.tools import priority
from fluid.tools.builtins import bash
from fluid.tools.builtins.bash import handler as bash_handler
from fluid.tools.registry import ToolCtx

_BELOW_NORMAL = 0x00004000


class _PriorityRecorder:
    def __init__(self, base: int = 0, error: Exception | None = None):
        self.base = base
        self.error = error
        self.calls: list[tuple[int, int, int]] = []

    def install(self, monkeypatch):
        monkeypatch.setattr(os, "getpriority", lambda which, who: self.base, raising=False)
        monkeypatch.setattr(os, "setpriority", self.setpriority, raising=False)
        return self

    def setpriority(self, which, who, value):
        self.calls.append((which, who, value))
        if self.error is not None:
            raise self.error


def _as_windows(monkeypatch) -> None:
    fake_sys = types.SimpleNamespace(platform="win32")
    monkeypatch.setattr(priority, "sys", fake_sys)
    monkeypatch.setattr(bash, "sys", fake_sys)
    monkeypatch.setattr(priority, "BELOW_NORMAL_PRIORITY_CLASS", _BELOW_NORMAL)


# -- FLUID_CHILD_NICE ----------------------------------------------------------


def test_default_when_unset_or_blank(monkeypatch) -> None:
    monkeypatch.delenv(priority.CHILD_NICE_ENV, raising=False)
    assert priority.child_nice() == 10
    monkeypatch.setenv(priority.CHILD_NICE_ENV, "  ")
    assert priority.child_nice() == 10


@pytest.mark.parametrize(("raw", "expected"), [("0", 0), ("5", 5), (" 19 ", 19)])
def test_valid_values(monkeypatch, raw, expected) -> None:
    monkeypatch.setenv(priority.CHILD_NICE_ENV, raw)
    assert priority.child_nice() == expected


@pytest.mark.parametrize("raw", ["abc", "-3", "20", "1.5", "ten"])
def test_bad_values_fall_back_to_default(monkeypatch, raw) -> None:
    monkeypatch.setenv(priority.CHILD_NICE_ENV, raw)
    assert priority.child_nice() == priority.DEFAULT_CHILD_NICE


# -- POSIX: process-group nice -------------------------------------------------


def test_sets_the_group_relative_to_the_host(monkeypatch) -> None:
    monkeypatch.setenv(priority.CHILD_NICE_ENV, "10")
    rec = _PriorityRecorder(base=3).install(monkeypatch)
    priority.lower_process_group_priority(1234)
    assert rec.calls == [(getattr(os, "PRIO_PGRP", 1), 1234, 13)]


def test_capped_at_19(monkeypatch) -> None:
    rec = _PriorityRecorder(base=15).install(monkeypatch)
    priority.lower_process_group_priority(1234, nice=10)
    assert rec.calls[0][2] == 19


def test_off_when_zero(monkeypatch) -> None:
    monkeypatch.setenv(priority.CHILD_NICE_ENV, "0")
    rec = _PriorityRecorder().install(monkeypatch)
    priority.lower_process_group_priority(1234)
    assert rec.calls == []


@pytest.mark.parametrize("error", [ProcessLookupError(), PermissionError(), OSError()])
def test_failure_is_swallowed(monkeypatch, error) -> None:
    rec = _PriorityRecorder(error=error).install(monkeypatch)
    priority.lower_process_group_priority(1234, nice=10)
    assert len(rec.calls) == 1


def test_no_setpriority_is_a_no_op(monkeypatch) -> None:
    monkeypatch.delattr(os, "setpriority", raising=False)
    priority.lower_process_group_priority(1234, nice=10)


# -- Windows: priority class -------------------------------------------------------


def test_windows_bash_starts_below_normal(monkeypatch) -> None:
    _as_windows(monkeypatch)
    monkeypatch.setenv(priority.CHILD_NICE_ENV, "10")
    assert bash._spawn_kwargs() == {"creationflags": _BELOW_NORMAL}


def test_windows_off_when_zero(monkeypatch) -> None:
    _as_windows(monkeypatch)
    monkeypatch.setenv(priority.CHILD_NICE_ENV, "0")
    assert bash._spawn_kwargs() == {}


def test_posix_bash_kwargs_unchanged(monkeypatch) -> None:
    fake_sys = types.SimpleNamespace(platform="linux")
    monkeypatch.setattr(priority, "sys", fake_sys)
    monkeypatch.setattr(bash, "sys", fake_sys)
    assert bash._spawn_kwargs() == {"start_new_session": True}
    assert priority.priority_creationflags(10) == 0


# -- the real handler ------------------------------------------------------------


async def test_command_still_runs_when_setpriority_fails(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv(priority.CHILD_NICE_ENV, "10")
    rec = _PriorityRecorder(error=ProcessLookupError()).install(monkeypatch)
    out = await bash_handler(
        {"command": f'"{sys.executable}" -c "print(42)"'}, ToolCtx(cwd=str(tmp_path))
    )
    assert not out.is_error, out.text
    assert out.text.strip() == "42"
    assert len(rec.calls) == 1


_PROBE = (
    "import os, sys\n"
    "if sys.platform == 'win32':\n"
    "    import ctypes\n"
    "    k = ctypes.windll.kernel32\n"
    "    k.GetCurrentProcess.restype = ctypes.c_void_p\n"
    "    k.GetPriorityClass.argtypes = [ctypes.c_void_p]\n"
    "    print(hex(k.GetPriorityClass(k.GetCurrentProcess())))\n"
    "else:\n"
    "    print(os.getpriority(os.PRIO_PROCESS, 0))\n"
)


async def test_command_reports_lowered_priority(monkeypatch, tmp_path) -> None:
    """The Python the shell starts (a grandchild of the host) runs lowered."""
    monkeypatch.setenv(priority.CHILD_NICE_ENV, "10")
    script = tmp_path / "prio.py"
    script.write_text(_PROBE, encoding="utf-8")
    out = await bash_handler(
        {"command": f'sleep 0.2; "{sys.executable}" "{script}"'},
        ToolCtx(cwd=str(tmp_path)),
    )
    assert not out.is_error, out.text
    if sys.platform == "win32":
        assert out.text.strip() == hex(_BELOW_NORMAL)
    else:
        expected = min(19, os.getpriority(os.PRIO_PROCESS, 0) + 10)
        assert out.text.strip() == str(expected)
