"""The remote Task wire accepts Swarm's entire 64-character alias."""
from app.core.task_registry import task_registry
from app.db import database
from tests.core.network.test_remote_task_control import dispatch, network, kicks  # noqa: F401


def test_remote_dispatch_preserves_64_character_roster_name(workers, monkeypatch, kicks):
    runner, caller, _ = network(workers, monkeypatch)
    alias = "a" * 63 + "x"
    with caller.active():
        result = dispatch(runner, caller, roster_name=alias)
    assert result["ok"] is True, result
    with caller.active():
        child_id = result["worker_session_id"]
        assert task_registry.get_task("caller", child_id)["name"] == alias
    with runner.active():
        assert database.get_session(child_id)["owner_device_id"] == runner.device_id
