"""What a terminal background-task chip SAYS, and what it looks like.

Two audited defects, both in the same three lines of
``chat_ws_handler._surface_terminal_notification``:

* ``content=summary or ""`` persisted BLANK ``role=system`` rows — fifteen in
  one session — because shell-kind tasks never populate ``summary``. The user
  saw "a task errored" with no reason while the model's own steer for the same
  event carried the full text.
* the chip carried the raw registry status, so ``killed`` rendered a red cross
  for work an agent had deliberately stopped seconds earlier (its own stale CI
  watcher, a dev server it no longer needed).
"""

from __future__ import annotations

import pytest

from app.api.chat_ws_handler import _terminal_chip_text
from app.core.task_registry import (
    AGENT_STOP_REASON,
    CANCELLED_CHIP_STATUS,
    SERVER_SHUTDOWN_REASON,
    USER_STOP_REASON,
    chip_status,
)


# ═══ chip text — never blank ═════════════════════════════════════════════════


def test_a_real_summary_is_used_verbatim():
    task = {"id": "t1", "kind": "agent", "summary": "Ran 382 tests, 3 failed"}
    assert _terminal_chip_text(task, "error") == "Ran 382 tests, 3 failed"


def test_a_summaryless_shell_falls_back_to_its_command():
    """The audited empty bubble: a Monitor shell settles with no summary."""
    task = {"id": "t1", "kind": "shell", "subject": "npm run dev", "summary": None}
    text = _terminal_chip_text(task, "error")
    assert "npm run dev" in text
    assert "failed" in text.lower()


def test_a_summaryless_agent_task_still_says_something():
    task = {"id": "t1", "kind": "agent", "subject": None, "summary": ""}
    assert _terminal_chip_text(task, CANCELLED_CHIP_STATUS).strip()


def test_a_very_long_command_is_trimmed_not_dropped():
    task = {"id": "t1", "kind": "shell", "subject": "x" * 400}
    text = _terminal_chip_text(task, "killed")
    assert len(text) < 220
    assert text.endswith("...")


@pytest.mark.parametrize("status", ["error", "killed", "cancelled", "completed"])
def test_the_chip_is_never_empty_whatever_the_status(status):
    assert _terminal_chip_text({"id": "t1"}, status).strip()


# ═══ chip status — a deliberate stop is not a failure ════════════════════════


@pytest.mark.parametrize(
    "reason",
    [USER_STOP_REASON, AGENT_STOP_REASON, "user_stopped_all", "parent_turn_ended"],
)
def test_a_deliberate_stop_reads_as_cancelled(reason):
    assert chip_status("killed", reason) == CANCELLED_CHIP_STATUS


@pytest.mark.parametrize(
    "reason",
    [None, "", "monitor_timeout", "provider_rate_limited", SERVER_SHUTDOWN_REASON],
)
def test_everything_else_keeps_its_killed_status(reason):
    """A reason-less ``killed`` is the ghost reap and the force-eviction sweep.

    Those really are failures from where the user sits — the work cannot
    finish — so they keep the failure vocabulary.
    """
    assert chip_status("killed", reason) == "killed"


@pytest.mark.parametrize("status", ["error", "completed", "running"])
def test_only_killed_is_ever_rewritten(status):
    assert chip_status(status, USER_STOP_REASON) == status
