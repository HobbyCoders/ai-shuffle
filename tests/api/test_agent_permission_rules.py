"""Owner-only saved permission rule management, using real DB helpers."""
from collections.abc import Iterator
from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.agents import router
from app.api.auth import require_admin
from app.db import database


@pytest.fixture
def rules_client() -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_admin] = lambda: "owner"
    with TestClient(app) as client:
        yield client


def test_list_and_delete_one(rules_client: TestClient) -> None:
    database.create_profile("rule-agent", "Rules", None, {})
    rule = database.add_permission_rule("rule-agent", "Bash", None, "deny")
    other = database.add_permission_rule("default", "Read", "/private/*", "allow")
    url = "/api/v1/agents/rule-agent/permission-rules"
    response = rules_client.get(url)
    assert response.status_code == 200
    saved = response.json()
    assert len(saved) == 1
    assert datetime.fromisoformat(saved[0].pop("created_at")) == (
        datetime.fromisoformat(rule.pop("created_at"))
    )
    assert saved == [rule]
    assert rules_client.delete(f"{url}/{other['id']}").status_code == 404
    assert database.get_permission_rule(other["id"]) is not None
    assert rules_client.delete(f"{url}/{rule['id']}").status_code == 204
    assert rules_client.get(url).json() == []
    assert rules_client.delete(f"{url}/{rule['id']}").status_code == 404


def test_delete_all_is_agent_scoped_and_idempotent(rules_client: TestClient) -> None:
    database.create_profile("rule-agent", "Rules", None, {})
    for tool in ("Bash", "Read"):
        database.add_permission_rule("rule-agent", tool, None, "allow")
    other = database.add_permission_rule("default", "Read", None, "deny")
    url = "/api/v1/agents/rule-agent/permission-rules"
    assert rules_client.delete(url).json() == {"deleted_count": 2}
    assert rules_client.delete(url).json() == {"deleted_count": 0}
    assert database.get_permission_rule(other["id"]) is not None


@pytest.mark.parametrize("method,suffix", [("get", ""), ("delete", ""), ("delete", "/rule-id")])
def test_missing_agent(rules_client: TestClient, method: str, suffix: str) -> None:
    response = getattr(rules_client, method)(f"/api/v1/agents/missing/permission-rules{suffix}")
    assert response.status_code == 404
    assert response.json() == {"detail": "Profile not found: missing"}


@pytest.mark.parametrize("method,suffix", [("get", ""), ("delete", ""), ("delete", "/rule-id")])
@pytest.mark.parametrize("principal", ["anonymous", "api-key", "paired"])
def test_owner_required(
    method: str, suffix: str, principal: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api import auth

    # A valid ordinary paired controller still lacks owner privileges.
    paired = {"id": "paired-controller", "account_owner_admin": False}
    monkeypatch.setattr(auth, "get_paired_client_from_token", lambda *a, **kw: (
        paired if principal == "paired" else None
    ))
    monkeypatch.setattr(auth.owner_auth, "owner_session", lambda *a: False)
    monkeypatch.setattr(auth.owner_auth, "local_access", lambda *a: False)
    app = FastAPI()
    app.include_router(router)
    headers = {} if principal == "anonymous" else {"X-API-Key": f"test-{principal}"}
    with TestClient(app, client=("203.0.113.1", 50000)) as client:
        response = getattr(client, method)(
            f"/api/v1/agents/default/permission-rules{suffix}", headers=headers,
        )
    assert response.status_code == 403
    assert response.json() == {"detail": "Owner access required"}
