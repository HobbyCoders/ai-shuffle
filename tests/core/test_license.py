"""Account plan metadata never gates local use."""
from concurrent.futures import ThreadPoolExecutor
import threading
from unittest.mock import Mock

import pytest

from app.core import account_hub, license as licensing
from app.db import mobile_hub

BINDING = {"active": 1, "hub_url": "https://hobbycoders.com", "server_id": "server",
           "linked_by": "account", "encrypted_secret": "sealed", "instance_id": "local"}


@pytest.fixture(autouse=True)
def cache(monkeypatch):
    monkeypatch.setattr(licensing, "_entry", None)
    monkeypatch.setattr(licensing, "_generation", 0)
    monkeypatch.setattr(mobile_hub, "get_binding", lambda: dict(BINDING))


def plan(**overrides):
    return {"plan": "paid", "status": "active", "remote_worker_limit": 100,
            "seats": 100, "used": 7, "renews_at": 1800000000000, **overrides}


def test_not_linked_never_calls_hub(monkeypatch):
    monkeypatch.setattr(mobile_hub, "get_binding", lambda: None)
    call = Mock(side_effect=AssertionError("must not call"))
    monkeypatch.setattr(account_hub, "call", call)
    result = licensing.status()
    assert result["state"] == "not_linked" and result["allowed"] is True
    assert result["source"] == "account" and result["remote_worker_limit"] == 0
    assert result["tier"] is None
    assert result["key_hint"] is result["install_id"] is None
    assert result["lease_state"] == "none" and result["server_configured"] is False
    call.assert_not_called()


@pytest.mark.parametrize("hub_status", [403, 404])
def test_free_before_hub_deployment_or_activation(monkeypatch, hub_status):
    monkeypatch.setattr(account_hub, "call", Mock(side_effect=account_hub.HubResponseError("unavailable", hub_status)))
    result = licensing.status()
    assert result["state"] == "free" and result["remote_worker_limit"] == 3
    assert result["message"] == "Free account: 3 remote workers."
    assert result["tier"] is None


@pytest.mark.parametrize("account_plan", ["free", "paid"])
@pytest.mark.parametrize("reported, shown", [(2, 2), (3, 3), (None, 3)])
def test_status_none_is_free(monkeypatch, account_plan, reported, shown):
    # An older hub gives free accounts 2; the app shows what the hub reports.
    monkeypatch.setattr(account_hub, "call", Mock(return_value=plan(plan=account_plan, status="none", remote_worker_limit=reported)))
    result = licensing.status()
    assert result["state"] == result["plan"] == "free"
    assert result["remote_worker_limit"] == result["seats"] == shown


@pytest.mark.parametrize("stale", [4, 10, 100])
def test_lapsed_paid_never_shows_stale_paid_limit(monkeypatch, stale):
    monkeypatch.setattr(account_hub, "call", Mock(return_value=plan(status="none", remote_worker_limit=stale, seats=stale)))
    result = licensing.status()
    assert result["state"] == "free" and result["remote_worker_limit"] == result["seats"] == 3


@pytest.mark.parametrize("state", ["active", "trialing", "grace"])
def test_paid_with_one_hundred_seats(monkeypatch, state):
    call = Mock(return_value=plan(status=state))
    monkeypatch.setattr(account_hub, "call", call)
    result = licensing.status()
    assert result["state"] == result["plan"] == "paid"
    assert result["remote_worker_limit"] == result["seats"] == 100
    assert result["seats_used"] == 7 and result["allowed"] is True
    assert result["expires_at"] == "2027-01-15T08:00:00+00:00"
    assert result["renews_at"] == 1800000000000
    assert result["grace"] is (state == "grace")
    assert result["message"] == "Paid plan: 100 remote workers."
    assert result["tier"] is None
    call.assert_called_once_with(BINDING, "GET", "plan", timeout=licensing.PLAN_TIMEOUT)


