"""Shared scaffolding for the end-to-end git / GitHub route tests.

* :func:`github_repo` builds a real repository whose ``origin`` *reads* as
  ``https://github.com/acme/app.git`` (so ``get_repo_from_remote`` resolves
  ``acme/app``) while pushes are rewritten to a local bare repository
  (``url.<bare>.pushInsteadOf``) and every https transport is disabled
  (``protocol.https.allow=never``). A push therefore lands on disk and a
  fetch/pull fails fast instead of reaching the network.
* :class:`FakeModel` replaces the provider adapter seam used by
  ``generate_with_claude`` so the real prompt building, fence stripping,
  JSON parsing and error mapping run against a scripted model.
* :func:`build_git_app` mounts the git, git-operations and GitHub routers in a
  bare FastAPI app with auth overridden.
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI

from tests.git_repo_helpers import commit_file, git, init_repo

GITHUB_URL = "https://github.com/acme/app.git"
REPO = "acme/app"


def github_repo(tmp_path: Path, *, feature: str | None = "feat/x", name: str = "repo") -> Path:
    """Repo on *feature* (one unpushed commit ahead of main) with a fake GitHub origin."""
    repo = init_repo(tmp_path / name, {"README.md": "hello\n"})
    bare = tmp_path / f"{name}-origin.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    git(repo, "remote", "add", "origin", GITHUB_URL)
    git(repo, "config", f"url.{bare}.pushInsteadOf", GITHUB_URL)
    git(repo, "config", "protocol.https.allow", "never")
    git(repo, "push", "-q", "-u", "origin", "main")
    git(repo, "remote", "set-head", "origin", "main")
    if feature:
        git(repo, "checkout", "-q", "-b", feature)
        commit_file(repo, "feature.txt", "feature\n", "feat: add feature file")
    return repo


def bare_of(repo: Path) -> Path:
    return repo.parent / f"{repo.name}-origin.git"


def remote_has_branch(repo: Path, branch: str) -> bool:
    out = git(bare_of(repo), "branch", "--list", branch)
    return bool(out.strip())


# =============================================================================
# Fake model runner
# =============================================================================


class FakeModel:
    """Scripted stand-in for the provider adapter behind ``generate_with_claude``.

    Each turn streams back the next entry of ``queue`` (or ``reply`` once the
    queue is empty) as one assistant text block followed by a clean
    ``ResultMessage``. Set ``connect_error`` to an exception to simulate an
    unavailable provider, ``result_error`` to a string for a failed turn, and
    ``config_error`` to make option building fail (no provider bound).
    """

    def __init__(self, reply: str = "feat: generated message"):
        self.reply = reply
        self.queue: list[str] = []
        self.prompts: list[str] = []
        self.connect_error: BaseException | None = None
        self.config_error: BaseException | None = None
        self.result_error: str | None = None
        self.disconnects = 0

    def install(self, monkeypatch) -> FakeModel:
        from app.api import generation
        from app.core import generation_options

        model = self

        class _Adapter:
            text = ""

            async def connect_with_sdk_options(self, options):
                if model.connect_error is not None:
                    raise model.connect_error

            async def send_query(self, prompt):
                model.prompts.append(prompt)
                self.text = model.queue.pop(0) if model.queue else model.reply

            async def receive_events(self):
                from app.core.provider.events import (
                    AssistantMessage,
                    ResultMessage,
                    TextBlock,
                )

                yield AssistantMessage(content=[TextBlock(text=self.text)], model="fake-model")
                if model.result_error:
                    yield ResultMessage(subtype="error", is_error=True, errors=[model.result_error])
                else:
                    yield ResultMessage(subtype="success", usage={"input_tokens": 1, "output_tokens": 1})

            async def disconnect(self):
                model.disconnects += 1

        def _build(config, **kwargs):
            if model.config_error is not None:
                raise model.config_error
            return SimpleNamespace(aih_provider_id="fake")

        monkeypatch.setattr(generation, "make_adapter", lambda: _Adapter())
        monkeypatch.setattr(generation_options, "build_generation_options", _build)
        monkeypatch.setattr(generation_options, "text_generation_config", lambda: {"model": "fake-model"})
        return self


# =============================================================================
# App
# =============================================================================


def build_git_app() -> FastAPI:
    from app.api import git as git_api
    from app.api import git_operations, github

    app = FastAPI()
    app.include_router(git_api.router)
    app.include_router(git_operations.router)
    app.include_router(github.router)
    for dep in {git_api.require_auth, git_operations.require_auth, github.require_auth}:
        app.dependency_overrides[dep] = lambda: "test-token"
    return app


def reset_git_state() -> None:
    """Drop every per-process cache these routes share."""
    from app.api import git_helpers, git_operations
    from app.core.git_cache import _git_cache
    from app.core.github_service import github_service

    git_operations._operations.clear()
    git_operations._invalidate_pr_state_cache()
    git_helpers.invalidate_working_dir_cache()
    github_service.invalidate_auth_cache()
    _git_cache.invalidate_prefix("")


def wait_op(client, project_id: str, op_id: str, timeout: float = 30.0) -> dict:
    """Poll the operation endpoint until it leaves ``running``."""
    deadline = time.monotonic() + timeout
    while True:
        response = client.get(f"/api/v1/projects/{project_id}/git/operations/{op_id}")
        assert response.status_code == 200, response.text
        data = response.json()
        if data["status"] != "running":
            return data
        if time.monotonic() > deadline:  # pragma: no cover - diagnostic
            raise AssertionError(f"operation still running: {data}")
        time.sleep(0.05)


def steps(snapshot: dict) -> dict:
    return {s["id"]: s for s in snapshot["steps"]}
