"""A Claude Code sign-in change reaches every open client.

The model picker lists Claude models only while ``/api/models`` reports a
Claude sign-in, and clients re-read ``/api/models`` when a ``settings``
resource change arrives. ``claude_cli_status.get_status`` publishes that change
when a probe finds the sign-in or the plan different from the previous one.
"""

import pytest

from app.core import claude_cli_status
from app.core.claude_cli_status import ClaudeCliStatus


@pytest.fixture
def probes(monkeypatch):
    """Queue probe results and record the admin ``settings`` events."""
    queue: list[ClaudeCliStatus] = []
    events: list[tuple] = []

    monkeypatch.setattr(claude_cli_status, "probe", lambda resolver=None: queue.pop(0))
    monkeypatch.setattr(claude_cli_status, "_last_account", None)
    claude_cli_status.invalidate()

    from app.core.global_events import global_events

    def record(*resources, user_type="admin", user_id=None):
        events.append((resources, user_type))

    monkeypatch.setattr(global_events, "resources_changed", record)

    def settings_events():
        return [e for e in events if e == (("settings",), "admin")]

    yield queue, settings_events
    claude_cli_status.invalidate()


def _status(signed_in, plan=None):
    return ClaudeCliStatus(installed=True, signed_in=signed_in, plan=plan)


def test_a_sign_in_publishes_one_settings_event(probes):
    queue, settings_events = probes
    queue.extend([_status(False), _status(True, "max")])

    claude_cli_status.get_status(force=True)
    assert settings_events() == []  # the first probe of the process is the baseline

    claude_cli_status.get_status(force=True)
    assert len(settings_events()) == 1


def test_an_unchanged_answer_publishes_nothing(probes):
    queue, settings_events = probes
    queue.extend([_status(True, "max"), _status(True, "max"), _status(True, "max")])

    for _ in range(3):
        claude_cli_status.get_status(force=True)

    assert settings_events() == []


def test_a_plan_change_publishes(probes):
    queue, settings_events = probes
    queue.extend([_status(True, "pro"), _status(True, "max")])

    claude_cli_status.get_status(force=True)
    claude_cli_status.get_status(force=True)

    assert len(settings_events()) == 1


def test_the_login_terminal_invalidate_does_not_hide_the_change(probes):
    """The login terminal calls ``invalidate()`` when it exits; the next probe
    must still be compared with the answer from before the login."""
    queue, settings_events = probes
    queue.extend([_status(False), _status(True, "max")])

    claude_cli_status.get_status(force=True)
    claude_cli_status.invalidate()
    claude_cli_status.get_status()

    assert len(settings_events()) == 1


def test_an_unreadable_probe_is_not_a_change(probes):
    queue, settings_events = probes
    unreadable = ClaudeCliStatus(installed=True, signed_in=None, error="timed out")
    queue.extend([_status(True, "max"), unreadable, _status(True, "max")])

    for _ in range(3):
        claude_cli_status.get_status(force=True)

    assert settings_events() == []


def test_a_cached_answer_publishes_nothing(probes):
    queue, settings_events = probes
    queue.extend([_status(False), _status(True, "max")])

    claude_cli_status.get_status(force=True)
    claude_cli_status.get_status()  # still fresh: served from the cache

    assert len(queue) == 1
    assert settings_events() == []
