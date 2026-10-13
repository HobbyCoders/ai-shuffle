"""Tests for the install-wide per-provider rate-limit circuit breaker.

Motivation is the 2026-07-22 rate-limit storm: 3,172 upstream POSTs in ~11
minutes (1,750 of them HTTP 429), all after the parent turn had ended, driven by
retry loops INSIDE the CLI processes.

This module (and this test file) was ``translator_gateway/circuit_breaker.py``
until the gateway was retired. The policy is unchanged; the RECORDING seam moved
to the fluid harness's usage-capture response hook, which is what now observes
every native-leg response.

Covered here:
  * the policy unit (trip on 429, Retry-After in BOTH forms, default cooldown,
    max-cooldown cap, expiry, reset, thread safety);
  * the fluid response-hook wiring (a 429 passing through
    ``_usage_capture_http_client`` trips the breaker with its ``Retry-After``;
    200/401/5xx do NOT);
  * the kill switch restoring the previous behavior exactly.

NO network: every upstream is an ``httpx.MockTransport`` or a stub. Nothing in
this file may ever reach a real provider.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import httpx
import pytest

from app.core import rate_limit_breaker as circuit_breaker
from app.core.config import settings


PROVIDER = "prov-cb-1"
OTHER_PROVIDER = "prov-cb-2"


@pytest.fixture(autouse=True)
def _clean_breaker(monkeypatch):
    """Every test starts with a closed breaker and the documented defaults."""
    circuit_breaker.reset()
    monkeypatch.setattr(settings, "provider_circuit_breaker_enabled", True)
    monkeypatch.setattr(settings, "provider_circuit_breaker_cooldown_seconds", 60)
    monkeypatch.setattr(settings, "provider_circuit_breaker_max_cooldown_seconds", 300)
    yield
    circuit_breaker.reset()


# ─── Retry-After parsing ─────────────────────────────────────────────────────

class TestRetryAfterParsing:
    def test_delta_seconds_form(self):
        assert circuit_breaker.parse_retry_after("120") == pytest.approx(120.0)

    def test_delta_seconds_with_surrounding_whitespace(self):
        assert circuit_breaker.parse_retry_after("  45  ") == pytest.approx(45.0)

    def test_http_date_form(self):
        now = time.time()
        when = datetime.fromtimestamp(now, tz=timezone.utc) + timedelta(seconds=90)
        header = format_datetime(when, usegmt=True)
        parsed = circuit_breaker.parse_retry_after(header, now=now)
        assert parsed is not None
        assert parsed == pytest.approx(90.0, abs=1.0)

    def test_http_date_in_the_past_is_unusable(self):
        now = time.time()
        when = datetime.fromtimestamp(now, tz=timezone.utc) - timedelta(seconds=90)
        header = format_datetime(when, usegmt=True)
        assert circuit_breaker.parse_retry_after(header, now=now) is None

    @pytest.mark.parametrize("value", [None, "", "   ", "soon", "0", "-5"])
    def test_missing_or_unusable_values(self, value):
        assert circuit_breaker.parse_retry_after(value) is None


# ─── Cooldown policy ─────────────────────────────────────────────────────────

class TestCooldownPolicy:
    def test_default_cooldown_when_header_absent(self):
        assert circuit_breaker.cooldown_seconds(None) == pytest.approx(60.0)

    def test_default_cooldown_when_header_malformed(self):
        assert circuit_breaker.cooldown_seconds("not-a-date") == pytest.approx(60.0)

    def test_retry_after_delta_seconds_honoured(self):
        assert circuit_breaker.cooldown_seconds("120") == pytest.approx(120.0)

    def test_retry_after_http_date_honoured(self):
        now = time.time()
        when = datetime.fromtimestamp(now, tz=timezone.utc) + timedelta(seconds=200)
        header = format_datetime(when, usegmt=True)
        assert circuit_breaker.cooldown_seconds(header, now=now) == pytest.approx(
            200.0, abs=1.0
        )

    def test_hostile_retry_after_is_capped(self):
        """A malformed/hostile header cannot lock a provider out indefinitely."""
        assert circuit_breaker.cooldown_seconds("999999999") == pytest.approx(300.0)

    def test_http_date_far_in_future_is_capped(self):
        now = time.time()
        when = datetime.fromtimestamp(now, tz=timezone.utc) + timedelta(days=30)
        header = format_datetime(when, usegmt=True)
        assert circuit_breaker.cooldown_seconds(header, now=now) == pytest.approx(300.0)

    def test_default_cooldown_is_also_capped(self, monkeypatch):
        monkeypatch.setattr(settings, "provider_circuit_breaker_cooldown_seconds", 5000)
        assert circuit_breaker.cooldown_seconds(None) == pytest.approx(300.0)


# ─── Trip / query / expiry ───────────────────────────────────────────────────

class TestTripAndExpiry:
    def test_first_429_trips_the_provider(self):
        trip = circuit_breaker.record_rate_limited(PROVIDER)
        assert trip is not None
        assert trip.provider_id == PROVIDER
        assert trip.status_code == 429
        assert circuit_breaker.is_tripped(PROVIDER)

    def test_trip_is_scoped_to_one_provider(self):
        circuit_breaker.record_rate_limited(PROVIDER)
        assert circuit_breaker.is_tripped(PROVIDER)
        assert not circuit_breaker.is_tripped(OTHER_PROVIDER)

    def test_trip_is_install_wide_not_session_scoped(self):
        """There is no session dimension: any caller asking about the provider
        sees the same open circuit (the 07-22 storm spanned many sessions)."""
        circuit_breaker.record_rate_limited(PROVIDER)
        assert circuit_breaker.check(PROVIDER) is not None

    def test_until_reflects_retry_after(self):
        now = 1_000_000.0
        trip = circuit_breaker.record_rate_limited(PROVIDER, "120", now=now)
        assert trip.until == pytest.approx(now + 120.0)

    def test_clears_automatically_after_expiry(self):
        now = 1_000_000.0
        circuit_breaker.record_rate_limited(PROVIDER, "30", now=now)
        assert circuit_breaker.check(PROVIDER, now=now + 29.0) is not None
        assert circuit_breaker.check(PROVIDER, now=now + 30.0) is None
        assert circuit_breaker.check(PROVIDER, now=now + 31.0) is None

    def test_expiry_prunes_state(self):
        now = 1_000_000.0
        circuit_breaker.record_rate_limited(PROVIDER, "30", now=now)
        circuit_breaker.check(PROVIDER, now=now + 100.0)  # prunes
        # A later check with an EARLIER clock must not resurrect the window.
        assert circuit_breaker.check(PROVIDER, now=now + 1.0) is None

    def test_repeated_429_extends_but_never_shortens(self):
        now = 1_000_000.0
        circuit_breaker.record_rate_limited(PROVIDER, "200", now=now)
        # A later 429 with a SHORT Retry-After must not walk back the window.
        circuit_breaker.record_rate_limited(PROVIDER, "5", now=now + 1.0)
        trip = circuit_breaker.check(PROVIDER, now=now + 10.0)
        assert trip is not None
        assert trip.until == pytest.approx(now + 200.0)
        # A later 429 with a LONGER Retry-After extends it.
        circuit_breaker.record_rate_limited(PROVIDER, "290", now=now + 20.0)
        trip = circuit_breaker.check(PROVIDER, now=now + 21.0)
        assert trip.until == pytest.approx(now + 310.0)

    def test_reset_single_provider(self):
        circuit_breaker.record_rate_limited(PROVIDER)
        circuit_breaker.record_rate_limited(OTHER_PROVIDER)
        circuit_breaker.reset(PROVIDER)
        assert not circuit_breaker.is_tripped(PROVIDER)
        assert circuit_breaker.is_tripped(OTHER_PROVIDER)

    def test_reset_all(self):
        circuit_breaker.record_rate_limited(PROVIDER)
        circuit_breaker.record_rate_limited(OTHER_PROVIDER)
        circuit_breaker.reset()
        assert not circuit_breaker.is_tripped(PROVIDER)
        assert not circuit_breaker.is_tripped(OTHER_PROVIDER)

    def test_empty_provider_id_is_a_noop(self):
        assert circuit_breaker.record_rate_limited(None) is None
        assert circuit_breaker.record_rate_limited("") is None
        assert circuit_breaker.check(None) is None


# ─── Kill switch ─────────────────────────────────────────────────────────────

class TestDisableFlag:
    def test_disabled_records_nothing(self, monkeypatch):
        monkeypatch.setattr(settings, "provider_circuit_breaker_enabled", False)
        assert circuit_breaker.record_rate_limited(PROVIDER, "120") is None
        assert circuit_breaker.check(PROVIDER) is None
        assert not circuit_breaker.is_tripped(PROVIDER)

    def test_disabled_never_refuses_even_with_prior_state(self, monkeypatch):
        circuit_breaker.record_rate_limited(PROVIDER, "300")
        assert circuit_breaker.is_tripped(PROVIDER)
        monkeypatch.setattr(settings, "provider_circuit_breaker_enabled", False)
        assert circuit_breaker.check(PROVIDER) is None
        assert not circuit_breaker.is_tripped(PROVIDER)


# ─── Concurrency ─────────────────────────────────────────────────────────────

class TestConcurrency:
    def test_concurrent_record_and_check_is_safe(self):
        """32 threads hammering record/check must not corrupt state or raise.

        This mirrors the real failure mode: ~122 background tasks issuing
        overlapping requests, all of which land on this one shared object.
        """
        errors: list[BaseException] = []
        barrier = threading.Barrier(32)

        def worker(index: int) -> None:
            try:
                barrier.wait(timeout=10)
                for _ in range(50):
                    circuit_breaker.record_rate_limited(
                        f"prov-{index % 4}", str(100 + index)
                    )
                    circuit_breaker.check(f"prov-{index % 4}")
                    circuit_breaker.is_tripped(f"prov-{(index + 1) % 4}")
            except BaseException as exc:  # noqa: BLE001 — surfaced below
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(32)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert errors == []
        for i in range(4):
            assert circuit_breaker.is_tripped(f"prov-{i}")

    def test_concurrent_reset_does_not_deadlock(self):
        errors: list[BaseException] = []

        def writer() -> None:
            try:
                for _ in range(200):
                    circuit_breaker.record_rate_limited(PROVIDER, "60")
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        def resetter() -> None:
            try:
                for _ in range(200):
                    circuit_breaker.reset(PROVIDER)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=writer) for _ in range(4)]
        threads += [threading.Thread(target=resetter) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert errors == []
        assert all(not t.is_alive() for t in threads)



# ─── fluid response-hook wiring (D2 port) ─────────────────────────────────────
# The retired gateway trapped 429s in ``upstream._note_rate_limit``. Its
# replacement is the fluid usage-capture response hook, which sees every native
# leg response and already carries the bound ``provider_id``. These tests drive
# the REAL hook through ``httpx.MockTransport`` — no network, no provider row.

from app.core.provider.harness_adapter import (  # noqa: E402  (after the fixtures above)
    _usage_capture_http_client,
)


def _mock_response_client(response_factory, *, use_codex_backend: bool = False):
    """The real capture client, with its transport swapped for a mock.

    ``_usage_capture_http_client`` builds the client (and installs the hook we
    are testing); replacing ``_transport`` afterwards keeps the event hook
    exactly as production wires it while guaranteeing no socket is opened.
    """
    client = _usage_capture_http_client(PROVIDER, use_codex_backend)
    client._transport = httpx.MockTransport(response_factory)
    return client


class TestFluidResponseHookTripsOnlyOn429:
    """D2: a 429 on a native fluid leg trips the breaker via the response hook."""

    async def test_429_through_the_hook_trips_the_breaker(self):
        def handler(request):
            return httpx.Response(429, headers={"retry-after": "120"}, content=b"slow down")

        async with _mock_response_client(handler) as client:
            resp = await client.post("https://api.openai.com/v1/responses", json={})
        assert resp.status_code == 429

        trip = circuit_breaker.check(PROVIDER)
        assert trip is not None
        assert trip.provider_id == PROVIDER
        assert trip.status_code == 429
        assert trip.until - trip.tripped_at == pytest.approx(120.0)

    async def test_429_without_retry_after_uses_the_default_cooldown(self):
        def handler(request):
            return httpx.Response(429, content=b"slow down")

        async with _mock_response_client(handler) as client:
            await client.post("https://api.openai.com/v1/responses", json={})

        trip = circuit_breaker.check(PROVIDER)
        assert trip is not None
        assert trip.until - trip.tripped_at == pytest.approx(60.0)

    async def test_429_on_the_codex_backend_also_trips(self):
        def handler(request):
            return httpx.Response(429, headers={"retry-after": "45"}, content=b"slow down")

        async with _mock_response_client(handler, use_codex_backend=True) as client:
            await client.post("https://chatgpt.com/backend-api/codex/responses", json={})

        trip = circuit_breaker.check(PROVIDER)
        assert trip is not None
        assert trip.until - trip.tripped_at == pytest.approx(45.0)

    @pytest.mark.parametrize("status", [200, 400, 401, 500, 502, 529])
    async def test_non_429_statuses_do_not_trip(self, status):
        def handler(request):
            return httpx.Response(status, content=b"{}")

        async with _mock_response_client(handler) as client:
            await client.post("https://api.openai.com/v1/responses", json={})

        assert not circuit_breaker.is_tripped(PROVIDER)

    async def test_kill_switch_disables_the_hook_recording(self, monkeypatch):
        monkeypatch.setattr(settings, "provider_circuit_breaker_enabled", False)

        def handler(request):
            return httpx.Response(429, headers={"retry-after": "120"}, content=b"nope")

        async with _mock_response_client(handler) as client:
            await client.post("https://api.openai.com/v1/responses", json={})

        assert circuit_breaker.check(PROVIDER) is None

    async def test_a_broken_breaker_never_breaks_the_response(self, monkeypatch):
        """Bookkeeping is a side channel: an exploding recorder is swallowed."""
        def boom(*_args, **_kwargs):
            raise RuntimeError("breaker exploded")

        monkeypatch.setattr(circuit_breaker, "record_rate_limited", boom)

        def handler(request):
            return httpx.Response(429, headers={"retry-after": "120"}, content=b"slow down")

        async with _mock_response_client(handler) as client:
            resp = await client.post("https://api.openai.com/v1/responses", json={})
        assert resp.status_code == 429
