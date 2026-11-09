"""Owner saved-memory routes against real file transactions; no live worker data."""
from __future__ import annotations

import json
import threading
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import auth, memories
from app.core import fluid_memory, owner_auth, preview_access
from fluid.codemode import facts
from fluid.codemode.skills import MAX_MEMORY_CHARS, SkillStore
from fluid.codemode.transactions import suppress_changes

BASE = "/api/v1/memories"


@pytest.fixture
def world(tmp_path, monkeypatch):
    root = tmp_path / "owner-facts"
    monkeypatch.setattr(fluid_memory, "skills_root", lambda: root)
    monkeypatch.setattr(owner_auth, "local_access", lambda request: True)
    monkeypatch.setattr(owner_auth, "same_origin", lambda request: None)
    monkeypatch.setattr(auth, "get_paired_client_from_token", lambda *a, **k: None)
    monkeypatch.setattr(owner_auth, "owner_session", lambda *a: False)
    monkeypatch.setattr(memories, "get_projects_by_ids", lambda ids: {
        pid: {"name": "Garden"} for pid in ids if pid == "garden"
    })
    app = FastAPI()
    app.include_router(memories.router)
    with suppress_changes(), TestClient(app) as client:
        yield client, root


def store_at(root):
    return SkillStore(root)


def save(store, text="The owner prefers concise status reports.", **kwargs):
    return store.save_memory(text, provenance="user", protected=True, **kwargs)


def edit(client, entry, **overrides):
    body = {"text": entry.text, "when": None, "expected_revision": entry.revision}
    body.update(overrides)
    return client.put(f"{BASE}/{entry.id}", json=body)


