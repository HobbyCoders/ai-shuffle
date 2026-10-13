"""HTTP contract of the assistant's "Run work on" preference."""
from tests.api.test_assistant import client  # noqa: F401 - fixture
from tests.core.test_assistant_work_worker import network  # noqa: F401 - fixture


def test_read_and_save_the_work_worker(client, network):
    state = client.get("/api/v1/assistant/worker")
    assert state.status_code == 200 and state.json()["device_id"] is None
    assert [row["name"] for row in state.json()["workers"]] == ["Laptop", "OldBox", "ServerWorker"]

    saved = client.put("/api/v1/assistant/worker", json={"device_id": "dev-unraid"})
    assert saved.status_code == 200 and saved.json()["device_id"] == "dev-unraid"
    assert client.get("/api/v1/assistant/worker").json()["device_id"] == "dev-unraid"

    refused = client.put("/api/v1/assistant/worker", json={"device_id": "dev-old"})
    assert refused.status_code == 409 and "needs an update" in refused.json()["detail"]
    assert client.get("/api/v1/assistant/worker").json()["device_id"] == "dev-unraid"

    cleared = client.put("/api/v1/assistant/worker", json={"device_id": None})
    assert cleared.status_code == 200 and cleared.json()["device_id"] is None
    assert client.put("/api/v1/assistant/worker", json={"device_id": "x", "extra": 1}).status_code == 422
