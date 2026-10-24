"""Required isolation preflight: no root writes before policy validation."""
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest


@pytest.fixture
def runtime(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    pytest.importorskip("fcntl")
    path = Path(__file__).resolve().parents[2] / "docker" / "container_runtime.py"
    spec = importlib.util.spec_from_file_location("isolation_preflight_runtime", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in ("AGENT_ISOLATION", "AGENT_ISOLATION_SERVICE_UID",
                 "AGENT_ISOLATION_SERVICE_GID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(module.os, "geteuid", lambda: 0)
    return module


def forbid_writes(runtime: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected(*args: Any, **kwargs: Any) -> None:
        pytest.fail("isolation must be validated before root filesystem work")

    for name in ("load_startup_settings", "remap_home", "repair_home_root_files",
                 "open_directory", "validate_mount_root"):
        monkeypatch.setattr(runtime, name, unexpected)


@pytest.mark.parametrize("action", ["validate-isolation", "remap-home"])
@pytest.mark.parametrize("problem,diagnostic", [
    ("uid", "AGENT_ISOLATION_SERVICE_UID must be a positive numeric id"),
    ("gid", "AGENT_ISOLATION_SERVICE_GID must be a positive numeric id"),
    ("uid_collision", "service uid/gid must differ from PUID/PGID"),
    ("gid_collision", "service uid/gid must differ from PUID/PGID"),
    ("helpers", "image lacks isolation helpers"),
    ("nonroot", "start the container as root, without --user"),
])
def test_required_refuses_before_root_work(
    runtime: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    tmp_path: Path, action: str, problem: str, diagnostic: str,
) -> None:
    monkeypatch.setenv("AGENT_ISOLATION", "required")
    if problem in {"uid", "gid", "uid_collision", "gid_collision"}:
        suffix = "UID" if problem.startswith("uid") else "GID"
        value = "41002" if problem.endswith("collision") else "invalid"
        monkeypatch.setenv("AGENT_ISOLATION_SERVICE_" + suffix, value)
    elif problem == "helpers":
        monkeypatch.setattr(runtime, "_ISOLATION_FILE", tmp_path / "missing.py")
    else:
        monkeypatch.setattr(runtime.os, "geteuid", lambda: 41002)
    forbid_writes(runtime, monkeypatch)
    args = [action, "--new-uid", "41002", "--new-gid", "41002"]
    if action == "remap-home":
        args += ["--old-uid", "41001", "--old-gid", "41001"]
    assert runtime.main(args) == 1
    error = capsys.readouterr().err
    assert "required agent isolation: startup refused; " in error
    assert diagnostic in error


@pytest.mark.parametrize("mode", ["on", "off", ""])
def test_compatibility_preflight_does_not_require_isolation(
    runtime: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: str,
) -> None:
    monkeypatch.setenv("AGENT_ISOLATION", mode)
    monkeypatch.setenv("AGENT_ISOLATION_SERVICE_GID", "invalid")
    monkeypatch.setattr(runtime, "_ISOLATION_FILE", tmp_path / "missing.py")
    monkeypatch.setattr(runtime.os, "geteuid", lambda: 41002)
    forbid_writes(runtime, monkeypatch)
    assert runtime.main(["validate-isolation", "--new-uid", "41002",
                         "--new-gid", "41002"]) == 0


def test_invalid_policy_refuses_before_root_work(
    runtime: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("AGENT_ISOLATION", "requried")
    forbid_writes(runtime, monkeypatch)
    assert runtime.main(["validate-isolation", "--new-uid", "41002",
                         "--new-gid", "41002"]) == 1
    assert "AGENT_ISOLATION: startup refused; valid values: off, on, required" in (
        capsys.readouterr().err
    )


def test_required_valid_identity_allows_home_preparation(
    runtime: ModuleType, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENT_ISOLATION", " required ")
    settings = SimpleNamespace(effective_data_dir=Path("/data"),
                               effective_workspace_dir=Path("/workspace"))
    calls: list[str] = []
    monkeypatch.setattr(runtime, "load_startup_settings", lambda: settings)
    monkeypatch.setattr(runtime, "remap_home", lambda *args: calls.append("remap"))
    monkeypatch.setattr(runtime, "repair_home_root_files", lambda *args: calls.append("repair"))
    assert runtime.main(["remap-home", "--old-uid", "41001", "--old-gid", "41001",
                         "--new-uid", "41002", "--new-gid", "41002"]) == 0
    assert calls == ["remap", "repair"]


def test_required_direct_runtime_refuses_before_mount_writes(
    runtime: ModuleType, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENT_ISOLATION", "required")
    monkeypatch.setenv("AGENT_ISOLATION_SERVICE_GID", "41002")
    account = SimpleNamespace(pw_uid=41002, pw_gid=41002, pw_dir="/home/appuser")
    monkeypatch.setattr(runtime.pwd, "getpwnam", lambda name: account)
    forbid_writes(runtime, monkeypatch)
    with pytest.raises(runtime.RuntimeFailure, match="required agent isolation: startup refused"):
        runtime.drop_privileges(SimpleNamespace())


@pytest.mark.parametrize("mode", ["required", "on"])
def test_migration_failure_preserves_mode_policy(
    runtime: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: str,
) -> None:
    monkeypatch.setenv("AGENT_ISOLATION", mode)
    module = runtime.isolation()
    calls: list[str] = []

    def unavailable(*args: Any) -> None:
        raise module.IsolationError("synthetic migration failure")

    monkeypatch.setattr(module, "ensure_account", unavailable)
    monkeypatch.setattr(module, "disable", lambda *args: calls.append("rollback"))
    monkeypatch.setattr(module, "release_code", lambda *args: calls.append("release"))
    settings = SimpleNamespace(effective_data_dir=tmp_path)
    account = SimpleNamespace(pw_uid=41002, pw_gid=41002)
    if mode == "required":
        with pytest.raises(runtime.RuntimeFailure, match="required agent isolation: startup refused"):
            runtime.isolate(settings, account)
        assert calls == []
    else:
        assert runtime.isolate(settings, account) is None
        assert calls == ["rollback", "release"]


def test_required_available_starts_app(
    runtime: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("AGENT_ISOLATION", "required")
    module = runtime.isolation()
    # Exercise the real policy/bootstrap producer; OS enforcement needs image smoke.
    monkeypatch.setattr(module, "ensure_account", lambda *args: None)
    monkeypatch.setattr(module, "protect_code", lambda *args: None)
    monkeypatch.setattr(module, "enable", lambda *args: None)
    settings = SimpleNamespace(effective_data_dir=tmp_path)
    account = SimpleNamespace(pw_uid=41002, pw_gid=41002)
    splits: list[Any] = []

    def drop(conf: Any, **kwargs: Any) -> None:
        runtime.validate_isolation_startup(account.pw_uid, account.pw_gid)
        splits.append(runtime.isolate(conf, account))

    monkeypatch.setattr(runtime, "load_startup_settings", lambda: settings)
    monkeypatch.setattr(runtime, "drop_privileges", drop)
    monkeypatch.setattr(runtime, "load_settings", lambda: settings)
    monkeypatch.setattr(runtime, "harden_process_introspection", lambda conf: None)
    monkeypatch.setattr(runtime, "handover", lambda conf: None)
    started: list[str] = []
    monkeypatch.setattr(runtime, "run", lambda *args: started.append("app") or 0)
    assert runtime.main(["run"]) == 0
    assert started == ["app"]
    assert splits == [module.identity(41002, 41002)]
    assert "required agent isolation established" in capsys.readouterr().err


def test_entrypoint_validates_before_setup() -> None:
    script = (Path(__file__).resolve().parents[2] / "entrypoint.sh").read_text()
    validate = script.index("container_runtime.py validate-isolation")
    for operation in ('require_bundled_tools "$@"', "if claude_cli_keep_installed",
                      "container_runtime.py remap-home", "groupmod -o", "usermod -o"):
        assert validate < script.index(operation)
