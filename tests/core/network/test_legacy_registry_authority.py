"""Network directory rows must never mutate the retired shared registry."""
from unittest.mock import Mock

import pytest
from fastapi import HTTPException

from app.api import cluster as api
from app.core.config import settings
from app.db import database
from app.network import engine, runners
from tests.core.network.harness import Worker


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["rename", "approve", "delete"])
@pytest.mark.parametrize("identifier", ["device", "legacy"])
async def test_network_directory_cannot_use_legacy_registry_mutations(tmp_path, monkeypatch, operation, identifier):
    worker = Worker("network-authority", tmp_path)
    spies = {}
    for name in ("set_install_name", "set_install_approval", "delete_install", "delete_usage_snapshot"):
        spies[name] = Mock(return_value={"id": "should-not-be-written"})
        monkeypatch.setattr(database, name, spies[name])
    try:
        with worker.active():
            engine.create_network("Registry guard test")
            # Use the real producing directory, not a synthetic UI-only flag.
            row = runners.directory()["runners"][0]
            target = row["id"] if identifier == "device" else settings.effective_instance_id
            with pytest.raises(HTTPException) as caught:
                if operation == "rename":
                    await api.update_install(target, api.InstallRenameRequest(device_name="Wrong registry"), token="admin")
                elif operation == "approve":
                    await api.update_install_approval(target, api.InstallApprovalRequest(approved=True), token="admin")
                else:
                    await api.delete_install(target, token="admin")
            assert caught.value.status_code == 409
            for spy in spies.values():
                spy.assert_not_called()
    finally:
        worker.close()
