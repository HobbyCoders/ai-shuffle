"""Preview lease teardown owns real subprocesses, never another session's app."""
import os
import subprocess
import sys
import threading
from pathlib import Path
from unittest.mock import Mock

import psutil
import pytest

from app.preview_run import (
    disposable_preview_data, initialize_preview_access, main, preview_environment,
    seed_dev_instance, supervise,
)

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux preview ownership")


def test_preview_environment_never_inherits_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", "/not-owned")
    monkeypatch.setenv("DATABASE_URL", "do-not-inherit")
    monkeypatch.setenv("AI_SHUFFLE_ENV_FILE", "/not-owned")
    env = preview_environment(tmp_path, 18880, 18881)
    assert "DATABASE_URL" not in env
    assert env["DATA_DIR"] == str(tmp_path / "data")
    assert not Path(env["DATA_DIR"]).exists()
    assert (tmp_path / ".branch-preview").read_text() == env["DATA_DIR"]
    assert Path(env["AI_SHUFFLE_ENV_FILE"]).read_text() == ""
    assert env["HOME"] == str(tmp_path / "home")
    assert env["LOCAL_MODE"] == "true"
    assert env["AI_SHUFFLE_PREVIEW"] == "1"
    assert env["HOST"] == "127.0.0.1"
    assert env["SHUFFLE_PREVIEW_PORT"] == "18880"
    assert env["PORT"] not in ("18880", "18881", "8000", "8001")


@pytest.mark.parametrize("host", ["127.0.0.1", "0.0.0.0", "10.0.0.30"])
def test_preview_bind_host_only_moves_guarded_listener(tmp_path: Path, host: str) -> None:
    env = preview_environment(tmp_path, 18880, 18881, host=host)
    assert env["HOST"] == "127.0.0.1"
    assert env["SHUFFLE_PREVIEW_HOST"] == host
    assert env["SHUFFLE_PREVIEW_PORT"] == "18880"


@pytest.mark.parametrize("reason", ["ttl", "stop", "error", "owner_exit", "pid_reused"])
def test_lease_closes_owned_database_and_worker(tmp_path, reason, monkeypatch):
    record = tmp_path / "pid"
    env = preview_environment(tmp_path, 18880, 18881)
    database = Mock()
    stopped = threading.Event()
    worker = ("import os,time,pathlib; "
              f"pathlib.Path({str(record)!r}).write_text(str(os.getpid())); time.sleep(60)")
    if reason == "stop":
        timer = threading.Timer(.5, stopped.set)
        timer.start()
    if reason in ("owner_exit", "pid_reused"):
        identities = iter(["original", None if reason == "owner_exit" else "reused"])
        monkeypatch.setattr("app.preview_run.process_identity", lambda pid: next(identities))
    if reason == "error":
        database.publish_descriptor.side_effect = RuntimeError("fixture failure")
        with pytest.raises(RuntimeError, match="fixture failure"):
            supervise([sys.executable, "-c", worker], env, database,
                      ttl=.5, owner_pid=os.getpid(), stopped=stopped)
    else:
        supervise([sys.executable, "-c", worker], env, database,
                  ttl=.5 if reason == "ttl" else 30, owner_pid=os.getpid(), stopped=stopped)
        assert record.is_file()
        assert not psutil.pid_exists(int(record.read_text()))
    database.close.assert_called_once()


@pytest.mark.parametrize("ttl,owner", [(0, 2), (14401, 2), (60, 1)])
def test_invalid_lease_never_starts_database(ttl, owner):
    database = Mock()
    with pytest.raises(ValueError):
        supervise([], {}, database, ttl=ttl, owner_pid=owner)
    database.open.assert_not_called()


def test_preview_teardown_does_not_kill_another_session(tmp_path):
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                             start_new_session=True)
    try:
        database = Mock()
        supervise([sys.executable, "-c", "import time; time.sleep(60)"],
                  preview_environment(tmp_path, 18880, 18881), database,
                  ttl=.2, owner_pid=os.getpid())
        database.close.assert_called_once()
        assert other.poll() is None
    finally:
        other.terminate()
        other.wait(timeout=5)


