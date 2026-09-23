"""Worker-side license leases: verification, storage, renewal and the run gate."""
import base64
import json
import time

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from app.core import license as licensing

DAY = 86400


class Keys:
    def __init__(self):
        self.private = Ed25519PrivateKey.generate()
        self.spki = base64.b64encode(self.private.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)).decode()

    def lease(self, install_id, key_id="test", **overrides):
        now = int(time.time())
        payload = {"v": 1, "lease_id": "l1", "account_id": "acct-1", "license_id": "lic-1", "install_id": install_id,
                   "fingerprint": "f" * 64, "plan": "monthly", "status": "active", "seats": 3, "seats_used": 1,
                   "grace": False, "grace_until": None, "key_hint": "7QK2", "issued_at": now, "expires_at": now + 14 * DAY}
        payload.update(overrides)
        body = json.dumps(payload).encode()
        return {"format": "shuffle-lease-v1", "key_id": key_id, "payload": base64.b64encode(body).decode(),
                "signature": base64.b64encode(self.private.sign(body)).decode()}


@pytest.fixture
def keys():
    return Keys()


@pytest.fixture
def store(monkeypatch, keys):
    values = {}
    monkeypatch.setattr(licensing.database, "get_system_setting", lambda key: values.get(key))
    monkeypatch.setattr(licensing.database, "get_system_setting_cached", lambda key, ttl=5.0: values.get(key))
    monkeypatch.setattr(licensing.database, "set_system_setting", lambda key, value: values.__setitem__(key, value))
    monkeypatch.setattr(licensing, "_state_key", lambda: "license_state:test")
    monkeypatch.setattr(licensing, "LEASE_PUBLIC_KEYS", {"test": keys.spki})
    monkeypatch.setattr(licensing.settings, "license_server_url", "https://license.example.com")
    monkeypatch.setattr(licensing.settings, "license_beta", False)
    licensing._verified.clear()
    licensing._lock = None
    return values


def state(values):
    return json.loads(values.get("license_state:test") or "{}")


def save(values, **fields):
    values["license_state:test"] = json.dumps(fields)


class FakeServer:
    """Stands in for licensing._post: answers per path, records requests."""

    def __init__(self, monkeypatch):
        self.answers = {}
        self.requests = []
        monkeypatch.setattr(licensing, "_post", self.post)

    async def post(self, path, body):
        self.requests.append((path, body))
        answer = self.answers[path]
        if isinstance(answer, Exception):
            raise answer
        return answer(body) if callable(answer) else answer


@pytest.fixture
def server(monkeypatch):
    return FakeServer(monkeypatch)


# ---------------------------------------------------------------------------
# Beta mode
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url,beta,keys_present", [("", False, True), ("https://license.example.com", True, True), ("https://license.example.com", False, False)])
def test_beta_mode_is_always_valid(monkeypatch, url, beta, keys_present):
    monkeypatch.setattr(licensing.settings, "license_server_url", url)
    monkeypatch.setattr(licensing.settings, "license_beta", beta)
    monkeypatch.setattr(licensing, "LEASE_PUBLIC_KEYS", {"k": "x"} if keys_present else {})
    monkeypatch.setattr(licensing.database, "get_system_setting", lambda key: pytest.fail("beta reads nothing"))
    assert licensing.beta_mode()
    assert licensing.license_allows_agent_runs() == (True, None)
    s = licensing.status()
    assert s["mode"] == "beta" and s["state"] == "beta" and s["allowed"] and s["banner"] is None


async def test_beta_mode_refuses_activation_without_calling_out(monkeypatch, server):
    monkeypatch.setattr(licensing.settings, "license_server_url", "")
    with pytest.raises(licensing.LicenseError) as err:
        await licensing.activate("ASH-AAAAA-AAAAA-AAAAA-AAAAA")
    assert err.value.status == 409
    assert server.requests == []


# ---------------------------------------------------------------------------
# Gate states
# ---------------------------------------------------------------------------

def test_unlicensed_blocks_new_runs_only_with_a_setup_message(store):
    allowed, reason = licensing.license_allows_agent_runs()
    assert allowed is False
    assert "Settings > License" in reason and "data is not affected" in reason
    assert licensing.status()["state"] == "unlicensed"


def test_active_lease_allows_runs(store, keys):
    save(store, install_id="inst-1", lease=keys.lease("inst-1"))
    assert licensing.license_allows_agent_runs() == (True, None)
    s = licensing.status()
    assert s["state"] == "active" and s["plan"] == "monthly" and s["seats"] == 3 and s["seats_used"] == 1
    assert s["account_id"] == "acct-1" and s["key_hint"] == "7QK2" and s["banner"] is None
    assert s["expires_at"].endswith("Z")


