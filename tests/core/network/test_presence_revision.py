"""Running build revisions survive presence publication, sync and the members API."""
import json

import pytest

from app.core import file_logging
from app.db import network as store
from app.network import engine
from tests.core.network.harness import exchange, join
from tests.core.network.test_api import make_client


@pytest.mark.parametrize("metadata", ["selected-source", "image-env", "bundled-desktop", "container", "installer-desktop"])
def test_bare_version_revision_reaches_peer_api(workers, make_client, monkeypatch, tmp_path, metadata):
    a, b = workers("unraid"), workers("desktop")
    with a.active():
        engine.create_network("Home")
    join(a, b, name="DesktopWorker")
    monkeypatch.delenv("SHUFFLE_SOURCE_REVISION", raising=False)
    monkeypatch.delenv("GIT_SHA", raising=False)
    server = tmp_path / "bundle" / "server"
    server.mkdir(parents=True)
    monkeypatch.setattr(file_logging, "_APP_ROOT", server)
    revision = "04d9a0cfb83b43031dd3565930455afa3049b7a5"
    (server.parent / "build-source.json").write_text(json.dumps({
        "version": "0.9.1", "sourceCommit": revision if metadata == "installer-desktop" else "a" * 40,
        "buildUrl": "https://example.com/build",
    }))
    if metadata == "selected-source":
        monkeypatch.setenv("SHUFFLE_SOURCE_REVISION", revision)
        monkeypatch.setenv("GIT_SHA", "a" * 40)  # selected source wins
    elif metadata == "image-env":
        monkeypatch.setenv("GIT_SHA", revision)
    elif metadata == "bundled-desktop":
        (server.parent / "desktop-build.json").write_text(json.dumps({"revision": revision}))
        (server / "GIT_SHA").write_text("a" * 40)
    elif metadata == "container":
        (server / "GIT_SHA").write_text(revision)
    with b.active():
        engine.publish_presence(version="0.9.1")
        assert engine.presence_of(b.device_id)["source_revision"] == revision[:9]
    # A different local build must not be substituted for the peer's SHA.
    monkeypatch.setenv("SHUFFLE_SOURCE_REVISION", "b" * 40)
    result = exchange(b, a)
    assert result.rejected == 0
    with a.active():
        client = make_client()
        row = next(m for m in client.get("/api/v1/network/members").json()["members"]
                   if m["device_id"] == b.device_id)
        assert row["version"] == "0.9.1"
        assert row["revision"] == revision[:9]
        status = client.get("/api/v1/network/status").json()
        assert status["revision"] == "bbbbbbbbb"
        assert next(m for m in status["members"] if m["device_id"] == b.device_id)["revision"] == revision[:9]
    # Updating only the revision (same release version) must publish a new op.
    monkeypatch.setenv("SHUFFLE_SOURCE_REVISION", "c" * 40)
    with b.active():
        assert engine.publish_presence(version="0.9.1")
    assert exchange(b, a).rejected == 0
    with a.active():
        assert engine.presence_of(b.device_id)["source_revision"] == "ccccccccc"
    # A fresh worker also learns the last reported revision via bootstrap.
    c = workers("new-worker")
    join(a, c)
    with c.active():
        peer = next(m for m in make_client().get("/api/v1/network/members").json()["members"]
                    if m["device_id"] == b.device_id)
        assert peer["revision"] == "ccccccccc"
        assert peer["version"] == "0.9.1"
    with b.active():
        assert not engine.publish_presence(version="0.9.1")


@pytest.mark.parametrize("value", ["unknown", "not-a-sha", "", "HEAD"])
def test_unavailable_revision_is_null_not_checkout_head(workers, make_client, monkeypatch, tmp_path, value):
    monkeypatch.setenv("SHUFFLE_SOURCE_REVISION", value)
    monkeypatch.delenv("GIT_SHA", raising=False)
    monkeypatch.setattr(file_logging, "_APP_ROOT", tmp_path / "missing")
    a = workers("a")
    with a.active():
        client = make_client()
        assert client.get("/api/v1/network/status").json()["revision"] is None
        engine.create_network("Home")
        engine.publish_presence(version="0.9.1")
        assert engine.presence_of(a.device_id)["source_revision"] is None
        assert client.get("/api/v1/network/members").json()["members"][0]["revision"] is None


@pytest.mark.parametrize("content", [
    "{not json", "[]", "null", "{}", '{"sourceCommit": "not-a-sha"}',
])
def test_malformed_installer_stamp_publishes_no_revision(workers, make_client, monkeypatch, tmp_path, content):
    monkeypatch.delenv("SHUFFLE_SOURCE_REVISION", raising=False)
    monkeypatch.delenv("GIT_SHA", raising=False)
    server = tmp_path / "worker" / "server"
    server.mkdir(parents=True)
    monkeypatch.setattr(file_logging, "_APP_ROOT", server)
    (server.parent / "build-source.json").write_text(content)
    a = workers("linux")
    with a.active():
        engine.create_network("Home")
        engine.publish_presence(version="0.9.1")
        assert engine.presence_of(a.device_id)["source_revision"] is None
        client = make_client()
        assert client.get("/api/v1/network/status").json()["revision"] is None
        assert client.get("/api/v1/network/members").json()["members"][0]["revision"] is None


def test_pre_revision_presence_and_additive_schema(workers, make_client):
    a, b = workers("a"), workers("old-peer")
    with a.active():
        engine.create_network("Home")
    join(a, b)
    with b.active():
        # Upgrade an existing schema, not just a freshly created database.
        b.raw.execute("ALTER TABLE network_presence DROP COLUMN source_revision")
        store._ensure_columns(b.shim.cursor())
        store._ensure_columns(b.shim.cursor())
        engine.publish_presence(version="0.9.1")
        row = engine.presence_of(b.device_id)
        row.pop("source_revision")  # an older peer doesn't send the field
        with store.get_db() as conn:
            engine.record(conn.cursor(), "presence", b.device_id, row)
    assert exchange(b, a).rejected == 0
    with a.active():
        row = next(m for m in make_client().get("/api/v1/network/members").json()["members"]
                   if m["device_id"] == b.device_id)
        assert row["version"] == "0.9.1"
        assert row["revision"] is None


def test_peer_losing_revision_metadata_clears_stale_revision(workers, make_client, monkeypatch):
    a, b = workers("a"), workers("desktop")
    with a.active():
        engine.create_network("Home")
    join(a, b)
    monkeypatch.setenv("SHUFFLE_SOURCE_REVISION", "a" * 40)
    with b.active():
        engine.publish_presence(version="0.9.1")
    assert exchange(b, a).rejected == 0
    with a.active():
        assert engine.presence_of(b.device_id)["source_revision"] == "aaaaaaaaa"
    monkeypatch.setenv("SHUFFLE_SOURCE_REVISION", "unknown")
    with b.active():
        assert engine.publish_presence(version="0.9.1")
    assert exchange(b, a).rejected == 0
    with a.active():
        peer = next(m for m in make_client().get("/api/v1/network/members").json()["members"]
                    if m["device_id"] == b.device_id)
        assert peer["version"] == "0.9.1"
        assert peer["revision"] is None
