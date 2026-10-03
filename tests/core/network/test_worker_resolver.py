"""One worker resolver for StartSession, HandOffSession and Task/Swarm, and the
``remote_task`` capability a worker publishes in its presence record."""
import pytest

from app.core import runner_capabilities
from app.db import model_providers
from app.network import engine, membership, runners, transport
from tests.core.network.harness import converge, join


def network(workers, *, online=True):
    """A ("Unraid") and B ("Windows") on one account; tests run as B."""
    a, b = workers("unraid"), workers("windows")
    with a.active():
        engine.create_network("Home", device_name="Unraid")
    join(a, b, name="Windows")
    converge([a, b])
    transport._last_contact.pop(a.device_id, None)
    if online:
        transport.note_contact(a.device_id)
    return a, b


def resolve(value, **kwargs):
    return runners.resolve_worker(value, instead="omit worker to run it here",
                                  refused="nothing was dispatched", **kwargs)


def test_resolves_an_exact_name_or_device_id(workers):
    a, b = network(workers)
    with b.active():
        assert resolve("unraid")["device_id"] == a.device_id
        assert resolve(f"  {a.device_id} ")["device_name"] == "Unraid"
        own = resolve("Windows")
        assert own["is_self"] is True and own["device_id"] == b.device_id
        assert resolve(b.device_id)["is_self"] is True


def test_unknown_worker_is_refused_with_the_choices(workers):
    _a, b = network(workers)
    with b.active(), pytest.raises(ValueError) as refused:
        resolve("Laptop")
    assert "Unknown worker 'Laptop'" in str(refused.value)
    assert "Unraid (online)" in str(refused.value) and "Windows (this worker)" in str(refused.value)


def test_offline_worker_is_refused_by_name_never_replaced_by_this_one(workers):
    _a, b = network(workers, online=False)
    with b.active(), pytest.raises(ValueError) as refused:
        resolve("Unraid")
    assert str(refused.value) == ("Unraid is offline; nothing was dispatched. Ask the user to bring it "
                                  "online, or omit worker to run it here.")


def test_inactive_member_is_refused(workers, monkeypatch):
    a, b = network(workers)
    with b.active():
        real_member = engine.context().member

        def member(device_id):
            found = real_member(device_id)
            if found and device_id == a.device_id:
                return membership.Member(**{**found.__dict__, "status": "revoked"})
            return found
        monkeypatch.setattr(engine.context(), "member", member, raising=False)
        with pytest.raises(ValueError, match="Unraid is not an active worker"):
            resolve("Unraid")
        assert runners.other_workers() == []


def test_ambiguous_names_and_bad_values_are_refused():
    listing = {"runners": [
        {"device_id": "dev-1", "device_name": "Box", "online": True, "is_self": False},
        {"device_id": "dev-2", "device_name": "box", "online": True, "is_self": False},
    ]}
    with pytest.raises(ValueError, match="More than one worker is named 'BOX'"):
        resolve("BOX", listing=listing)
    assert resolve("dev-2", listing=listing)["device_name"] == "box"
    for bad in ("", "   ", 7, "x" * 201):
        with pytest.raises(ValueError, match="worker name or device id"):
            resolve(bad, listing=listing)


def test_not_networked_is_refused(workers):
    solo = workers("solo")
    with solo.active():
        with pytest.raises(ValueError, match="not part of a worker network; omit worker to run it here"):
            resolve("Unraid")
        assert runners.other_workers() == []


def test_other_workers_lists_peers_online_or_not(workers):
    _a, b = network(workers, online=False)
    with b.active():
        rows = runners.other_workers()
    assert [(row["device_name"], row["online"]) for row in rows] == [("Unraid", False)]


def test_presence_carries_remote_task_and_provider_kinds(workers):
    a, b = network(workers)
    with b.active():
        model_providers.create_model_provider("p-openai", "Mine", "openai", "https://api.example.invalid",
                                              auth_token="enc:x")
        model_providers.create_model_provider("p-empty", "No key", "openrouter", "https://or.example.invalid")
        model_providers.create_model_provider("p-off", "Off", "custom", "https://c.example.invalid",
                                              auth_token="enc:y", enabled=False)
        capabilities = transport._capabilities()
        assert capabilities["remote_task"] == runner_capabilities.REMOTE_TASK_VERSION == 1
        assert capabilities["provider_kinds"] == ["openai"]
        engine.publish_presence(capabilities=capabilities, version="test")
    converge([a, b])
    with a.active():
        row = runners.find(b.device_id)
    assert row["capabilities"]["remote_task"] == 1
    assert row["capabilities"]["provider_kinds"] == ["openai"]