def test_paid_defaults_to_ten(monkeypatch):
    monkeypatch.setattr(account_hub, "call", Mock(return_value=plan(remote_worker_limit=None, seats=None, renews_at=None)))
    result = licensing.status()
    assert result["remote_worker_limit"] == 10 and result["seats"] is None
    assert result["expires_at"] is None


@pytest.mark.parametrize("hub_status", [429, 503])
def test_cache_ttl_and_error_keeps_last_good(monkeypatch, hub_status):
    now = [1000.0]
    monkeypatch.setattr(licensing.time, "monotonic", lambda: now[0])
    call = Mock(side_effect=[plan(), account_hub.HubResponseError("offline", hub_status), plan(plan="free", remote_worker_limit=2)])
    monkeypatch.setattr(account_hub, "call", call)
    assert licensing.status()["state"] == "paid"
    now[0] += licensing.PLAN_TTL - 1
    assert licensing.status()["seats"] == 100 and call.call_count == 1
    now[0] += 1
    assert licensing.status()["seats"] == 100 and call.call_count == 2
    assert licensing.status()["seats"] == 100 and call.call_count == 2
    now[0] += licensing.RETRY_INTERVAL
    assert licensing.status()["state"] == "free" and call.call_count == 3


@pytest.mark.parametrize("tier, limit, message", [
    ("pro", 10, "Pro plan: 10 remote workers."),
    ("ultimate", 30, "Ultimate plan: 30 remote workers."),
    # The owner's complimentary licence: a custom tier keeps the paid wording.
    ("custom", 100, "Paid plan: 100 remote workers."),
])
def test_tier_names_the_plan(monkeypatch, tier, limit, message):
    monkeypatch.setattr(account_hub, "call", Mock(return_value=plan(tier=tier, remote_worker_limit=limit, seats=limit)))
    result = licensing.status()
    assert result["state"] == result["plan"] == "paid" and result["tier"] == tier
    assert result["remote_worker_limit"] == limit and result["message"] == message


def test_free_tier_passes_through(monkeypatch):
    monkeypatch.setattr(account_hub, "call", Mock(return_value=plan(plan="free", status="none", tier="free", remote_worker_limit=3, seats=3)))
    result = licensing.status()
    assert result["state"] == "free" and result["tier"] == "free"
    assert result["message"] == "Free account: 3 remote workers."


@pytest.mark.parametrize("tier", ["enterprise", "", 7, None, ["pro"], "PRO"])
def test_unknown_tier_falls_back_to_paid_wording(monkeypatch, tier):
    monkeypatch.setattr(account_hub, "call", Mock(return_value=plan(tier=tier, remote_worker_limit=12, seats=12)))
    result = licensing.status()
    assert result["state"] == "paid" and result["tier"] is None
    assert result["remote_worker_limit"] == 12 and result["message"] == "Paid plan: 12 remote workers."


def test_tier_never_sets_the_limit(monkeypatch):
    # The hub's count wins even when it disagrees with the tier's usual size.
    monkeypatch.setattr(account_hub, "call", Mock(return_value=plan(tier="pro", remote_worker_limit=25, seats=25)))
    assert licensing.status()["remote_worker_limit"] == 25
    licensing.invalidate_plan_cache()
    monkeypatch.setattr(account_hub, "call", Mock(return_value=plan(tier="ultimate", remote_worker_limit=None, seats=None)))
    assert licensing.status()["remote_worker_limit"] == licensing.PAID_WORKERS


@pytest.mark.parametrize("tier", ["pro", "ultimate", "custom"])
def test_lapsed_paid_tier_reads_free(monkeypatch, tier):
    monkeypatch.setattr(account_hub, "call", Mock(return_value=plan(status="none", tier=tier, remote_worker_limit=30)))
    result = licensing.status()
    assert result["state"] == "free" and result["tier"] is None
    assert result["remote_worker_limit"] == result["seats"] == 3
    assert result["message"] == "Free account: 3 remote workers."


