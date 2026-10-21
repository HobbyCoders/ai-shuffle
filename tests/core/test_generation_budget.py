"""Durable budget and confirmation-producing paths, on the isolated test DB."""
import pytest

from app.core import assistant_limits, generation_budget
from app.core.generation_confirmations import Confirmations


def test_generation_reservations_use_existing_cap_and_count_in_status():
    assert assistant_limits.status()["spent_today_usd"] == 0
    assert generation_budget.reserve("19.95")
    assert assistant_limits.status()["spent_today_usd"] == 19.95
    assert not generation_budget.reserve("0.06")
    assert generation_budget.reserve("0.05")
    assert assistant_limits.status()["limit_reached"]
    assert not generation_budget.reserve("0.01")


@pytest.mark.parametrize("amount", ["NaN", "Infinity", "-1"])
def test_invalid_reservation_cannot_change_budget(amount):
    with pytest.raises(ValueError):
        generation_budget.reserve(amount)
    assert assistant_limits.status()["spent_today_usd"] == 0


async def test_confirmation_survives_tool_server_rebuild_and_is_one_use():
    first = Confirmations()
    token = await first.issue("fingerprint")
    rebuilt = Confirmations()
    assert await rebuilt.consume(token, "fingerprint")
    assert not await rebuilt.consume(token, "fingerprint")


async def test_confirmation_expires_and_mismatches_are_consumed(monkeypatch):
    import app.core.generation_confirmations as module
    clock = [1000.0]
    monkeypatch.setattr(module.time, "time", lambda: clock[0])
    receipts = Confirmations()
    token = await receipts.issue("first")
    assert not await receipts.consume(token, "second")
    assert not await receipts.consume(token, "first")
    token = await receipts.issue("first")
    clock[0] += module.TTL + 1
    assert not await receipts.consume(token, "first")


def test_durable_remote_submission_claim_is_not_replayed():
    from app.network.generation import claim
    command = "00000000-0000-0000-0000-000000000001"
    assert claim(command)
    assert not claim(command)


def test_budget_does_not_round_off_small_reservations():
    assert generation_budget.reserve("19.99996")
    assert generation_budget.reserve("0.00004")
    assert not generation_budget.reserve("0.00001")


def test_reported_overrun_is_counted_even_if_it_crosses_the_cap():
    assert generation_budget.reserve("19.99")
    generation_budget.record_overrun("0.04")
    assert assistant_limits.status()["spent_today_usd"] == 20.03
    assert assistant_limits.status()["limit_reached"]
    assert not generation_budget.reserve("0.01")
    with pytest.raises(ValueError):
        generation_budget.record_overrun("-1")
