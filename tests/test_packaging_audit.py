"""Offline setup/release/backup regression checks. Never contact Postgres or providers."""

import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

import run_local
from scripts.stage_release import stage_release

ROOT = Path(__file__).resolve().parents[1]


def test_setup_installs_required_harness(tmp_path, monkeypatch):
    monkeypatch.setattr(run_local, "__file__", str(tmp_path / "run_local.py"))
    (tmp_path / "requirements.txt").write_text("fastapi")
    (tmp_path / "fluid").mkdir(parents=True)
    (tmp_path / "fluid/pyproject.toml").write_text("[project]")
    calls = []
    monkeypatch.setattr(
        run_local.subprocess, "run", lambda args, **kw: calls.append(args)
    )
    assert run_local.install_dependencies()
    assert calls[0][-1] == "pip==26.2.1"
    assert str(tmp_path / "fluid") in calls[1]
    assert calls[2][-1] == "import fluid"


def test_missing_harness_rejects_setup_before_install(tmp_path, monkeypatch):
    monkeypatch.setattr(run_local, "__file__", str(tmp_path / "run_local.py"))
    (tmp_path / "requirements.txt").write_text("fastapi")
    monkeypatch.setattr(
        run_local.subprocess, "run", lambda *a, **kw: pytest.fail("must not install")
    )
    assert not run_local.install_dependencies()


def test_existing_frontend_is_rebuilt_and_stale_assets_removed(tmp_path, monkeypatch):
    monkeypatch.setattr(run_local, "__file__", str(tmp_path / "run_local.py"))
    for directory in ("app/static", "frontend/build"):
        (tmp_path / directory).mkdir(parents=True)
    (tmp_path / "app/static/index.html").write_text("old")
    (tmp_path / "app/static/obsolete.js").write_text("old")
    (tmp_path / "frontend/build/index.html").write_text("new")
    monkeypatch.setattr(run_local.shutil, "which", lambda name: name)
    calls = []
    monkeypatch.setattr(
        run_local.subprocess, "run", lambda args, **kw: calls.append(args)
    )
    assert run_local.build_frontend()
    assert calls == [["npm", "ci"], ["npm", "run", "build"]]
    assert (tmp_path / "app/static/index.html").read_text() == "new"
    assert not (tmp_path / "app/static/obsolete.js").exists()


def test_release_frontend_can_be_used_without_sources(tmp_path, monkeypatch):
    monkeypatch.setattr(run_local, "__file__", str(tmp_path / "run_local.py"))
    (tmp_path / "app/static").mkdir(parents=True)
    (tmp_path / "app/static/index.html").write_text("prebuilt")
    assert run_local.build_frontend()


def test_node_refresh_does_not_skip_existing_dist(tmp_path, monkeypatch):
    monkeypatch.setattr(run_local, "__file__", str(tmp_path / "run_local.py"))
    package = tmp_path / "browser-mcp"
    (package / "dist").mkdir(parents=True)
    (package / "dist/index.js").write_text("stale")
    (package / "package-lock.json").write_text("{}")
    calls = []
    monkeypatch.setattr(run_local.shutil, "which", lambda name: name)
    monkeypatch.setattr(
        run_local.subprocess, "run", lambda args, **kw: calls.append(args)
    )
    assert run_local.build_browser_mcp()
    assert calls == [["npm", "ci", "--ignore-scripts"], ["npm", "run", "build"]]


@pytest.mark.parametrize(
    "version,supported",
    [
        ("v18.20.8", False),
        ("v20.18.0", False),
        ("v20.19.0", True),
        ("v22.11.0", False),
        ("v22.12.0", True),
        ("v24.0.0", True),
    ],
)
def test_node_version_floor(version, supported, monkeypatch):
    monkeypatch.setattr(run_local.shutil, "which", lambda name: name)
    monkeypatch.setattr(run_local.subprocess, "check_output", lambda *a, **kw: version)
    assert run_local.check_node_version() is supported


def test_stage_release_includes_runtime_sources_without_local_artifacts(tmp_path):
    repo = tmp_path / "repo"
    for name in (
        "app/main.py",
        "frontend/build/index.html",
        "fluid/pyproject.toml",
        "fluid/fluid/__init__.py",
        "tools/package-lock.json",
        "tools/worker.ts",
        "tools/scripts/clean.mjs",
        "browser-mcp/package-lock.json",
        "browser-mcp/src/index.ts",
        "requirements.txt",
        "requirements-native.txt",
        "run_local.py",
        ".env.example",
        "LICENSE",
        "tools/node_modules/private.txt",
        ".env",
    ):
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture")
    dest = tmp_path / "release"
    stage_release(repo, dest)
    for name in (
        "fluid/fluid/__init__.py",
        "tools/worker.ts",
        "tools/scripts/clean.mjs",
        "browser-mcp/src/index.ts",
        "app/static/index.html",
    ):
        assert (dest / name).is_file()
    assert not (dest / ".env").exists()
    assert not (dest / "tools/node_modules").exists()
    with pytest.raises(ValueError, match="refusing to overwrite"):
        stage_release(repo, dest)


