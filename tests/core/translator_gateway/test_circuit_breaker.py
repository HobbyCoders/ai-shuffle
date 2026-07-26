"""Tests for the install-wide per-provider circuit breaker (P3 rate-limit guard).

Motivation is the 2026-07-22 rate-limit storm: 3,172 upstream POSTs in ~11
minutes (1,750 of them HTTP 429), all after the parent turn had ended, driven by
retry loops INSIDE the CLI processes. The gateway is the only chokepoint that
sees that traffic, so it must refuse.

Covered here:
  * the policy unit (trip on 429, Retry-After in BOTH forms, default cooldown,
    max-cooldown cap, expiry, reset, thread safety);
  * the upstream wiring (429 trips; 401 and 5xx do NOT; the Codex 401
    token-rotation retry is untouched);
  * the router pre-flight (refusal for both the buffered and the streaming path,
    with NO upstream call and NO provider load);
  * the kill switch restoring the previous behavior exactly.

NO network: every upstream is an ``httpx.MockTransport`` or a stub. Nothing in
this file may ever reach a real provider.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from typing import Any, AsyncIterator, Dict, Tuple

import httpx
import pytest

from app.core import encryption
from app.core.config import settings
from app.core.translator_gateway import circuit_breaker, routing, upstream
from app.core.translator_gateway.service import build_gateway_app
from app.db import database as db


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
        assert circuit_breaker.get_breaker().check(PROVIDER) is not None

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

    def test_refusal_message_names_provider_and_clear_time(self):
        now = 1_000_000.0
        trip = circuit_breaker.record_rate_limited(PROVIDER, "120", now=now)
        msg = circuit_breaker.refusal_message(trip, now=now)
        assert PROVIDER in msg
        assert "rate limited" in msg.lower()
        assert "120s" in msg
        assert trip.until_iso() in msg
        assert "NOT sent upstream" in msg


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


# ─── upstream.py wiring (httpx.MockTransport — never a real provider) ────────

def _seed_openai_provider(provider_id: str = PROVIDER) -> None:
    db.create_model_provider(
        provider_id=provider_id,
        name="OpenAI",
        provider_type="openai",
        base_url="https://api.openai.com/v1",
        enabled=True,
        auth_token="sk-test-key",
    )


def _seed_codex_provider(provider_id: str = "prov-cb-codex") -> None:
    db.create_model_provider(
        provider_id=provider_id,
        name="Codex",
        provider_type="custom",
        base_url="https://chatgpt.com/backend-api/codex",
        enabled=True,
    )
    db.set_codex_token_bundle(
        provider_id,
        access_token=encryption.encrypt_value("access-tok"),
        refresh_token=encryption.encrypt_value("refresh-tok"),
        account_id="acct-1",
        id_token_expires_at=None,
        last_refresh=None,
        needs_reauth=False,
    )


async def _drain(**kwargs) -> None:
    async for _ in upstream.stream_responses_events(**kwargs):
        pass


class TestUpstreamTripsOnlyOn429:
    async def test_429_trips_the_breaker(self, mock_db):
        _seed_openai_provider()

        def handler(request):
            return httpx.Response(429, headers={"retry-after": "120"}, content=b"slow down")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(upstream.UpstreamError) as excinfo:
                await _drain(
                    provider_id=PROVIDER,
                    mode="openai",
                    responses_body={"model": "gpt-5"},
                    client=client,
                )
        assert excinfo.value.status_code == 429
        trip = circuit_breaker.check(PROVIDER)
        assert trip is not None
        assert trip.remaining_seconds() == pytest.approx(120.0, abs=2.0)

    async def test_429_without_retry_after_uses_default(self, mock_db):
        _seed_openai_provider()

        def handler(request):
            return httpx.Response(429, content=b"slow down")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(upstream.UpstreamError):
                await _drain(
                    provider_id=PROVIDER,
                    mode="openai",
                    responses_body={"model": "gpt-5"},
                    client=client,
                )
        trip = circuit_breaker.check(PROVIDER)
        assert trip is not None
        assert trip.remaining_seconds() == pytest.approx(60.0, abs=2.0)

    async def test_401_does_not_trip(self, mock_db):
        _seed_openai_provider()

        def handler(request):
            return httpx.Response(401, content=b"bad key")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(upstream.UpstreamError) as excinfo:
                await _drain(
                    provider_id=PROVIDER,
                    mode="openai",
                    responses_body={"model": "gpt-5"},
                    client=client,
                )
        assert excinfo.value.status_code == 401
        assert not circuit_breaker.is_tripped(PROVIDER)

    @pytest.mark.parametrize("status", [500, 502, 503, 529])
    async def test_5xx_does_not_trip(self, mock_db, status):
        _seed_openai_provider()

        def handler(request):
            return httpx.Response(status, content=b"upstream boom")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(upstream.UpstreamError):
                await _drain(
                    provider_id=PROVIDER,
                    mode="openai",
                    responses_body={"model": "gpt-5"},
                    client=client,
                )
        assert not circuit_breaker.is_tripped(PROVIDER)

    async def test_400_does_not_trip(self, mock_db):
        _seed_openai_provider()

        def handler(request):
            return httpx.Response(400, content=b"bad request")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(upstream.UpstreamError):
                await _drain(
                    provider_id=PROVIDER,
                    mode="openai",
                    responses_body={"model": "gpt-5"},
                    client=client,
                )
        assert not circuit_breaker.is_tripped(PROVIDER)

    async def test_disabled_flag_records_nothing_on_429(self, mock_db, monkeypatch):
        """Kill switch → byte-identical to the pre-breaker behavior: the same
        UpstreamError, and no state recorded."""
        monkeypatch.setattr(settings, "provider_circuit_breaker_enabled", False)
        _seed_openai_provider()

        def handler(request):
            return httpx.Response(429, headers={"retry-after": "120"}, content=b"slow down")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(upstream.UpstreamError) as excinfo:
                await _drain(
                    provider_id=PROVIDER,
                    mode="openai",
                    responses_body={"model": "gpt-5"},
                    client=client,
                )
        assert excinfo.value.status_code == 429
        # Re-enabling must not reveal a window recorded while it was off.
        monkeypatch.setattr(settings, "provider_circuit_breaker_enabled", True)
        assert not circuit_breaker.is_tripped(PROVIDER)


class TestCodex401RotationUntouched:
    async def test_codex_401_still_refreshes_and_retries_without_tripping(
        self, mock_db, monkeypatch
    ):
        """The existing token-rotation path must be completely unaffected."""
        encryption.set_encryption_key("test-password", db)
        _seed_codex_provider()
        codex_id = "prov-cb-codex"

        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(401, content=b"expired")
            return httpx.Response(200, content=b"data: [DONE]\n\n")

        from app.core import codex_token_refresh

        async def _fake_refresh(provider_id, *, stale_access_token=None, client=None):
            return True

        monkeypatch.setattr(codex_token_refresh, "refresh_on_401_once", _fake_refresh)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            events = [
                ev
                async for ev in upstream.stream_responses_events(
                    provider_id=codex_id,
                    mode="codex",
                    responses_body={"model": "gpt-5-codex"},
                    client=client,
                )
            ]

        assert calls["n"] == 2           # refreshed and retried exactly once
        assert events == []              # [DONE]-only body
        assert not circuit_breaker.is_tripped(codex_id)


class TestPassthroughTrips:
    async def test_passthrough_json_429_trips(self, mock_db):
        db.create_model_provider(
            provider_id="prov-cb-anthropic",
            name="Anthropic",
            provider_type="anthropic",
            base_url="https://api.anthropic.com",
            enabled=True,
            auth_token="sk-ant-test",
        )

        def handler(request):
            return httpx.Response(429, headers={"retry-after": "90"}, content=b"limit")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(upstream.UpstreamError):
                await upstream.anthropic_passthrough_json(
                    provider_id="prov-cb-anthropic",
                    anthropic_body={"model": "claude-x", "messages": []},
                    client=client,
                )
        trip = circuit_breaker.check("prov-cb-anthropic")
        assert trip is not None
        assert trip.remaining_seconds() == pytest.approx(90.0, abs=2.0)


# ─── router.py pre-flight (refusal WITHOUT an upstream call) ─────────────────

async def _gateway_client() -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=build_gateway_app())
    return httpx.AsyncClient(transport=transport, base_url="http://gw")


async def _never_called_source(**kwargs) -> AsyncIterator[Tuple[str, Dict[str, Any]]]:
    raise AssertionError("upstream must NOT be called while the breaker is open")
    yield  # pragma: no cover


class TestRouterRefusal:
    def setup_method(self):
        routing.get_registry().clear()

    async def _no_upstream(self, monkeypatch) -> Dict[str, int]:
        counters = {"upstream": 0, "provider_loads": 0}

        def _fake_stream(*, provider_id, mode, responses_body, session_id=None, client=None):
            counters["upstream"] += 1
            return _never_called_source()

        async def _fake_provider(provider_id):
            counters["provider_loads"] += 1
            return {"id": provider_id, "provider_type": "chatgpt", "auth_mode": "chatgpt"}

        monkeypatch.setattr(upstream, "stream_responses_events", _fake_stream)
        monkeypatch.setattr(
            "app.core.translator_gateway.router._load_provider", _fake_provider
        )
        return counters

    async def test_buffered_request_refused_while_tripped(self, monkeypatch):
        counters = await self._no_upstream(monkeypatch)
        token = routing.mint_session_token("sess-cb", PROVIDER)
        circuit_breaker.record_rate_limited(PROVIDER, "120")

        async with await _gateway_client() as c:
            resp = await c.post(
                "/v1/messages",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "model": "gpt-5",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": False,
                },
            )

        assert resp.status_code == 429
        body = resp.json()
        assert body["type"] == "error"
        assert body["error"]["type"] == "rate_limit_error"
        assert PROVIDER in body["error"]["message"]
        assert "rate limited" in body["error"]["message"].lower()
        # Nothing was sent upstream, and the provider row was never even loaded.
        assert counters["upstream"] == 0
        assert counters["provider_loads"] == 0

    async def test_buffered_refusal_carries_a_real_retry_after(self, monkeypatch):
        """The retry loop lives in the CLI, so the header is the only thing this
        response can say to it: back off for as long as the breaker is open."""
        await self._no_upstream(monkeypatch)
        token = routing.mint_session_token("sess-cb", PROVIDER)
        circuit_breaker.record_rate_limited(PROVIDER, "120")

        async with await _gateway_client() as c:
            resp = await c.post(
                "/v1/messages",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "model": "gpt-5",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": False,
                },
            )

        assert resp.status_code == 429
        retry_after = resp.headers["retry-after"]
        assert retry_after.isdigit()
        # The cooldown remaining, ceil'd — never 0 (a 0 would mean "retry now").
        assert 100 <= int(retry_after) <= 120

    async def test_retry_after_rounds_up_and_never_says_zero(self, monkeypatch):
        """A window with milliseconds left must not render as ``Retry-After: 0``
        — that reads as "retry now", straight back into the open breaker."""
        from app.core.translator_gateway import router as gw_router

        now = time.time()
        trip = circuit_breaker.BreakerTrip(
            provider_id=PROVIDER, tripped_at=now, until=now + 0.01
        )
        monkeypatch.setattr(gw_router.circuit_breaker, "check", lambda pid: trip)

        resp = gw_router._circuit_breaker_refusal(PROVIDER, False)

        assert resp.status_code == 429
        assert resp.headers["retry-after"] == "1"

    async def test_streaming_refusal_carries_no_retry_after(self, monkeypatch):
        """The streaming refusal is an HTTP 200 with an SSE error frame, so a
        ``Retry-After`` on it would be meaningless — deliberately absent."""
        await self._no_upstream(monkeypatch)
        token = routing.mint_session_token("sess-cb", PROVIDER)
        circuit_breaker.record_rate_limited(PROVIDER, "120")

        async with await _gateway_client() as c:
            resp = await c.post(
                "/v1/messages",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "model": "gpt-5",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": True,
                },
            )

        assert resp.status_code == 200
        assert "retry-after" not in resp.headers

    async def test_streaming_request_refused_with_sse_error_frame(self, monkeypatch):
        counters = await self._no_upstream(monkeypatch)
        token = routing.mint_session_token("sess-cb", PROVIDER)
        circuit_breaker.record_rate_limited(PROVIDER, "120")

        async with await _gateway_client() as c:
            resp = await c.post(
                "/v1/messages",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "model": "gpt-5",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": True,
                },
            )

        # Same shape a real upstream 429 takes on the streaming path today: a
        # terminal Anthropic SSE error frame (finite body — safe over ASGITransport).
        assert resp.status_code == 200
        text = resp.text
        assert "event: error" in text
        assert "rate_limit_error" in text
        assert PROVIDER in text
        assert counters["upstream"] == 0
        assert counters["provider_loads"] == 0

    async def test_request_allowed_once_cooldown_expires(self, monkeypatch):
        """After expiry the gateway resumes calling upstream normally."""
        token = routing.mint_session_token("sess-cb", PROVIDER)
        circuit_breaker.record_rate_limited(PROVIDER, "30", now=time.time() - 31.0)

        async def _fake_provider(provider_id):
            return {"id": provider_id, "provider_type": "chatgpt", "auth_mode": "chatgpt"}

        called = {"n": 0}

        async def _source() -> AsyncIterator[Tuple[str, Dict[str, Any]]]:
            called["n"] += 1
            if False:  # pragma: no cover - empty async generator
                yield "", {}

        def _fake_stream(*, provider_id, mode, responses_body, session_id=None, client=None):
            return _source()

        monkeypatch.setattr(upstream, "stream_responses_events", _fake_stream)
        monkeypatch.setattr(
            "app.core.translator_gateway.router._load_provider", _fake_provider
        )

        async with await _gateway_client() as c:
            resp = await c.post(
                "/v1/messages",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "model": "gpt-5",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": False,
                },
            )

        assert resp.status_code == 200
        assert called["n"] == 1
        assert not circuit_breaker.is_tripped(PROVIDER)

    async def test_other_providers_unaffected(self, monkeypatch):
        """A tripped provider must not block a session bound to a different one."""
        circuit_breaker.record_rate_limited(OTHER_PROVIDER, "120")
        token = routing.mint_session_token("sess-cb", PROVIDER)

        async def _fake_provider(provider_id):
            return {"id": provider_id, "provider_type": "chatgpt", "auth_mode": "chatgpt"}

        async def _source() -> AsyncIterator[Tuple[str, Dict[str, Any]]]:
            if False:  # pragma: no cover - empty async generator
                yield "", {}

        monkeypatch.setattr(
            upstream,
            "stream_responses_events",
            lambda **kwargs: _source(),
        )
        monkeypatch.setattr(
            "app.core.translator_gateway.router._load_provider", _fake_provider
        )

        async with await _gateway_client() as c:
            resp = await c.post(
                "/v1/messages",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "model": "gpt-5",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": False,
                },
            )
        assert resp.status_code == 200

    async def test_disabled_flag_lets_the_request_through(self, monkeypatch):
        """Kill switch ON → today's behavior exactly: the request still dispatches
        even with an open window recorded."""
        circuit_breaker.record_rate_limited(PROVIDER, "300")
        monkeypatch.setattr(settings, "provider_circuit_breaker_enabled", False)
        token = routing.mint_session_token("sess-cb", PROVIDER)

        called = {"n": 0}

        async def _source() -> AsyncIterator[Tuple[str, Dict[str, Any]]]:
            called["n"] += 1
            if False:  # pragma: no cover - empty async generator
                yield "", {}

        async def _fake_provider(provider_id):
            return {"id": provider_id, "provider_type": "chatgpt", "auth_mode": "chatgpt"}

        monkeypatch.setattr(
            upstream, "stream_responses_events", lambda **kwargs: _source()
        )
        monkeypatch.setattr(
            "app.core.translator_gateway.router._load_provider", _fake_provider
        )

        async with await _gateway_client() as c:
            resp = await c.post(
                "/v1/messages",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "model": "gpt-5",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": False,
                },
            )
        assert resp.status_code == 200
        assert called["n"] == 1

    async def test_count_tokens_is_not_blocked(self, monkeypatch):
        """count_tokens is a local estimate with no upstream call, so the breaker
        must not refuse it."""
        circuit_breaker.record_rate_limited(PROVIDER, "300")
        token = routing.mint_session_token("sess-cb", PROVIDER)

        async def _fake_provider(provider_id):
            return {"id": provider_id, "provider_type": "chatgpt", "auth_mode": "chatgpt"}

        monkeypatch.setattr(
            "app.core.translator_gateway.router._load_provider", _fake_provider
        )

        async with await _gateway_client() as c:
            resp = await c.post(
                "/v1/messages/count_tokens",
                headers={"Authorization": f"Bearer {token}"},
                json={"model": "gpt-5", "messages": [{"role": "user", "content": "hello"}]},
            )
        assert resp.status_code == 200
        assert resp.json()["input_tokens"] > 0