def contents(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_missing_store_is_empty_without_creating_any_files(world):
    client, root = world
    response = client.get(BASE)
    assert response.status_code == 200
    assert response.json() == {"items": [], "limits": {
        "text_chars": MAX_MEMORY_CHARS, "when_chars": facts.MAX_WHEN_CHARS,
    }}
    assert not root.exists()


def test_list_shape_tiers_archive_worker_visibility_and_read_only(world):
    client, root = world
    store = store_at(root)
    user = save(store, when="When writing updates", always=True)
    agent = store.save_memory("Use the garden's build helper.", durable=True, scope="project:garden")
    learned = store.save_memory("The greenhouse has three shelves.", scope="git:opaque-hash", provenance="tool")
    local = store.save_memory("The local compiler is available.", per_worker=True, provenance="tool")
    archive = store.save_memory("The old toolchain is retired.", durable=True)
    assert store.retire_memory(archive.id, reason="test", expected_revision=archive.revision)
    control = save(store, "The owner accepted two coexisting rules.", key="memory.coexist.answer")
    remote = store.save_memory("The other worker uses another compiler.", per_worker=True)
    doc = store._facts.snapshot()
    for entry in doc["entries"]:
        if entry["id"] == remote.id:
            entry["worker"] = "another-worker"
        if entry["id"] == learned.id:
            entry.update(created_at=0, updated_at=0, last_recalled_at=0)
        if entry["id"] == user.id:
            entry.update(conflicted=True, updated_at=9_999_999_999)
    store._save_memories(doc)
    before = contents(root)
    response = client.get(BASE)
    assert response.status_code == 200
    assert contents(root) == before
    items = response.json()["items"]
    by_id = {item["id"]: item for item in items}
    assert control.id not in by_id and remote.id not in by_id
    assert items[0]["id"] == user.id
    assert by_id[user.id] == {
        "id": user.id, "revision": user.revision, "text": user.text,
        "when": "When writing updates", "status": "active", "tier": "user",
        "source": "user", "scope": "global", "scope_label": "All projects",
        "always": True, "needs_review": True, "this_worker_only": False,
        "created_at": user.created_at, "updated_at": 9_999_999_999,
        "last_recalled_at": None, "expires_at": None, "uses": 0,
    }
    assert by_id[agent.id]["tier"] == "agent"
    assert by_id[agent.id]["scope_label"] == "Garden"
    assert by_id[learned.id]["tier"] == "learned"
    assert by_id[learned.id]["source"] == "tool"
    assert by_id[learned.id]["when"] is None  # no derived line
    assert by_id[learned.id]["scope_label"] == "Other project"
    assert by_id[learned.id]["created_at"] is None
    assert by_id[archive.id]["status"] == "archived"
    assert by_id[local.id]["this_worker_only"] is True
    assert by_id[agent.id]["expires_at"] > 0
    assert all(set(item) == set(by_id[user.id]) for item in items)


def test_read_legacy_store_and_archive_without_migrating_or_curation(world):
    client, root = world
    root.mkdir()
    legacy = root / ".archive" / "memories-archive.jsonl"
    legacy.parent.mkdir()
    # Expired live rows remain observable; listing must not curate them.
    (root / "memories.json").write_text(json.dumps({"entries": [{
        "id": "m1", "text": "A legacy tool observation.", "protected": True,
        "provenance": "tool", "expires_at": 1,
    }]}))
    legacy.write_text(json.dumps({"id": "m2", "text": "A legacy archive."}) + "\n")
    before = contents(root)
    items = client.get(BASE).json()["items"]
    assert {item["id"] for item in items} == {"m1", "m2"}
    assert next(item for item in items if item["id"] == "m1")["tier"] == "agent"
    assert contents(root) == before


@pytest.mark.parametrize("hidden", ["remote", "control", "unknown"])
@pytest.mark.parametrize("method", ["put", "delete"])
def test_hidden_and_unknown_ids_are_refused(world, hidden, method):
    client, root = world
    store = store_at(root)
    entry = save(store)
    doc = store._facts.snapshot()
    if hidden == "control":
        doc["entries"][0]["key"] = "memory.coexist.internal"
    elif hidden == "remote":
        doc["entries"][0]["worker"] = "other-worker"
    store._save_memories(doc)
    before = store.memories_path.read_bytes()
    memory_id = "missing" if hidden == "unknown" else entry.id
    if method == "put":
        response = client.put(f"{BASE}/{memory_id}", json={
            "text": "An owner correction.", "when": None, "expected_revision": 1,
        })
    else:
        response = client.delete(f"{BASE}/{memory_id}?expected_revision=1")
    assert response.status_code == 404
    assert isinstance(response.json()["detail"], str)
    assert store.memories_path.read_bytes() == before


def test_edit_promotes_shares_and_bumps_revision(world):
    client, root = world
    store = store_at(root)
    entry = store.save_memory("The worker has a local build helper.", per_worker=True,
                              when="When building", provenance="tool", durable=True)
    response = edit(client, entry, text="The owner wants the shared build helper.")
    assert response.status_code == 200
    item = response.json()
    assert item["tier"] == "user" and item["source"] == "user"
    assert item["this_worker_only"] is False and item["expires_at"] is None
    assert item["revision"] == entry.revision + 1
    assert item["when"] == entry.when  # null preserves the old written line
    assert client.get(BASE).json()["items"] == [item]
    assert edit(client, entry, text="A stale correction.").status_code == 409
    assert client.delete(f"{BASE}/{entry.id}?expected_revision={entry.revision}").status_code == 409


@pytest.mark.parametrize("archived", [False, True])
def test_when_only_edit_at_explicit_cap_and_archive_reactivation(world, monkeypatch, archived):
    client, root = world
    monkeypatch.setattr(facts, "MAX_EXPLICIT", 1)
    store = store_at(root)
    entry = save(store, when="When planning")
    if archived:
        doc = store._facts.snapshot()
        doc["archived"], doc["entries"] = doc["entries"], []
        store._save_memories(doc)
    response = edit(client, entry, when="When writing a progress report")
    assert response.status_code == 200
    item = response.json()
    assert item["when"] == "When writing a progress report"
    assert item["revision"] == entry.revision  # metadata has its own replica stamp
    assert item["updated_at"] == entry.updated_at
    assert item["status"] == "active"
    assert not store._facts.snapshot(read_only=True)["archived"]
    corrected = edit(client, entry, text="The owner prefers a clear progress report.")
    assert corrected.status_code == 200
    assert corrected.json()["revision"] == entry.revision + 1
    inferred = store.save_memory("Another learned preference.")
    refused = edit(client, inferred)
    assert refused.status_code == 409 and "full" in refused.json()["detail"]


def test_identical_owner_edit_claims_provenance_and_clears_worker_tag(world):
    client, root = world
    store = store_at(root)
    entry = save(store)
    doc = store._facts.snapshot()
    doc["entries"][0].update(provenance="session", worker=doc["store_id"])
    store._save_memories(doc)
    response = edit(client, entry)
    assert response.status_code == 200
    assert response.json()["source"] == "user"
    assert response.json()["this_worker_only"] is False
    assert response.json()["revision"] == entry.revision + 1


@pytest.mark.parametrize("override", [
    {"text": "   "}, {"text": "x" * (MAX_MEMORY_CHARS + 1)},
    {"text": "api_key=" + "example" * 6},
    {"when": "w" * (facts.MAX_WHEN_CHARS + 1)}, {"when": "   "},
    {"when": "api_key=" + "example" * 6},
    {"text": 123}, {"expected_revision": 0}, {"expected_revision": True},
    {"scope": "global"},
])
def test_validation_is_plain_and_never_echoes_input(world, override):
    client, root = world
    store = store_at(root)
    entry = save(store)
    response = edit(client, entry, **override)
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert isinstance(detail, str) and "api_key=" not in detail
    assert store.memories()[0].text == entry.text


@pytest.mark.parametrize("query", ["", "?expected_revision=0", "?expected_revision=abc"])
def test_delete_requires_positive_revision(world, query):
    client, root = world
    entry = save(store_at(root))
    response = client.delete(f"{BASE}/{entry.id}{query}")
    assert response.status_code == 422 and isinstance(response.json()["detail"], str)


def test_delete_explicitly_purges_history_and_journal_and_blocks_re_save(world):
    client, root = world
    store = store_at(root)
    entry = save(store)
    revised = store.save_memory("The owner now prefers detailed reports.", memory_id=entry.id,
                                expected_revision=entry.revision, protected=True, provenance="user")
    store._journal_path.write_text(json.dumps({"text": entry.text}) + "\n")
    response = client.delete(f"{BASE}/{entry.id}?expected_revision={revised.revision}")
    assert response.status_code == 204 and response.content == b""
    doc = store._facts.snapshot(read_only=True)
    assert doc["entries"] == doc["archived"] == []
    assert doc["forgotten_before"] > 0
    assert entry.text not in store.memories_path.read_text()
    assert revised.text not in store.memories_path.read_text()
    assert entry.text not in store._journal_path.read_text()
    for text in (entry.text, revised.text):
        with pytest.raises(ValueError, match="deletion barrier|forgotten"):
            save(store, text)
    survivor = save(store, "The owner prefers weekly summaries.")
    conflict = edit(client, survivor, text=entry.text)
    assert conflict.status_code == 409 and "deleted" in conflict.json()["detail"]
    assert client.get(BASE).json()["items"][0]["text"] == survivor.text


@pytest.mark.parametrize("method", ["get", "put", "delete"])
def test_non_admin_refused(world, monkeypatch, method):
    client, _ = world
    monkeypatch.setattr(owner_auth, "local_access", lambda request: False)
    monkeypatch.setattr(preview_access, "grant", lambda request: None)
    response = getattr(client, method)(BASE if method == "get" else f"{BASE}/m1",
                                       **({"json": {"text": "change", "when": None,
                                                    "expected_revision": 1}} if method == "put" else {}))
    assert response.status_code == 403


def test_preview_cookie_is_read_only_even_with_independent_owner_access(world, monkeypatch):
    client, root = world
    entry = save(store_at(root))
    grant = preview_access.Grant("test-preview", 9_999_999_999)
    monkeypatch.setattr(preview_access, "verify", lambda token: grant)
    client.cookies.set(preview_access.cookie_name(), "test-preview-cookie")
    assert client.get(BASE).status_code == 200
    assert edit(client, entry).status_code == 403
    assert client.delete(f"{BASE}/{entry.id}?expected_revision=1").status_code == 403
    # The cookie alone may read owner routes too, but never write them.
    monkeypatch.setattr(owner_auth, "local_access", lambda request: False)
    monkeypatch.setattr(preview_access, "grant", lambda request: grant)
    assert client.get(BASE).status_code == 200
    assert edit(client, entry).status_code == 403


@pytest.mark.parametrize("method", ["get", "put", "delete"])
def test_storage_failures_are_503_logged_without_fact_text(world, monkeypatch, caplog, method):
    client, root = world
    entry = save(store_at(root))
    marker = "Private fact text must never enter diagnostics"
    monkeypatch.setattr(facts.FactStore, "snapshot", Mock(side_effect=TimeoutError(marker)))
    if method == "get":
        response = client.get(BASE)
    elif method == "put":
        response = edit(client, entry)
    else:
        response = client.delete(f"{BASE}/{entry.id}?expected_revision=1")
    assert response.status_code == 503
    assert marker not in response.text and marker not in caplog.text
    assert "Memory storage operation failed (TimeoutError)" in caplog.text


def test_corrupt_store_is_not_renamed_or_exposed_by_list(world, caplog):
    client, root = world
    root.mkdir()
    payload = b'{"entries": ["A private broken document"'
    (root / "memories.json").write_bytes(payload)
    before = contents(root)
    response = client.get(BASE)
    assert response.status_code == 503
    assert "private broken document" not in response.text + caplog.text
    assert contents(root) == before


def test_file_work_runs_off_the_event_loop(world, monkeypatch):
    client, root = world
    entry = save(store_at(root))
    event_threads = []
    file_threads = []
    run_sync = memories.run_sync
    snapshot = facts.FactStore.snapshot

    async def observed_run(operation, *args):
        event_threads.append(threading.get_ident())
        return await run_sync(operation, *args)

    def observed_snapshot(self, **kwargs):
        file_threads.append(threading.get_ident())
        return snapshot(self, **kwargs)

    monkeypatch.setattr(memories, "run_sync", observed_run)
    monkeypatch.setattr(facts.FactStore, "snapshot", observed_snapshot)
    assert client.get(BASE).status_code == 200
    response = edit(client, entry, text="The owner prefers clear reports.")
    assert response.status_code == 200
    assert client.delete(f"{BASE}/{entry.id}?expected_revision={response.json()['revision']}").status_code == 204
    assert event_threads and file_threads and set(event_threads).isdisjoint(file_threads)