@pytest.mark.parametrize("options,host,browser_host", [
    ([], "127.0.0.1", "127.0.0.1"),
    (["--host", "0.0.0.0", "--browser-host", "10.0.0.30"], "0.0.0.0", "10.0.0.30"),
    (["--host", "10.0.0.30"], "10.0.0.30", "10.0.0.30"),
])
def test_cli_passes_preview_host_and_lease(
    monkeypatch: pytest.MonkeyPatch, options: list[str], host: str, browser_host: str,
) -> None:
    from docker import container_runtime

    database = Mock()
    supervise_mock = Mock(return_value=0)
    monkeypatch.setattr(container_runtime, "Database", database)
    monkeypatch.setattr("app.preview_run.supervise", supervise_mock)
    assert main(["--port", "18880", "--ttl-seconds", "60", *options]) == 0
    env = supervise_mock.call_args.args[1]
    assert env["HOST"] == "127.0.0.1"
    assert env["SHUFFLE_PREVIEW_PORT"] == "18880"
    assert env["SHUFFLE_PREVIEW_HOST"] == host
    assert env["SHUFFLE_PREVIEW_ACCESS_HOST"] == browser_host
    assert env["SHUFFLE_PREVIEW_ACCESS_TTL"] == "60"
    assert supervise_mock.call_args.kwargs["ttl"] == 60
    ports = {int(env[key]) for key in ("PORT", "SHUFFLE_PROTECTED_PORT", "SHUFFLE_PREVIEW_PORT")}
    ports.add(database.call_args.kwargs["port"])
    assert len(ports) == 4 and not ports.intersection({8000, 8001})


@pytest.mark.parametrize("options", [
    ["--host", "0.0.0.0"], ["--host", "::"], ["--host", "not-an-address"],
    ["--browser-host", "0.0.0.0"], ["--port", "8000"], ["--ttl-seconds", "14401"],
    ["--protected-port", "18880"],
])
def test_cli_rejects_invalid_preview_before_starting(
    monkeypatch: pytest.MonkeyPatch, options: list[str],
) -> None:
    supervise_mock = Mock()
    monkeypatch.setattr("app.preview_run.supervise", supervise_mock)
    with pytest.raises(SystemExit) as error:
        main(["--port", "18880", *options])
    assert error.value.code == 2
    supervise_mock.assert_not_called()


@pytest.mark.parametrize("preview,host", [("0", "10.0.0.30"), ("1", "")])
def test_access_bootstrap_is_opt_in(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], preview: str, host: str,
) -> None:
    monkeypatch.setenv("AI_SHUFFLE_PREVIEW", preview)
    monkeypatch.setenv("SHUFFLE_PREVIEW_ACCESS_HOST", host)
    initialize_preview_access()
    assert capsys.readouterr().out == ""