def test_paid_plan_with_free_tier_keeps_paid_wording(monkeypatch):
    monkeypatch.setattr(account_hub, "call", Mock(return_value=plan(tier="free", remote_worker_limit=10, seats=10)))
    result = licensing.status()
    assert result["state"] == "paid" and result["tier"] is None
    assert result["message"] == "Paid plan: 10 remote workers."


def test_downgrade_usage_can_exceed_limit_and_seat_cap(monkeypatch):
    monkeypatch.setattr(account_hub, "call", Mock(return_value=plan(plan="free", remote_worker_limit=2, used=101)))
    result = licensing.status()
    assert result["state"] == "free" and result["remote_worker_limit"] == 2
    assert result["seats_used"] == 101


def test_initial_hub_error_is_free_and_allowed(monkeypatch):
    monkeypatch.setattr(account_hub, "call", Mock(side_effect=TimeoutError()))
    assert licensing.status()["remote_worker_limit"] == 3
    assert licensing.status()["allowed"] is True


def test_relink_does_not_reuse_previous_accounts_plan(monkeypatch):
    call = Mock(side_effect=[plan(), account_hub.HubResponseError("offline", 503)])
    monkeypatch.setattr(account_hub, "call", call)
    assert licensing.status()["state"] == "paid"
    monkeypatch.setattr(mobile_hub, "get_binding", lambda: dict(BINDING, linked_by="other"))
    assert licensing.status()["state"] == "free"
    assert call.call_count == 2


def test_invalidation_refetches_before_ttl(monkeypatch):
    call = Mock(side_effect=[plan(), plan(plan="free", remote_worker_limit=2)])
    monkeypatch.setattr(account_hub, "call", call)
    licensing.status()
    licensing.invalidate_plan_cache()
    assert licensing.status()["state"] == "free" and call.call_count == 2


def test_concurrent_read_serves_last_good_without_waiting(monkeypatch):
    started, finish = threading.Event(), threading.Event()
    def fetch(*args, **kwargs):
        started.set()
        assert finish.wait(5)
        return plan()
    monkeypatch.setattr(account_hub, "call", fetch)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(licensing.status)
        assert started.wait(5)
        try:
            assert licensing.status()["state"] == "free"
        finally:
            finish.set()
        assert future.result()["state"] == "paid"


def test_relink_while_fetching_does_not_expose_old_plan(monkeypatch):
    binding = [dict(BINDING)]
    monkeypatch.setattr(mobile_hub, "get_binding", lambda: binding[0])
    def fetch(*args, **kwargs):
        binding[0] = dict(BINDING, linked_by="other")
        return plan()
    monkeypatch.setattr(account_hub, "call", fetch)
    assert licensing.status()["state"] == "free"


def test_removed_key_and_lease_machinery_stays_absent():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    for name in ("activate", "release_seat", "deactivate", "refresh_loop", "verify_lease", "lease_header", "capture_env_key"):
        assert not hasattr(licensing, name)
    for name in ("app/core/config.py", "app/main.py", "app/core/startup.py", "app/core/account_hub.py",
                 "docker/application_updates.py", "docker/container_runtime.py",
                 "desktop/src/main/source-auth.ts", "desktop/src/main/source-updates.ts"):
        text = (root / name).read_text()
        for obsolete in ("license_server_url", "license_beta", "X-Shuffle-Lease", "LICENSE_KEY", "license-key.json"):
            assert obsolete not in text, (name, obsolete)


def test_old_saved_license_state_is_ignored(mock_db, monkeypatch):
    from app.db import database
    from app.core.config import settings
    database.set_system_setting("license_state:" + settings.effective_instance_id, '{"lease":{"seats":100}}')
    monkeypatch.setattr(account_hub, "call", Mock(return_value=plan(plan="free", remote_worker_limit=2)))
    assert licensing.status()["state"] == "free"
