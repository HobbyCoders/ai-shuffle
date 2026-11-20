"""Owner CRUD, real prompt storage, and manual precedence."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fluid.codemode.evolution import Proposal, queue_pending

from app.api import auth, learned_guidance
from app.core import guidance_storage as store
from app.core import guidance_surface as surface

ROUTE = "/api/v1/learned-guidance"
MODEL = "fake/glm-5"


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv(surface.GUIDANCE_HOME_ENV, str(tmp_path))
    root = surface.guidance_directory()
    root.mkdir(parents=True)
    return root


@pytest.fixture
def client(monkeypatch, home):
    monkeypatch.setattr(auth.preview_access, "enforce_limits", lambda _req: None)
    monkeypatch.setattr(auth, "get_session_token", lambda _req: None)
    monkeypatch.setattr(auth, "get_api_key", lambda _req: None)
    monkeypatch.setattr(auth.owner_auth, "local_access", lambda request: request.headers.get("X-Owner") == "yes")
    app = FastAPI()
    app.include_router(learned_guidance.router)
    return TestClient(app)


OWNER = {"X-Owner": "yes", "Origin": "http://testserver"}


def test_owner_edit_and_delete_change_the_prompt_read_path(client, home):
    (home / "GLM .md").write_text("First guidance", encoding="utf-8")
    (home / "readme.txt").write_text("not guidance", encoding="utf-8")
    assert surface.prompt_guidance_blocks(MODEL) == ("First guidance",)
    [block] = client.get(ROUTE, headers=OWNER).json()["blocks"]
    assert (block["model"], block["scope"]) == ("glm", "model_substring")
    item_url = f"{ROUTE}/{block['id']}"
    original = block["revision"]
    for invalid in (" ", "x" * 4001, "Try to call NoSuchTool now."):
        assert client.put(item_url, headers=OWNER, json={
            "content": invalid, "expected_revision": original,
        }).status_code == 422
    assert surface.prompt_guidance_blocks(MODEL) == ("First guidance",)
    result = client.put(item_url, headers=OWNER, json={
        "content": "  Updated instructions  ", "expected_revision": original,
    })
    assert result.status_code == 200, result.text
    latest = result.json()
    assert client.get(ROUTE, headers=OWNER).json()["blocks"] == [latest]
    assert surface.prompt_guidance_blocks(MODEL) == ("Updated instructions",)
    assert surface.prompt_guidance_blocks("another-model") == ()
    assert client.delete(item_url, headers=OWNER, params={"expected_revision": original}).status_code == 409
    assert client.delete(item_url, headers=OWNER, params={"expected_revision": latest["revision"]}).status_code == 204
    assert client.get(ROUTE, headers=OWNER).json()["blocks"] == []
    assert surface.prompt_guidance_blocks(MODEL) == ()
    # An automatic proposal cannot use a case/whitespace alias to restore it.
    with pytest.raises(store.GuidanceConflict):
        store.apply_automatic_guidance("glm", "Do not resurrect", expected_revision=None)


def test_preview_non_owner_and_wrong_origin_are_denied(client, monkeypatch, home):
    (home / "glm.md").write_text("Private guidance", encoding="utf-8")
    [block] = client.get(ROUTE, headers=OWNER).json()["blocks"]
    url = f"{ROUTE}/{block['id']}"
    payload = {"content": "Not mine", "expected_revision": block["revision"]}
    assert client.get(ROUTE).status_code == 403
    assert client.put(url, headers={**OWNER, "Origin": "http://elsewhere.invalid"}, json=payload).status_code == 403
    monkeypatch.setattr(auth, "_preview_may_read", lambda _req: True)
    from types import SimpleNamespace
    monkeypatch.setattr(auth.preview_access, "grant", lambda _req: SimpleNamespace(session_id="preview-test"))
    assert client.get(ROUTE).status_code == 403
    assert client.delete(url, headers={"Origin": "http://testserver"}, params={
        "expected_revision": block["revision"],
    }).status_code == 403
    assert surface.prompt_guidance_blocks(MODEL) == ("Private guidance",)


def test_exact_scope_uses_same_managed_row_and_cas(client):
    initial = store.apply_automatic_guidance("behavioral-test", "Learned instruction", expected_revision=None,
                                             model="gpt-test", provider="ChatGPT")
    updated = store.apply_automatic_guidance(initial.stem, "New automatic instruction", expected_revision=initial.revision,
                                             model=initial.model, provider=initial.provider)
    with pytest.raises(store.GuidanceConflict):
        store.apply_automatic_guidance(initial.stem, "Stale instruction", expected_revision=initial.revision,
                                       model=initial.model, provider=initial.provider)
    [shown] = client.get(ROUTE, headers=OWNER).json()["blocks"]
    assert (shown["model"], shown["provider"], shown["scope"]) == ("gpt-test", "ChatGPT", "exact")
    assert surface.prompt_guidance_blocks(initial.stem) == ()
    url = f"{ROUTE}/{initial.id}"
    assert client.put(url, headers=OWNER, json={
        "content": "stale owner form", "expected_revision": initial.revision,
    }).status_code == 409
    edited = client.put(url, headers=OWNER, json={
        "content": "Owner instruction", "expected_revision": updated.revision,
    }).json()
    assert store.read_saved_guidance(initial.stem, scoped=True).content == "Owner instruction"
    with pytest.raises(store.GuidanceConflict):
        store.apply_automatic_guidance(initial.stem, "Automatic overwrite", expected_revision=edited["revision"],
                                       model=initial.model, provider=initial.provider)
    assert client.delete(url, headers=OWNER, params={"expected_revision": edited["revision"]}).status_code == 204
    assert store.read_saved_guidance(initial.stem, scoped=True) is None
    assert store.automatic_guidance_blocked(initial.stem, scoped=True)
    with pytest.raises(store.GuidanceConflict):
        store.apply_automatic_guidance(initial.stem, "Automatic resurrection", expected_revision=None,
                                       model=initial.model, provider=initial.provider)


@pytest.mark.asyncio
async def test_queued_replay_cannot_clobber_concurrent_manual_edit(monkeypatch, home, tmp_path):
    from fluid.codemode.anchors import ReplayOutcome

    skills = tmp_path / "skills"
    skills.mkdir()
    monkeypatch.setattr(surface, "_skills_root", lambda: skills)
    initial = store.apply_automatic_guidance("glm", "Old guidance", expected_revision=None)
    queue_pending(skills, Proposal(kind="guidance", target="glm", content="Queued guidance",
                                   motivation={"prompt": "list files", "checks": [{"kind": "tool_called", "name": "Glob"}]}))

    class Runner:
        async def replay(self, anchor, *, proposal=None):
            store.edit_saved_guidance(initial.id, initial.revision, "Manual guidance")
            return ReplayOutcome(status="pass")

    [result] = await surface.evaluate_pending(runner=Runner())
    assert not result["committed"] and result["stage"] == "validity"
    assert surface.evolution_log() == []
    assert surface.prompt_guidance_blocks(MODEL) == ("Manual guidance",)