def test_trial_lease_reports_trialing(store, keys):
    save(store, install_id="inst-1", lease=keys.lease("inst-1", status="trialing", plan="trial"))
    assert licensing.status()["state"] == "trialing"


def test_expired_lease_blocks_and_says_data_is_safe(store, keys):
    now = int(time.time())
    save(store, install_id="inst-1", lease=keys.lease("inst-1", issued_at=now - 15 * DAY, expires_at=now - DAY))
    allowed, reason = licensing.license_allows_agent_runs()
    assert not allowed and "lease ended" in reason and "data is not affected" in reason
    s = licensing.status()
    assert s["state"] == "expired" and s["banner"]["severity"] == "error"


def test_payment_grace_keeps_running_with_a_banner(store, keys):
    now = int(time.time())
    save(store, install_id="inst-1", lease=keys.lease("inst-1", grace=True, status="grace", grace_until=now + 10 * DAY, expires_at=now + 10 * DAY))
    assert licensing.license_allows_agent_runs() == (True, None)
    s = licensing.status()
    assert s["state"] == "grace" and s["grace"] is True and s["grace_until"]
    assert "payment failed" in s["banner"]["message"]


def test_offline_lease_warns_in_its_last_three_days_then_stops(store, keys):
    now = int(time.time())
    save(store, install_id="inst-1", lease=keys.lease("inst-1", issued_at=now - 12 * DAY, expires_at=now + 2 * DAY))
    s = licensing.status()
    assert s["state"] == "expiring" and s["allowed"] and s["banner"]["severity"] == "warning"
    # Fourteen days offline and the lease is over.
    assert licensing.status(now=now + 2 * DAY + 1)["state"] == "expired"


@pytest.mark.parametrize("tamper", ["other_install", "forged", "unknown_key", "garbage"])
def test_leases_that_do_not_verify_block_runs(store, keys, tamper):
    lease = keys.lease("inst-1")
    if tamper == "other_install":
        lease = keys.lease("inst-2")
    elif tamper == "forged":
        payload = json.loads(base64.b64decode(lease["payload"]))
        payload["seats"] = 99
        lease["payload"] = base64.b64encode(json.dumps(payload).encode()).decode()
    elif tamper == "unknown_key":
        lease = Keys().lease("inst-1")
    elif tamper == "garbage":
        lease = {"format": "shuffle-lease-v1", "key_id": "test", "payload": "%%%", "signature": "x"}
    save(store, install_id="inst-1", lease=lease)
    allowed, reason = licensing.license_allows_agent_runs()
    assert not allowed and "could not be verified" in reason
    assert licensing.status()["state"] == "invalid"


def test_lease_header_carries_the_signed_envelope_for_the_hub(store, keys, monkeypatch):
    assert licensing.lease_header() is None
    lease = keys.lease("inst-1")
    save(store, install_id="inst-1", lease=lease)
    header = licensing.lease_header()
    assert "=" not in header and "+" not in header and "/" not in header
    assert json.loads(base64.urlsafe_b64decode(header + "=" * (-len(header) % 4))) == lease
    monkeypatch.setattr(licensing.settings, "license_beta", True)
    assert licensing.lease_header() is None


def test_gate_fails_open_when_state_cannot_be_read(store, monkeypatch):
    def broken(key, ttl=5.0):
        raise RuntimeError("database unavailable")
    monkeypatch.setattr(licensing.database, "get_system_setting_cached", broken)
    assert licensing.license_allows_agent_runs() == (True, None)


# ---------------------------------------------------------------------------
# Activation, renewal, deactivation
# ---------------------------------------------------------------------------

async def test_activation_stores_a_verified_lease_and_reuses_the_install_id(store, keys, server):
    server.answers["activate"] = lambda body: (200, {"lease": keys.lease(body["install_id"]), "install_token": "a" * 64, "seats": 3, "seats_used": 1})
    s = await licensing.activate("  ASH-AAAAA-AAAAA-AAAAA-AAAAA ")
    assert s["state"] == "active" and s["allowed"]
    path, body = server.requests[0]
    assert path == "activate" and body["key"] == "ASH-AAAAA-AAAAA-AAAAA-AAAAA"
    assert len(body["fingerprint"]) == 64 and body["app_version"] and body["name"]
    assert set(body) == {"key", "install_id", "fingerprint", "name", "app_version"}
    saved = state(store)
    assert saved["install_id"] == body["install_id"] and saved["install_token"] == "a" * 64
    assert "ASH-" not in json.dumps(saved), "the key itself is never stored"
    first_install = body["install_id"]
    server.answers["activate"] = lambda body: (200, {"lease": keys.lease(body["install_id"]), "install_token": "b" * 64})
    await licensing.activate("ASH-AAAAA-AAAAA-AAAAA-AAAAA")
    assert server.requests[1][1]["install_id"] == first_install


