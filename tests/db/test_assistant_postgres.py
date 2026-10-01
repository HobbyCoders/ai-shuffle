"""Assistant transaction and restart contracts on disposable PostgreSQL."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from tests.db.postgres_harness import pg_schema, requires_real_pg  # noqa: F401

pytestmark = requires_real_pg


def test_real_postgres_atomic_fingerprint_admission_and_restart(monkeypatch):
    from app.core import assistant_service as service
    from app.core.config import settings
    from app.db import assistant as store, base, database

    identity = "assistant-pg-" + uuid4().hex
    agent_id = identity + "-agent"
    monkeypatch.setattr(settings, "instance_id", identity)
    assert store.get_state(identity) is None  # Never auto-enable at bootstrap.
    database.create_profile(agent_id, "PG owner assistant", None, {})
    service.configure({"agent_id": agent_id, "enabled": True})
    with ThreadPoolExecutor(max_workers=2) as executor:
        homes = list(executor.map(lambda _: service.open_home(), range(2)))
    assert homes[0]["id"] == homes[1]["id"]
    from app.core import connector_service, connector_packages
    monkeypatch.setattr(connector_packages, "require_installed", lambda _: "test-runtime")
    connector = asyncio.run(connector_service.create_connector("Disposable source", "gmail", {}, []))
    connector_id = connector["id"]
    assert connector["api_user_id"] is not None
    database.update_connector(connector_id, is_active=True)
    database.update_connector_status(connector_id, "connected")
    service.set_source(connector_id, True)

    def produce(_):
        return service.record_proactive_event(connector_id, "email_received", {"message_id": "one", "subject": "External data"})

    with ThreadPoolExecutor(max_workers=4) as executor:
        observations = list(executor.map(produce, range(12)))
    assert sum(len(value) for value in observations) == 1
    store.record_observations(identity, connector_id, [], {"seen_ids": ["one"]})
    with ThreadPoolExecutor(max_workers=4) as executor:
        admissions = list(executor.map(lambda _: store.admit_run(identity), range(8)))
    run, = [value for value in admissions if value]
    assert run["event_ids"] == [next(value[0]["id"] for value in observations if value)]
    base.close_pool()
    base.init_pool(min_conn=1, max_conn=6)
    assert store.get_state(identity)["home_session_id"] == homes[0]["id"]
    assert store.get_sources(identity)[0]["watermark"] == {"seen_ids": ["one"]}
    assert produce(None) == []
    service.configure({"paused": True})
    assert store.admit_run(identity) is None
    store.settle_run(run["id"], "cancelled", "Owner paused")
    assert store.get_run(run["id"])["status"] == "cancelled"