def test_access_bootstrap_requires_guarded_listener(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core import preview_access

    monkeypatch.setenv("AI_SHUFFLE_PREVIEW", "1")
    monkeypatch.setenv("SHUFFLE_PREVIEW_ACCESS_HOST", "10.0.0.30")
    monkeypatch.setattr(preview_access, "_listener_port", None)
    with pytest.raises(RuntimeError, match="token-only listener"):
        initialize_preview_access()


def test_bootstrap_link_uses_real_session_and_normal_guard(
    bare_db: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from urllib.parse import parse_qs, urlsplit
    from fastapi import Depends, FastAPI
    from fastapi.testclient import TestClient
    from app.api import auth
    from app.core import preview_access
    from app.core.config import settings
    from app.db.sessions import get_session, delete_session
    from app.serve import IngressApplication

    env = preview_environment(tmp_path, 18880, 18881)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(settings, "data_dir", Path(env["DATA_DIR"]))
    monkeypatch.setattr(type(settings), "managed_database_available", lambda self: True)
    monkeypatch.setattr(preview_access, "_listener_port", 18880)
    monkeypatch.setenv("AI_SHUFFLE_PREVIEW", "1")
    monkeypatch.setenv("SHUFFLE_PREVIEW_ACCESS_HOST", "10.0.0.30")
    monkeypatch.setenv("SHUFFLE_PREVIEW_ACCESS_TTL", "60")
    from app.core import encryption
    from app.db import database

    monkeypatch.setattr(encryption, "_encryption_key", None)
    monkeypatch.setattr(encryption, "_credential_status", {})
    assert encryption.initialize_server_key(database)
    initialize_preview_access()
    url = capsys.readouterr().out.strip().split()[-1]
    parts = urlsplit(url)
    token = parse_qs(parts.query)["token"][0]
    grant = preview_access.verify(token)
    assert grant is not None
    row = get_session(grant.session_id)
    assert row["hidden"] and row["api_user_id"] is None
    assert parts.netloc == "10.0.0.30:18880"

    app = FastAPI()
    app.include_router(auth.router)

    @app.get("/")
    @app.post("/api/v1/settings/test")
    async def guarded(_: str = Depends(auth.require_auth)) -> dict[str, bool]:
        return {"ok": True}

    client = TestClient(IngressApplication(app, 18882, 18881, 18880),
                        base_url="http://10.0.0.30:18880", client=("10.0.0.20", 54321))
    assert client.get("/").status_code == 403
    assert client.get(parts.path + "?token=forged").status_code == 403
    assert not client.cookies
    assert client.get(parts.path + "?" + parts.query).json() == {"ok": True}
    assert client.get("/").json() == {"ok": True}
    status = client.get("/api/v1/auth/status").json()
    assert status["preview_access"] is True
    assert status["setup_required"] is False
    assert status["onboarding_complete"] is True
    assert client.post("/api/v1/auth/setup").status_code == 403
    assert client.post("/api/v1/auth/onboarding/complete").status_code == 403
    assert client.post("/api/v1/settings/test").status_code == 403
    assert preview_access.verify(token, now=grant.expires_at + 1) is None
    delete_session(grant.session_id)
    assert client.get("/").status_code == 403

@pytest.mark.parametrize("preview,disposable", [("0", True), ("1", False)])
def test_setup_bootstrap_requires_preview_and_disposable_data(
    bare_db: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], preview: str, disposable: bool,
) -> None:
    from app.core import preview_access
    from app.core.auth import auth_service
    from app.core.config import settings
    from app.db import database

    env = preview_environment(tmp_path, 18880, 18881)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("AI_SHUFFLE_PREVIEW", preview)
    data = Path(env["DATA_DIR"])
    if not disposable:
        (data.parent / ".branch-preview").unlink()
    monkeypatch.setattr(settings, "data_dir", data)
    monkeypatch.setattr(type(settings), "managed_database_available", lambda self: True)
    monkeypatch.setattr(preview_access, "_listener_port", 18880)

    initialize_preview_access()

    assert auth_service.is_setup_required() is True
    assert auth_service.is_onboarding_complete() is False
    assert database.get_system_setting("installation_configured") is None
    assert database.get_system_setting("onboarding_complete") is None
    assert database.get_system_setting(f"workspace_path:{settings.effective_instance_id}") is None
    if preview == "0":
        assert capsys.readouterr().out == ""



# ─── dev loop (scripts/devloop) ─────────────────────────────────────────────


def test_dev_environment_never_inherits_credentials_or_dev_settings(tmp_path, monkeypatch):
    for key, value in {"DATA_DIR": "/not-owned", "DATABASE_URL": "postgres://live",
                       "OPENAI_API_KEY": "sk-live", "ANTHROPIC_API_KEY": "sk-ant-live",
                       "SENTRY_DSN": "https://live", "SHUFFLE_DEV_SEED": "inherited",
                       "AI_SHUFFLE_DEV_API": "http://localhost:8000"}.items():
        monkeypatch.setenv(key, value)
    env = preview_environment(tmp_path, 18880, 18881,
                              extra={"SHUFFLE_DEV_FAKE_AI_URL": "http://127.0.0.1:9"})
    assert env["DATA_DIR"] == str(tmp_path / "data")
    assert env["SHUFFLE_DEV_FAKE_AI_URL"] == "http://127.0.0.1:9"
    for key in ("DATABASE_URL", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "SENTRY_DSN",
                "SHUFFLE_DEV_SEED", "AI_SHUFFLE_DEV_API"):
        assert key not in env
    assert not {value for value in env.values()} & {"sk-live", "sk-ant-live", "postgres://live"}


@pytest.mark.parametrize("key", ["DATA_DIR", "HOME", "DATABASE_URL", "OPENAI_API_KEY",
                                 "SHUFFLE_DEV_", "shuffle_dev_seed"])
def test_dev_environment_extras_cannot_replace_isolation(tmp_path, key):
    with pytest.raises(ValueError, match="SHUFFLE_DEV_"):
        preview_environment(tmp_path, 18880, 18881, extra={key: "x"})


def _starts_worker(record: Path, *, crash_first: bool = False) -> list[str]:
    code = (f"import os,sys,time,pathlib; p=pathlib.Path({str(record)!r}); "
            "n=len(p.read_text().split()) if p.exists() else 0; "
            "p.open('a').write(str(os.getpid())+' '); "
            f"sys.exit(1) if {crash_first!r} and n == 0 else time.sleep(60)")
    return [sys.executable, "-c", code]


def _pids(record: Path) -> list[int]:
    return [int(pid) for pid in record.read_text().split()] if record.exists() else []


def _wait_for(predicate, timeout: float = 10.0) -> None:
    import time

    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.05)


