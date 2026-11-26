"""Settings > Memory proposal inbox routes (Memory v3 session reflection)."""
from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import auth, memories
from app.core import fluid_memory, owner_auth, preview_access
from app.db import sessions as sessions_db
from fluid.codemode import reflection
from fluid.codemode.skills import SkillStore
from fluid.codemode.transactions import suppress_changes

BASE = "/api/v1/memories/proposals"
pytestmark = pytest.mark.memory_reflection


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
    monkeypatch.setattr(sessions_db, "get_sessions_by_ids", lambda ids: {
        sid: {"id": sid, "title": f"Chat {sid}"} for sid in ids if sid != "ses_gone"
    })
    app = FastAPI()
    app.include_router(memories.router)
    with suppress_changes(), TestClient(app) as client:
        yield client, root


def propose(root, text="The project deploys with make release.", *, kind="project",
            scope="project:garden", session="ses_1", quote=None):
    root.mkdir(parents=True, exist_ok=True)
    return reflection.ProposalInbox(root).propose(kind=kind, text=text, scope=scope,
                                                  session=session, quote=quote,
                                                  when="When deploying the garden")


def test_empty_inbox_lists_nothing_and_creates_no_files(world):
    client, root = world
    body = client.get(BASE).json()
    assert body == {"items": [], "ttl_days": 14, "mode": "reflection"}
    assert not root.exists()


def test_list_shows_text_kind_quote_sessions_and_expiry(world):
    client, root = world
    row = propose(root, quote="use make release")
    propose(root, "The owner keeps notes at example.com/wiki.", kind="owner_fact",
            scope="global", session="ses_gone")
    items = client.get(BASE).json()["items"]
    assert [i["kind"] for i in items] == ["owner_fact", "project"]
    project = next(i for i in items if i["id"] == row["id"])
    assert project["kind_label"] == "Project knowledge"
    assert project["scope_label"] == "Garden" and project["quote"] == "use make release"
    assert project["sessions"] == [{"id": "ses_1", "title": "Chat ses_1"}]
    assert project["session_count"] == 1
    assert project["expires_at"] - project["created_at"] == pytest.approx(14 * 86400)
    owner = items[0]
    assert owner["scope_label"] == "All projects" and owner["sessions"] == [{"id": "ses_gone", "title": None}]


def test_accept_saves_an_agent_saved_fact_and_closes_the_proposal(world):
    client, root = world
    row = propose(root)
    response = client.post(f"{BASE}/{row['id']}/accept")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "accepted"
    assert body["memory"]["text"] == "The project deploys with make release."
    assert body["memory"]["when"] == "When deploying the garden"
    entry = next(e for e in SkillStore(root)._facts.snapshot()["entries"]
                 if e["id"] == body["memory"]["id"])
    assert entry.get("durable") and not entry.get("protected") and entry["scope"] == "project:garden"
    assert entry["provenance"] == "session"
    assert client.get(BASE).json()["items"] == []
    # Only the winning decision counts: a second accept or a late dismiss is 409.
    assert client.post(f"{BASE}/{row['id']}/accept").status_code == 409
    assert client.post(f"{BASE}/{row['id']}/dismiss").status_code == 409
    assert sum("make release" in e["text"] for e in SkillStore(root)._facts.snapshot()["entries"]) == 1


def test_an_accepted_proposal_with_a_quote_is_still_agent_saved(world):
    client, root = world
    row = propose(root, "The owner keeps release notes at example.com/blog.", kind="owner_fact",
                  scope="global", quote="I keep release notes at example.com/blog")
    body = client.post(f"{BASE}/{row['id']}/accept").json()
    entry = next(e for e in SkillStore(root)._facts.snapshot()["entries"]
                 if e["id"] == body["memory"]["id"])
    # The quote is the owner's words in a chat, not the owner saving a memory.
    assert entry["provenance"] == "session" and not entry.get("protected")


def test_accept_after_a_dismissal_on_another_worker_is_refused(world):
    client, root = world
    row = propose(root)
    inbox = reflection.ProposalInbox(root)
    inbox.apply_remote("dev-other", [], {row["id"]: {"status": "dismissed", "at": 1.0}})
    assert client.post(f"{BASE}/{row['id']}/accept").status_code == 409
    memories_file = root / "memories.json"
    assert not memories_file.exists() or "make release" not in memories_file.read_text()


def test_dismiss_saves_nothing(world):
    client, root = world
    row = propose(root)
    response = client.post(f"{BASE}/{row['id']}/dismiss")
    assert response.status_code == 200 and response.json()["status"] == "dismissed"
    assert client.get(BASE).json()["items"] == []
    memories_file = root / "memories.json"
    assert not memories_file.exists() or "make release" not in memories_file.read_text()
    stored = json.loads((root / reflection.PROPOSAL_FILE).read_text())
    assert stored["proposals"] == [] and stored["decisions"][row["id"]]["status"] == "dismissed"
    # A later chat proposing it again does not bring it back.
    propose(root, session="ses_2")
    assert client.get(BASE).json()["items"] == []


def test_unknown_or_malformed_ids_are_refused(world):
    client, _ = world
    assert client.post(f"{BASE}/pnothere/accept").status_code == 404
    assert client.post(f"{BASE}/{'x' * 41}/dismiss").status_code == 422


@pytest.mark.parametrize("method,path", [("get", ""), ("post", "/p1/accept"), ("post", "/p1/dismiss")])
def test_non_admin_refused(world, monkeypatch, method, path):
    client, _ = world
    monkeypatch.setattr(owner_auth, "local_access", lambda request: False)
    monkeypatch.setattr(preview_access, "grant", lambda request: None)
    assert getattr(client, method)(BASE + path).status_code == 403


def test_preview_cookie_cannot_decide(world, monkeypatch):
    client, root = world
    row = propose(root)
    grant = preview_access.Grant("test-preview", 9_999_999_999)
    monkeypatch.setattr(preview_access, "verify", lambda token: grant)
    client.cookies.set(preview_access.cookie_name(), "test-preview-cookie")
    assert client.get(BASE).status_code == 200
    assert client.post(f"{BASE}/{row['id']}/accept").status_code == 403
    assert client.get(BASE).json()["items"][0]["id"] == row["id"]
