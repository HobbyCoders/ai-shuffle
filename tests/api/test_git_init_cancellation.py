"""A cancelled init request must not delete metadata while a Git thread writes it."""

import asyncio
import threading

import pytest
from starlette.requests import Request

from app.api import git as git_api
from app.core.git_service import git_service
from tests.git_repo_helpers import hermetic_git_env, write


@pytest.mark.asyncio
@pytest.mark.parametrize("settles", [True, False])
async def test_cancelled_init_waits_for_the_git_writer_or_keeps_metadata(tmp_path, monkeypatch, settles):
    hermetic_git_env(monkeypatch, tmp_path)
    target = tmp_path / "project"
    target.mkdir()
    write(target / "keep.txt", "keep me\n")
    started = asyncio.Event()
    release = threading.Event()
    finished = threading.Event()
    rollback_started = threading.Event()
    loop = asyncio.get_running_loop()
    init = git_service.init_repo
    rollback = git_service.rollback_init

    async def working_dir(*args, **kwargs):
        return str(target)

    def slow_init(*args, **kwargs):
        loop.call_soon_threadsafe(started.set)
        try:
            if not release.wait(10):
                raise RuntimeError("test did not release the Git writer")
            return init(*args, **kwargs)
        finally:
            finished.set()

    def rollback_after_writer(*args, **kwargs):
        rollback_started.set()
        assert finished.is_set(), "rollback raced an in-flight Git writer"
        return rollback(*args, **kwargs)

    monkeypatch.setattr(git_api, "check_project_access", lambda *args: None)
    monkeypatch.setattr(git_api, "get_working_dir_async", working_dir)
    monkeypatch.setattr(git_service, "is_git_repo", lambda *args: False)
    monkeypatch.setattr(git_service, "init_repo", slow_init)
    monkeypatch.setattr(git_service, "rollback_init", rollback_after_writer)
    if not settles:
        monkeypatch.setattr(git_api, "_GIT_INIT_CANCEL_WAIT_SECONDS", 0.01, raising=False)

    request = asyncio.create_task(git_api.init_repository.__wrapped__(
        Request({"type": "http", "headers": []}), "cancel-project",
        git_api.InitRepoRequest(create_initial_commit=False), token="test",
    ))
    await asyncio.wait_for(started.wait(), timeout=5)
    request.cancel()
    early_rollback = False
    try:
        if settles:
            early_rollback = await asyncio.to_thread(rollback_started.wait, 0.05)
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(request, timeout=2)
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 5)
    # Let the tracked operation consume its result after a bounded-timeout path.
    # This barrier is an executor task behind the writer, not a sleep/poll loop.
    await git_api.run_git(lambda: None)
    assert not early_rollback
    assert rollback_started.is_set() is settles
    assert (target / ".git").exists() is not settles
    assert (target / "keep.txt").read_text() == "keep me\n"