def test_restart_replaces_only_the_app_process(tmp_path):
    record = tmp_path / "pids"
    env = preview_environment(tmp_path, 18880, 18881)
    database, stopped, restart = Mock(), threading.Event(), threading.Event()
    started = []

    def drive():
        _wait_for(lambda: len(_pids(record)) == 1)
        restart.set()
        restart.set()  # a burst of changes folds into one restart
        _wait_for(lambda: len(_pids(record)) == 2)
        stopped.set()

    driver = threading.Thread(target=drive)
    driver.start()
    supervise(_starts_worker(record), env, database, ttl=30, owner_pid=os.getpid(),
              stopped=stopped, restart=restart, on_start=started.append, stop_timeout=2)
    driver.join()
    first, second = _pids(record)
    assert [process.pid for process in started] == [first, second]
    assert not psutil.pid_exists(first) and not psutil.pid_exists(second)
    database.open.assert_called_once()  # PostgreSQL and data stay across restarts
    database.close.assert_called_once()


def test_crashed_app_waits_for_a_change_instead_of_ending_the_lease(tmp_path):
    record = tmp_path / "pids"
    env = preview_environment(tmp_path, 18880, 18881)
    database, stopped, restart = Mock(), threading.Event(), threading.Event()
    exited = []

    def on_exit(process):
        exited.append(process.returncode)
        restart.set()  # what the file watcher does on the fix

    def stop_after_second_start():
        _wait_for(lambda: len(_pids(record)) == 2)
        stopped.set()

    driver = threading.Thread(target=stop_after_second_start)
    driver.start()
    supervise(_starts_worker(record, crash_first=True), env, database, ttl=30,
              owner_pid=os.getpid(), stopped=stopped, restart=restart, on_exit=on_exit,
              stop_timeout=2)
    driver.join()
    assert exited == [1]  # only the real crash, never the teardown
    assert not any(psutil.pid_exists(pid) for pid in _pids(record))
    database.close.assert_called_once()