def bash_path():
    git_bash = Path("C:/Program Files/Git/bin/bash.exe")
    if sys.platform == "win32":
        if git_bash.is_file():
            return str(git_bash)
        pytest.skip("Git Bash not installed")
    return shutil.which("bash") or pytest.skip("bash not installed")


def backup_fixture(tmp_path):
    data, workspace, settings, bindir = [
        tmp_path / name for name in ("data", "workspace", "settings", "bin")
    ]
    for folder in (
        data / "fluid/skills",
        workspace / "project with spaces/generated-images",
        workspace / "nested/project/generated-models",
        settings,
        bindir,
    ):
        folder.mkdir(parents=True)
    (data / "fluid/skills/memories.json").write_text("offline fixture")
    (data / ".instance_id").write_text("fixture-id")
    (workspace / "project with spaces/generated-images/image.png").write_text("fixture")
    (workspace / "nested/project/generated-models/model.glb").write_text("fixture")
    (settings / "settings.toml").write_text("fixture")
    stub = bindir / "pg_dump"
    stub.write_text(
        '#!/usr/bin/env bash\nfor arg in "$@"; do case "$arg" in --file=*) printf fixture > "${arg#--file=}" ;; esac; done\n'
    )
    stub.chmod(0o755)
    env = os.environ.copy()

    def shell_path(path):
        value = path.as_posix()
        return "/" + value[0].lower() + value[2:] if sys.platform == "win32" else value

    env.update(
        DATA_DIR=shell_path(data),
        WORKSPACE_DIR=shell_path(workspace),
        FLUID_HOME=shell_path(settings),
        BACKUP_DIR=shell_path(data / "backups"),
        PATH=str(bindir) + os.pathsep + env["PATH"],
    )
    return data, env


def test_backup_covers_project_media_memory_and_excludes_backup_recursion(tmp_path):
    data, env = backup_fixture(tmp_path)
    result = subprocess.run(
        [bash_path(), str(ROOT / "scripts/backup.sh")],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    with tarfile.open(
        next((data / "backups").glob("ai-shuffle-media-*.tar.gz"))
    ) as archive:
        names = archive.getnames()
        assert "./project with spaces/generated-images/image.png" in names
        assert "./nested/project/generated-models/model.glb" in names
    with tarfile.open(
        next((data / "backups").glob("ai-shuffle-data-*.tar.gz"))
    ) as archive:
        names = archive.getnames()
        assert "./fluid/skills/memories.json" in names
        assert "./.instance_id" in names
        assert not any(name.startswith("./backups") for name in names)


def test_backup_dry_run_via_entrypoint_writes_nothing(tmp_path):
    data, env = backup_fixture(tmp_path)
    result = subprocess.run(
        [
            bash_path(),
            str(ROOT / "docker/backup-entrypoint.sh"),
            bash_path(),
            str(ROOT / "scripts/backup.sh"),
            "--dry-run",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "dry-run" in result.stdout
    assert not (data / "backups").exists()


def test_custom_backup_destination_inside_project_media_is_excluded(tmp_path):
    _data, env = backup_fixture(tmp_path)
    env["BACKUP_DIR"] = (
        env["WORKSPACE_DIR"] + "/project with spaces/generated-images/backups"
    )
    result = subprocess.run(
        [bash_path(), str(ROOT / "scripts/backup.sh")],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    backup_dir = tmp_path / "workspace/project with spaces/generated-images/backups"
    with tarfile.open(next(backup_dir.glob("ai-shuffle-media-*.tar.gz"))) as archive:
        assert "./project with spaces/generated-images/image.png" in archive.getnames()
        assert not any("/backups" in name for name in archive.getnames())


def test_failed_sql_restore_does_not_report_success(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    docker = bindir / "docker"
    docker.write_text(
        '#!/usr/bin/env bash\ncat >/dev/null\necho "fixture SQL error" >&2\nexit 1\n'
    )
    docker.chmod(0o755)
    backup = tmp_path / "fixture.sql"
    backup.write_text("invalid SQL for the fake client")
    env = os.environ.copy()
    env["PATH"] = str(bindir) + os.pathsep + env["PATH"]
    result = subprocess.run(
        [bash_path(), str(ROOT / "tools/restore-db.sh"), backup.as_posix()],
        input=b"y\n",
        env=env,
        text=False,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert result.returncode != 0
    assert b"fixture SQL error" in result.stderr
    assert b"Done." not in result.stdout
