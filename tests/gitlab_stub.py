"""A fake GitLab v4 API for tests: answers ``httpx.get`` / ``httpx.request``
inside ``app.core.gitlab_service`` without any network."""

from __future__ import annotations

import json as _json

import httpx

from app.core import gitlab_service as gl

HOST = "https://gitlab.example.com"
TOKEN = "glpat-test-token-0001"


class FakeGitLab:
    def __init__(self, monkeypatch, host: str = HOST):
        self.host = host
        # Other servers this fake also answers for (a reconnect to a second
        # server); one shared route table, which is enough for these tests.
        self.extra_hosts: set[str] = set()
        self.calls: list[tuple[str, str, dict, dict | None]] = []
        self.routes: dict[tuple[str, str], tuple[int, object]] = {}
        self.valid_tokens = {TOKEN}
        self.unreachable = False
        monkeypatch.setattr(gl.httpx, "get", self._get)
        monkeypatch.setattr(gl.httpx, "request", self._request)

    def route(self, method: str, path: str, status: int, body: object) -> None:
        self.routes[(method, path)] = (status, body)

    def paths(self) -> list[tuple[str, str]]:
        return [(m, p) for m, p, _q, _b in self.calls]

    def _answer(self, method, url, headers, params=None, json=None):
        if self.unreachable:
            raise httpx.ConnectError("offline")
        api = next(
            (f"{h}/api/v4" for h in (self.host, *self.extra_hosts) if url.startswith(f"{h}/api/v4")), None
        )
        assert api, url
        path = url[len(api):]
        self.calls.append((method, path, dict(params or {}), json))
        if headers.get("PRIVATE-TOKEN") not in self.valid_tokens:
            return httpx.Response(401, json={"message": "401 Unauthorized"})
        if (method, path) == ("GET", "/user"):
            return httpx.Response(200, json={"username": "octo"})
        status, body = self.routes.get((method, path), (404, {"message": "404 Not Found"}))
        return httpx.Response(status, json=body)

    def _get(self, url, headers=None, timeout=None):  # noqa: ARG002
        return self._answer("GET", url, headers or {})

    def _request(self, method, url, params=None, json=None, headers=None, timeout=None):  # noqa: ARG002
        return self._answer(method, url, headers or {}, params, json)


def save_account(service, host: str = HOST, token: str = TOKEN, user: str = "octo") -> None:
    """Sign *service* in without touching git config (route tests)."""
    service.account_file.parent.mkdir(parents=True, exist_ok=True)
    service.account_file.write_text(_json.dumps({"host": host, "token": token, "user": user}), encoding="utf-8")


def mr(iid: int = 7, state: str = "opened", host: str = HOST, repo: str = "acme/app", **extra) -> dict:
    base = {
        "iid": iid, "title": "Add feature", "description": "", "state": state,
        "source_branch": "feat/x", "target_branch": "main", "author": {"username": "octo"},
        "web_url": f"{host}/{repo}/-/merge_requests/{iid}", "created_at": "c", "updated_at": "u",
        "sha": "a" * 40, "source_project_id": 1, "target_project_id": 1,
        "detailed_merge_status": "mergeable",
    }
    base.update(extra)
    return base