def test_without_restart_an_exited_app_ends_the_lease(tmp_path):
    record = tmp_path / "pids"
    env = preview_environment(tmp_path, 18880, 18881)
    database = Mock()
    code = supervise(_starts_worker(record, crash_first=True), env, database, ttl=30,
                     owner_pid=os.getpid())
    assert code == 1 and len(_pids(record)) == 1
    database.close.assert_called_once()


def _dev_env(tmp_path, monkeypatch, *, marker: str | None = "own", preview: str = "1"):
    from app.core.config import settings

    env = preview_environment(tmp_path, 18880, 18881, extra={"SHUFFLE_DEV_SEED": "default"})
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("AI_SHUFFLE_PREVIEW", preview)
    data = Path(env["DATA_DIR"])
    monkeypatch.setattr(settings, "data_dir", data)
    if marker is None:
        (tmp_path / ".branch-preview").unlink()
    elif marker == "other":
        (tmp_path / ".branch-preview").write_text("/somewhere/else/data")
    return data


@pytest.mark.parametrize("marker,preview", [(None, "1"), ("other", "1"), ("own", "0")])
def test_dev_seed_refuses_anything_but_launcher_disposable_data(
    tmp_path, monkeypatch, marker, preview,
):
    import asyncio

    from scripts.devloop import seed as seed_module

    _dev_env(tmp_path, monkeypatch, marker=marker, preview=preview)
    called = Mock()
    monkeypatch.setattr(seed_module, "seed", called)
    assert disposable_preview_data() is False
    with pytest.raises(RuntimeError, match="refusing to seed"):
        asyncio.run(seed_dev_instance())
    called.assert_not_called()


def test_dev_seed_runs_on_marked_disposable_data_only_when_asked(tmp_path, monkeypatch):
    import asyncio

    from scripts.devloop import seed as seed_module

    _dev_env(tmp_path, monkeypatch)
    seen = []

    async def fake_seed(name):
        seen.append(name)

    monkeypatch.setattr(seed_module, "seed", fake_seed)
    assert disposable_preview_data() is True
    asyncio.run(seed_dev_instance())
    assert seen == ["default"]
    monkeypatch.delenv("SHUFFLE_DEV_SEED")
    (tmp_path / ".branch-preview").unlink()
    asyncio.run(seed_dev_instance())  # not asked: a no-op, even without the marker
    assert seen == ["default"]


def test_saved_access_is_reused_only_on_launcher_disposable_data(
    bare_db: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A dev-loop restart keeps its link; without the launcher's marker a
    saved access file is ignored and a fresh link is minted."""
    import json

    from app.core import encryption, preview_access
    from app.core.config import settings
    from app.db import database

    access = tmp_path / "access.json"
    env = preview_environment(tmp_path, 18880, 18881,
                              extra={"SHUFFLE_DEV_ACCESS_FILE": str(access)})
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("SHUFFLE_PREVIEW_ACCESS_HOST", "10.0.0.30")
    monkeypatch.setattr(settings, "data_dir", Path(env["DATA_DIR"]))
    monkeypatch.setattr(type(settings), "managed_database_available", lambda self: True)
    monkeypatch.setattr(preview_access, "_listener_port", 18880)
    monkeypatch.setattr(encryption, "_encryption_key", None)
    monkeypatch.setattr(encryption, "_credential_status", {})
    assert encryption.initialize_server_key(database)

    initialize_preview_access()
    first = json.loads(access.read_text())
    assert first["url"] in capsys.readouterr().out
    assert access.stat().st_mode & 0o777 == 0o600

    initialize_preview_access()  # a restart on the same disposable data
    assert "still valid after the restart" in capsys.readouterr().out
    assert json.loads(access.read_text()) == first

    (tmp_path / ".branch-preview").unlink()  # not the launcher's data any more
    assert disposable_preview_data() is False
    initialize_preview_access()
    out = capsys.readouterr().out
    assert "still valid" not in out and "open once in the agent browser" in out
    assert json.loads(access.read_text())["token"] != first["token"]