async def test_install_token_is_encrypted_when_credential_storage_is_ready(store, keys, server, monkeypatch):
    from app.core import encryption
    monkeypatch.setattr(encryption, "is_encryption_ready", lambda: True)
    monkeypatch.setattr(encryption, "encrypt_value", lambda v: "enc:" + v[::-1])
    monkeypatch.setattr(encryption, "decrypt_value", lambda v: v[4:][::-1])
    server.answers["activate"] = lambda body: (200, {"lease": keys.lease(body["install_id"]), "install_token": "0123456789abcdef" * 4})
    await licensing.activate("ASH-AAAAA-AAAAA-AAAAA-AAAAA")
    assert state(store)["install_token"].startswith("enc:")
    server.answers["renew"] = lambda body: (200, {"lease": keys.lease(body["install_id"])})
    await licensing.refresh()
    assert server.requests[-1][1]["install_token"] == "0123456789abcdef" * 4


async def test_activation_errors_surface_the_server_message_and_seats(store, server):
    installs = [{"install_id": "x", "name": "Tower", "last_seen": "2026-09-01T00:00:00Z"}]
    server.answers["activate"] = (409, {"error": "All 3 workers on this license are in use.", "code": "seats_full", "seats": 3, "installs": installs})
    with pytest.raises(licensing.LicenseError) as err:
        await licensing.activate("ASH-AAAAA-AAAAA-AAAAA-AAAAA")
    assert err.value.status == 409 and err.value.extra["code"] == "seats_full" and err.value.extra["installs"] == installs
    server.answers["activate"] = (404, {"error": "License key not recognised"})
    with pytest.raises(licensing.LicenseError) as err:
        await licensing.activate("ASH-AAAAA-AAAAA-AAAAA-AAAAA")
    assert err.value.status == 404 and err.value.message == "License key not recognised"
    assert state(store) == {}


async def test_activation_rejects_a_lease_this_build_cannot_verify(store, server):
    server.answers["activate"] = lambda body: (200, {"lease": Keys().lease(body["install_id"]), "install_token": "a" * 64})
    with pytest.raises(licensing.LicenseError) as err:
        await licensing.activate("ASH-AAAAA-AAAAA-AAAAA-AAAAA")
    assert err.value.status == 502
    assert state(store) == {}


async def test_renewal_replaces_the_lease_and_schedules_a_jittered_daily_check(store, keys, server):
    now = int(time.time())
    save(store, install_id="inst-1", install_token="a" * 64, lease=keys.lease("inst-1", issued_at=now - 5 * DAY, expires_at=now + 9 * DAY), failures=2, last_error="old")
    server.answers["renew"] = lambda body: (200, {"lease": keys.lease("inst-1")})
    s = await licensing.refresh()
    saved = state(store)
    assert s["state"] == "active" and saved["failures"] == 0 and saved["last_error"] is None
    assert json.loads(base64.b64decode(saved["lease"]["payload"]))["issued_at"] >= now
    assert DAY - 2 * 3600 - 5 <= saved["next_refresh_at"] - time.time() <= DAY + 2 * 3600 + 5
    assert server.requests[0][1]["install_id"] == "inst-1"


async def test_renewal_failure_keeps_the_lease_and_backs_off(store, keys, server):
    lease = keys.lease("inst-1")
    save(store, install_id="inst-1", install_token="a" * 64, lease=lease)
    server.answers["renew"] = licensing.LicenseError(503, "Could not reach the license server.")
    for attempt in (1, 2, 3):
        s = await licensing.refresh()
        saved = state(store)
        assert saved["failures"] == attempt and saved["lease"] == lease
        delay = saved["next_refresh_at"] - time.time()
        base = min(licensing.RETRY_MAX, licensing.RETRY_BASE * 2 ** (attempt - 1))
        assert 0.5 * base - 5 <= delay <= 1.5 * base + 5
    assert s["allowed"] and s["last_error"] == "Could not reach the license server."
    assert licensing._retry_delay(30) <= 1.5 * licensing.RETRY_MAX


