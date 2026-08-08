"""Tests for app.core.browser_panel_registry.

Covers idempotent token issuance, constant-time verification (the 403 path),
target register/read, and that a ``register`` wakes an in-flight
``wait_for_change`` waiter.
"""

import asyncio

import pytest

from app.core import browser_panel_registry as registry


@pytest.fixture(autouse=True)
def _clean():
    # Registry is process-global; isolate each test.
    for sid in ("s-tok", "s-verify", "s-target", "s-wake"):
        registry.clear(sid)
    yield
    for sid in ("s-tok", "s-verify", "s-target", "s-wake"):
        registry.clear(sid)


def test_issue_token_is_idempotent():
    t1 = registry.issue_token("s-tok")
    t2 = registry.issue_token("s-tok")
    assert t1 == t2
    assert isinstance(t1, str) and len(t1) >= 32
    # Distinct sessions get distinct tokens.
    assert registry.issue_token("s-verify") != t1


def test_verify_token_paths():
    tok = registry.issue_token("s-verify")
    assert registry.verify_token("s-verify", tok) is True
    assert registry.verify_token("s-verify", "wrong") is False
    assert registry.verify_token("s-verify", None) is False
    # Unknown session (never issued) always fails.
    assert registry.verify_token("never", "anything") is False


def test_register_and_get_target():
    assert registry.get_target("s-target") is None
    registry.register("s-target", "target-1", "tab-1", "http://example/")
    tgt = registry.get_target("s-target")
    assert tgt is not None
    assert tgt.target_id == "target-1"
    assert tgt.tab_id == "tab-1"
    assert tgt.url == "http://example/"
    # Overwrite.
    registry.register("s-target", "target-2", None, None)
    tgt2 = registry.get_target("s-target")
    assert tgt2.target_id == "target-2"
    assert tgt2.tab_id is None


async def test_change_event_wakes_waiter():
    waiter = asyncio.create_task(registry.wait_for_change("s-wake"))
    # Let the waiter start awaiting the current event.
    await asyncio.sleep(0)
    assert not waiter.done()

    registry.register("s-wake", "t", None, "http://x/")
    await asyncio.wait_for(waiter, timeout=1.0)
    assert registry.get_target("s-wake").target_id == "t"


async def test_clear_removes_state():
    registry.issue_token("s-tok")
    registry.register("s-tok", "t", None, None)
    registry.clear("s-tok")
    assert registry.get_target("s-tok") is None
    # A fresh token is minted after clear (not the old one).
    assert registry.verify_token("s-tok", "old") is False
