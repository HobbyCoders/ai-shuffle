"""A provider poll that fails is logged at ERROR, not warning or debug.

Only ERROR+ reaches the durable ``error_log`` table, which is the only thing
``GetAppErrors`` can read. The credit pollers logged HTTP failures at warning
and 401/403 at debug, so an agent asking why a balance was missing got
``{"errors": [], "count": 0}`` and gave up. A poller repeats every five
minutes, so the escalation is paired with time-based de-duplication.
"""

from __future__ import annotations

import logging

import pytest

from app.core import provider_usage as pu


@pytest.fixture(autouse=True)
def _clean_state():
    pu._poll_failure_state.clear()
    yield
    pu._poll_failure_state.clear()


def _levels(caplog):
    return [r.levelno for r in caplog.records if r.name == pu.__name__]


def test_first_failure_is_an_error(caplog):
    with caplog.at_level(logging.DEBUG, logger=pu.__name__):
        pu._log_poll_failure("openai:p1", "poll for %s failed: HTTP %s", "p1", 403)

    assert _levels(caplog) == [logging.ERROR]
    assert "poll for p1 failed: HTTP 403" in caplog.text


def test_the_same_failure_repeating_drops_to_debug(caplog):
    with caplog.at_level(logging.DEBUG, logger=pu.__name__):
        for _ in range(4):
            pu._log_poll_failure("openai:p1", "poll for %s failed: HTTP %s", "p1", 403)

    assert _levels(caplog) == [logging.ERROR] + [logging.DEBUG] * 3


def test_a_changed_failure_is_re_asserted_immediately(caplog):
    with caplog.at_level(logging.DEBUG, logger=pu.__name__):
        pu._log_poll_failure("openai:p1", "poll for %s failed: HTTP %s", "p1", 403)
        pu._log_poll_failure("openai:p1", "poll for %s failed: HTTP %s", "p1", 500)

    assert _levels(caplog) == [logging.ERROR, logging.ERROR]


def test_a_long_standing_failure_is_re_asserted_after_the_window(caplog, monkeypatch):
    """A permanently-broken key must not go silent forever — an operator
    looking at the error log today should still see yesterday's outage."""
    clock = {"t": 1000.0}
    monkeypatch.setattr(pu.time, "monotonic", lambda: clock["t"])

    with caplog.at_level(logging.DEBUG, logger=pu.__name__):
        pu._log_poll_failure("openai:p1", "still broken")
        clock["t"] += pu._POLL_FAILURE_REASSERT_SECONDS / 2
        pu._log_poll_failure("openai:p1", "still broken")
        clock["t"] += pu._POLL_FAILURE_REASSERT_SECONDS
        pu._log_poll_failure("openai:p1", "still broken")

    assert _levels(caplog) == [logging.ERROR, logging.DEBUG, logging.ERROR]


def test_distinct_providers_do_not_suppress_each_other(caplog):
    with caplog.at_level(logging.DEBUG, logger=pu.__name__):
        pu._log_poll_failure("openai:p1", "poll failed")
        pu._log_poll_failure("openai:p2", "poll failed")

    assert _levels(caplog) == [logging.ERROR, logging.ERROR]


def test_recovery_makes_the_next_failure_loud_again(caplog):
    with caplog.at_level(logging.DEBUG, logger=pu.__name__):
        pu._log_poll_failure("openai:p1", "poll failed")
        pu._log_poll_failure("openai:p1", "poll failed")
        pu._clear_poll_failure("openai:p1")
        pu._log_poll_failure("openai:p1", "poll failed")

    assert _levels(caplog) == [logging.ERROR, logging.DEBUG, logging.ERROR]


def test_a_bad_format_string_still_logs(caplog):
    """Logging is never allowed to be the thing that breaks."""
    with caplog.at_level(logging.DEBUG, logger=pu.__name__):
        pu._log_poll_failure("k", "not a %d placeholder", "abc")

    assert _levels(caplog) == [logging.ERROR]
    assert "not a %d placeholder" in caplog.text


# ─── the call sites ──────────────────────────────────────────────────────────


class _Resp:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code

    def json(self):
        return {}


async def test_unconfigured_openai_does_not_log_repeated_billing_failures(caplog):
    monitor = pu.ProviderUsageMonitor()
    with caplog.at_level(logging.DEBUG, logger=pu.__name__):
        await monitor._poll_openai_provider({"id": "not-configured"})
    assert not [r for r in caplog.records if r.levelno == logging.ERROR]
