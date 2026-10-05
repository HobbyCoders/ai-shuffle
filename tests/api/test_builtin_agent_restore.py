"""Built-in subagents: is_modified detection and POST /agents/{id}/restore.

Built-in subagents (general-purpose, Plan, frontend-designer) live in
the unified agents table and compare against their code definitions in
BUILTIN_SUBAGENTS. The list endpoint and the restore endpoint both use that
subagent comparison — otherwise the revert button never appears.
"""

from app.core.builtin_subagents import (
    GENERAL_PURPOSE_PROMPT,
    get_builtin_subagent_definition,
    seed_builtin_subagents,
)
from app.db import database


def _general_purpose(client) -> dict:
    resp = client.get("/api/v1/agents")
    assert resp.status_code == 200
    rows = {p["id"]: p for p in resp.json()}
    assert "general-purpose" in rows
    return rows["general-purpose"]


def test_pristine_builtin_is_not_modified(authenticated_client):
    seed_builtin_subagents()
    assert _general_purpose(authenticated_client)["is_modified"] is False


def test_edited_builtin_reports_modified_and_restores(authenticated_client):
    seed_builtin_subagents()
    database.update_subagent(
        "general-purpose", prompt="custom prompt", description="custom description", model="opus",
    )

    profile = _general_purpose(authenticated_client)
    assert profile["is_modified"] is True

    resp = authenticated_client.post("/api/v1/agents/general-purpose/restore")
    assert resp.status_code == 200, resp.text
    assert resp.json()["is_modified"] is False

    restored = database.get_subagent("general-purpose")
    assert restored["prompt"] == GENERAL_PURPOSE_PROMPT
    assert restored["description"] == get_builtin_subagent_definition("general-purpose")["description"]
    assert restored["model"] is None
    assert _general_purpose(authenticated_client)["is_modified"] is False


def test_startup_keeps_saved_builtin_until_restore(authenticated_client):
    seed_builtin_subagents()
    database.update_subagent(
        "general-purpose", prompt="previous version prompt", description="previous version description",
    )

    seed_builtin_subagents()
    saved = database.get_subagent("general-purpose")
    assert saved["prompt"] == "previous version prompt"
    assert saved["description"] == "previous version description"

    resp = authenticated_client.post("/api/v1/agents/general-purpose/restore")
    assert resp.status_code == 200, resp.text
    restored = database.get_subagent("general-purpose")
    defaults = get_builtin_subagent_definition("general-purpose")
    assert restored["prompt"] == defaults["prompt"]
    assert restored["description"] == defaults["description"]


def test_restore_keeps_user_owned_extras(authenticated_client):
    seed_builtin_subagents()
    database.update_subagent("general-purpose", prompt="custom", skills=["unnslop"])
    assert _general_purpose(authenticated_client)["is_modified"] is True

    resp = authenticated_client.post("/api/v1/agents/general-purpose/restore")
    assert resp.status_code == 200, resp.text

    restored = database.get_subagent("general-purpose")
    assert restored["prompt"] == GENERAL_PURPOSE_PROMPT
    assert restored["skills"] == ["unnslop"]


def test_builtin_subagent_is_modified_is_pure():
    definition = {
        "id": "general-purpose",
        "is_builtin": True,
        "name": "General Purpose",
        "description": "x",
        "config": {"system_prompt": {"content": GENERAL_PURPOSE_PROMPT}},
    }
    # Description differs from the code definition.
    assert database.builtin_subagent_is_modified(definition) is True
    assert database.builtin_subagent_is_modified({"id": "custom", "is_builtin": False}) is None
    assert database.builtin_subagent_is_modified({"id": "nope", "is_builtin": True}) is None
