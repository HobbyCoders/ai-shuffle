"""Android command service: argument validation edges, device authorization and
the execute() wait loop. The queue and device lookups are replaced with
in-memory doubles, so nothing touches a database or a phone.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.services import mobile_tools as service
from app.services.mobile_tools import CommandError


def _ok(name, args):
    return json.loads(service.validate_arguments(name, args))


def _err(name, args):
    with pytest.raises(CommandError) as exc:
        service.validate_arguments(name, args)
    return str(exc.value)


# ---------------------------------------------------------------------------
# bounded_json
# ---------------------------------------------------------------------------


def test_bounded_json_rejections():
    for value in (None, [], "x"):
        with pytest.raises(CommandError, match="object_required"):
            service.bounded_json(value, 100)
    with pytest.raises(CommandError, match="invalid_json"):
        service.bounded_json({1: "int key"}, 100)
    with pytest.raises(CommandError, match="invalid_json"):
        service.bounded_json({"x": {"nested": object()}}, 100)
    with pytest.raises(CommandError, match="invalid_json"):
        service.bounded_json({"x": float("inf")}, 100)
    deep = {"x": 1}
    for _ in range(14):
        deep = {"d": [deep]}
    with pytest.raises(CommandError, match="payload_too_deep"):
        service.bounded_json(deep, 10_000)
    with pytest.raises(CommandError, match="payload_too_large"):
        service.bounded_json({"x": "é" * 60}, 100)  # multi-byte counted in bytes
    assert service.bounded_json({"b": [True, None, 1.5], "a": "s"}, 100) == '{"a":"s","b":[true,null,1.5]}'


# ---------------------------------------------------------------------------
# validate_arguments
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("url", [
    "http://[::1",            # unterminated IPv6 host: urlsplit raises ValueError
    "https://example.com:99999/",  # port out of range
    "https://example.com:abc/",    # non-numeric port
    "ftp://example.com/",
    "https:///no-host",
    "https://user:pw@example.com/",
    "https://user@example.com/",
])
def test_open_url_rejects_malformed_urls_as_command_errors(url):
    """Every malformed URL is a CommandError, never a bare ValueError that escapes execute()."""
    assert _err("open_url", {"url": url}) == "invalid_url"


def test_open_url_accepts_ports_and_ipv6():
    assert _ok("open_url", {"url": "https://example.com:8443/x?y=1"})["url"].endswith("y=1")
    assert _ok("open_url", {"url": "http://[::1]:8080/"})


async def test_execute_returns_error_for_malformed_ipv6_url(monkeypatch):
    monkeypatch.setattr(service, "authorized_devices", lambda s: pytest.fail("must fail validation first"))
    assert await service.execute("s", "dev", "open_url", {"url": "http://[::1/x"}) == {"error": "invalid_url"}


@pytest.mark.parametrize("name,args,error", [
    ("open_app", {"package_name": "com.example.app"}, None),
    ("open_app", {"package_name": "noDots"}, "invalid_package"),
    ("set_alarm", {"hour": 0, "minute": 59, "label": "wake"}, None),
    ("set_alarm", {"hour": 7, "minute": 60}, "invalid_alarm"),
    ("set_alarm", {"hour": -1, "minute": 0}, "invalid_alarm"),
    ("set_timer", {"seconds": 86400}, None),
    ("set_timer", {"seconds": 86401}, "invalid_timer"),
    ("set_timer", {"seconds": 1.0}, "invalid_arguments"),
    ("scroll", {"ref": "n", "direction": "left"}, None),
    ("navigate_to", {"destination": "   "}, "invalid_arguments"),
    ("navigate_to", {"destination": "x" * 2049}, "invalid_arguments"),
    ("get_location", {}, None),
    ("get_location", {"extra": 1}, "invalid_arguments"),
    ("create_calendar_event", {"title": "t", "start_time": "2026-01-01T10:00:00Z"}, None),
    ("create_calendar_event", {"title": "t", "start_time": "2026-01-01T10:00:00+02:00",
                               "end_time": "2026-01-01T09:00:00Z"}, None),
    ("create_calendar_event", {"title": "t", "start_time": "2026-01-01T10:00:00Z",
                               "end_time": "2026-01-01T09:00:00Z"}, "invalid_calendar_time"),
    ("create_calendar_event", {"title": "t", "start_time": "2026-01-01T10:00:00Z",
                               "end_time": "2026-01-01T11:00:00"}, "invalid_calendar_time"),
    ("create_calendar_event", {"title": "t", "start_time": "tomorrow"}, "invalid_calendar_time"),
    ("nope", {}, "unknown_tool"),
])
def test_validate_arguments_matrix(name, args, error):
    if error is None:
        assert _ok(name, args) == args
    else:
        assert _err(name, args) == error


# ---------------------------------------------------------------------------
# authorized_devices / list_devices
# ---------------------------------------------------------------------------


NOW = datetime.now(timezone.utc)


def _device(device_id, **over):
    base = {"device_id": device_id, "device_name": device_id.upper(), "scopes": ["mobile:actions"],
            "api_key_expires_at": NOW + timedelta(days=1)}
    base.update(over)
    return base


@pytest.fixture
def devices(monkeypatch):
    table = {}
    clients = {}
    monkeypatch.setattr(service.mobile, "get_mobile_device", table.get)
    monkeypatch.setattr(service.pairing, "get_paired_client", clients.get)
    monkeypatch.setattr(service, "device_authorizer", lambda sid: list(table))
    return table, clients


def test_authorizer_contract(monkeypatch):
    with pytest.raises(CommandError, match="controller_context_missing"):
        service.authorized_devices("")
    monkeypatch.setattr(service, "device_authorizer", lambda sid: "a")
    with pytest.raises(CommandError, match="controller_context_invalid"):
        service.authorized_devices("s")
    monkeypatch.setattr(service, "device_authorizer", lambda sid: ["a", 3])
    with pytest.raises(CommandError, match="controller_context_invalid"):
        service.authorized_devices("s")


def test_authorized_devices_filters(devices):
    table, clients = devices
    table["ok"] = _device("ok")
    table["iso"] = _device("iso", api_key_expires_at=(NOW + timedelta(days=1)).isoformat().replace("+00:00", "Z"))
    table["revoked"] = _device("revoked", revoked_at="2026-01-01")
    table["noscope"] = _device("noscope", scopes=["controller"])
    table["expired"] = _device("expired", api_key_expires_at=NOW - timedelta(seconds=1))
    # Legacy rows: no explicit expiry -> created_at + 365 days, else expired.
    table["legacy_ok"] = _device("legacy_ok", api_key_expires_at=None, created_at=NOW - timedelta(days=10))
    table["legacy_old"] = _device("legacy_old", api_key_expires_at=None, created_at=NOW - timedelta(days=400))
    table["legacy_none"] = _device("legacy_none", api_key_expires_at=None)
    table["client_ok"] = _device("client_ok", paired_client_id="pc1")
    table["client_revoked"] = _device("client_revoked", paired_client_id="pc2")
    table["client_missing"] = _device("client_missing", paired_client_id="pc3")
    clients["pc1"] = {"id": "pc1", "revoked_at": None}
    clients["pc2"] = {"id": "pc2", "revoked_at": "2026-01-01"}
    ids = {d["device_id"] for d in service.authorized_devices("s")}
    assert ids == {"ok", "iso", "legacy_ok", "client_ok"}


@pytest.mark.parametrize("field,value", ids=["expires-datetime","expires-iso","created-text"], argvalues=[
    # Naive timestamps (no offset) are read as UTC rather than raising TypeError.
    ("api_key_expires_at", (NOW + timedelta(days=1)).replace(tzinfo=None)),
    ("api_key_expires_at", (NOW + timedelta(days=1)).replace(tzinfo=None).isoformat()),
    ("created_at", (NOW - timedelta(days=1)).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")),
])
def test_authorized_devices_tolerates_naive_timestamps(devices, field, value):
    table, _ = devices
    dev = _device("n", api_key_expires_at=None)
    dev[field] = value
    table["n"] = dev
    assert [d["device_id"] for d in service.authorized_devices("s")] == ["n"]


def test_list_devices_online_state(devices, monkeypatch):
    table, _ = devices
    table["on"] = _device("on")
    table["off"] = _device("off")
    table["new"] = _device("new")
    states = {
        "on": {"enabled": True, "tools": '["read_screen"]', "heartbeat_at": NOW.isoformat()},
        "off": {"enabled": True, "tools": "[]", "heartbeat_at": NOW - timedelta(minutes=5)},
    }
    monkeypatch.setattr(service.queue, "state", states.get)
    out = {d["device_id"]: d for d in service.list_devices("s")["devices"]}
    assert out["on"]["online"] is True and out["on"]["tools"] == ["read_screen"]
    assert out["off"]["online"] is False and out["off"]["last_seen"]
    assert out["new"] == {"device_id": "new", "name": "NEW", "enabled": False, "tools": [],
                          "last_seen": None, "online": False}


def test_list_devices_empty(devices):
    assert service.list_devices("s") == {"devices": [], "error": "no_devices"}


# ---------------------------------------------------------------------------
# execute()
# ---------------------------------------------------------------------------


@pytest.fixture
def wired(monkeypatch):
    state = {"authorized": ["dev"], "outcomes": [], "cancelled": []}
    monkeypatch.setattr(service, "authorized_devices",
                        lambda sid: [{"device_id": d} for d in state["authorized"]])
    monkeypatch.setattr(service.queue, "enqueue", lambda *a: {"id": "cmd-1"})

    def outcome(cid, sid):
        return state["outcomes"].pop(0) if state["outcomes"] else {"status": "pending", "result": None}

    monkeypatch.setattr(service.queue, "outcome", outcome)
    monkeypatch.setattr(service.queue, "cancel", lambda cid, sid: state["cancelled"].append(cid))
    return state


@pytest.mark.parametrize("device_id,timeout,error", [
    ("", 5, "explicit_device_id_required"),
    (None, 5, "explicit_device_id_required"),
    ("x" * 257, 5, "explicit_device_id_required"),
    ("dev", 0, "invalid_timeout"),
    ("dev", 26, "invalid_timeout"),
    ("dev", float("nan"), "invalid_timeout"),
    ("dev", True, "invalid_timeout"),
    ("dev", "5", "invalid_timeout"),
])
async def test_execute_rejects_bad_inputs(wired, device_id, timeout, error):
    assert await service.execute("s", device_id, "read_screen", {}, timeout=timeout) == {"error": error}
    assert wired["cancelled"] == []


async def test_execute_unauthorized_device(wired):
    wired["authorized"] = ["other"]
    assert await service.execute("s", "dev", "read_screen", {}) == {"error": "device_not_authorized"}


async def test_execute_queue_error_is_returned(wired, monkeypatch):
    monkeypatch.setattr(service.queue, "enqueue", lambda *a: {"error": "device_busy"})
    assert await service.execute("s", "dev", "read_screen", {}) == {"error": "device_busy"}
    assert wired["cancelled"] == []


async def test_execute_non_handoff_result_is_passed_through(wired):
    wired["outcomes"] = [{"status": "pending"}, {"status": "completed", "result": '{"text":"screen","success":true}'}]
    out = await service.execute("s", "dev", "read_screen", {}, timeout=5)
    assert out == {"command_id": "cmd-1", "result": {"text": "screen", "success": True}}
    assert wired["cancelled"] == ["cmd-1"]


@pytest.mark.parametrize("result,status", [
    ({"status": "completed"}, "launched_confirmation"),
    ({"status": "launched"}, "launched_confirmation"),
    ({"success": True, "error": "denied"}, None),
    ({"success": False}, None),
])
async def test_execute_handoff_status(wired, result, status):
    wired["outcomes"] = [{"status": "completed", "result": json.dumps(result)}]
    out = await service.execute("s", "dev", "set_timer", {"seconds": 5}, timeout=5)
    assert out["result"].get("status") == (status or result.get("status"))


@pytest.mark.parametrize("row", [None, {"status": "expired"}, {"status": "cancelled"}])
async def test_execute_expired_or_cancelled(wired, monkeypatch, row):
    monkeypatch.setattr(service.queue, "outcome", lambda cid, sid: row)
    assert await service.execute("s", "dev", "read_screen", {}, timeout=5) == {
        "error": "command_expired_or_cancelled",
    }
    assert wired["cancelled"] == ["cmd-1"]


async def test_execute_revocation_during_wait(wired, monkeypatch):
    calls = {"n": 0}

    def authorized(sid):
        calls["n"] += 1
        return [{"device_id": "dev"}] if calls["n"] == 1 else []

    monkeypatch.setattr(service, "authorized_devices", authorized)
    assert await service.execute("s", "dev", "read_screen", {}, timeout=5) == {"error": "device_not_authorized"}
    assert wired["cancelled"] == ["cmd-1"]