async def test_server_saying_no_keeps_the_current_lease_until_it_ends(store, keys, server):
    save(store, install_id="inst-1", install_token="a" * 64, lease=keys.lease("inst-1"))
    server.answers["renew"] = (402, {"error": "The subscription for this license has ended.", "status": "canceled"})
    s = await licensing.refresh()
    assert s["state"] == "ending" and s["allowed"]
    assert s["server_status"]["code"] == "canceled"
    assert "subscription for this license has ended" in s["banner"]["message"]
    # A later successful renewal clears it.
    server.answers["renew"] = lambda body: (200, {"lease": keys.lease("inst-1")})
    assert (await licensing.refresh())["state"] == "active"


async def test_deactivation_frees_the_seat_and_keeps_the_install_id(store, keys, server):
    save(store, install_id="inst-1", install_token="a" * 64, lease=keys.lease("inst-1"), key_hint="7QK2")
    server.answers["deactivate"] = (200, {"ok": True, "seats_used": 0})
    s = await licensing.deactivate()
    assert server.requests == [("deactivate", {"install_id": "inst-1", "install_token": "a" * 64})]
    assert s["seat_released"] is True and s["state"] == "unlicensed"
    assert state(store) == {"install_id": "inst-1"}


async def test_deactivation_offline_still_forgets_the_lease(store, keys, server):
    save(store, install_id="inst-1", install_token="a" * 64, lease=keys.lease("inst-1"))
    server.answers["deactivate"] = licensing.LicenseError(503, "offline")
    s = await licensing.deactivate()
    assert s["seat_released"] is False and s["state"] == "unlicensed"


async def test_release_seat_uses_the_key_to_free_another_worker(store, server):
    server.answers["deactivate"] = (200, {"ok": True, "seats_used": 2})
    assert await licensing.release_seat(" ASH-AAAAA-AAAAA-AAAAA-AAAAA ", "other-install") == {"ok": True, "seats_used": 2}
    assert server.requests == [("deactivate", {"key": "ASH-AAAAA-AAAAA-AAAAA-AAAAA", "install_id": "other-install"})]
    server.answers["deactivate"] = (404, {"error": "No such worker on this license"})
    with pytest.raises(licensing.LicenseError) as err:
        await licensing.release_seat("ASH-AAAAA-AAAAA-AAAAA-AAAAA", "other-install")
    assert err.value.status == 404 and err.value.message == "No such worker on this license"


def test_refresh_schedule(store, keys):
    now = time.time()
    assert licensing._seconds_until_refresh(now) == licensing.REFRESH_INTERVAL  # nothing to renew
    save(store, install_id="inst-1", install_token="a" * 64, lease=keys.lease("inst-1"), next_refresh_at=now - 100)
    assert licensing._seconds_until_refresh(now) == licensing.FIRST_CHECK_DELAY
    save(store, install_id="inst-1", install_token="a" * 64, lease=keys.lease("inst-1"), next_refresh_at=now + 5000)
    assert licensing._seconds_until_refresh(now) == pytest.approx(5000)


# ---------------------------------------------------------------------------
# HTTP transport
# ---------------------------------------------------------------------------

@pytest.fixture
def transport(monkeypatch):
    calls = []
    handler = {"fn": lambda request: httpx.Response(200, json={"ok": True})}
    real = httpx.AsyncClient

    def factory(**kwargs):
        def record(request):
            calls.append(request)
            return handler["fn"](request)
        return real(transport=httpx.MockTransport(record), **kwargs)

    monkeypatch.setattr(licensing.httpx, "AsyncClient", factory)
    return calls, handler


async def test_post_uses_https_and_maps_server_trouble_to_503(store, transport):
    calls, handler = transport
    code, data = await licensing._post("renew", {"a": 1})
    assert code == 200 and data == {"ok": True}
    assert str(calls[0].url) == "https://license.example.com/v1/license/renew"
    handler["fn"] = lambda request: httpx.Response(500, text="boom")
    with pytest.raises(licensing.LicenseError) as err:
        await licensing._post("renew", {})
    assert err.value.status == 503
    handler["fn"] = lambda request: httpx.Response(402, json={"error": "no", "status": "canceled"})
    assert await licensing._post("renew", {}) == (402, {"error": "no", "status": "canceled"})


async def test_post_refuses_plain_http_to_a_remote_host(store, monkeypatch, transport):
    monkeypatch.setattr(licensing.settings, "license_server_url", "http://license.example.com")
    with pytest.raises(licensing.LicenseError) as err:
        await licensing._post("renew", {})
    assert err.value.status == 503 and transport[0] == []
