"""Docker agent isolation (#1957): the migration plan, its rollback and the
guarantee that the default (off) changes nothing.

Ownership is simulated: the tests run unprivileged, so a fake ``Ops`` keeps an
owner table over a real temporary DATA_DIR. The real-uid proof (agent shells
get "Permission denied") is ``test_agent_isolation_root.py``.
"""
import importlib.util
import os
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("fcntl", reason="The Linux container supervisor requires POSIX file locking")

DOCKER = Path(__file__).resolve().parents[2] / "docker"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


iso = _load("shuffle_agent_isolation_test", DOCKER / "agent_isolation.py")
PUID, PGID, SUID, SGID = 1000, 1000, 64000, 64000
IDENT = iso.Identity(PUID, PGID, SUID, SGID)


class FakeOps(iso.Ops):
    """Real directory tree, simulated owners and modes; records every change."""

    def __init__(self, root):
        self.root = Path(root)
        self.owners, self.modes, self.devices, self.changes = {}, {}, {}, []

    def lstat(self, path):
        path = Path(path)
        real = os.lstat(path)
        default = (0, 0) if path.name == iso.RECORD_NAME else (PUID, PGID)
        uid, gid = self.owners.get(path, default)
        mode = stat.S_IFMT(real.st_mode) | self.modes.get(path, stat.S_IMODE(real.st_mode))
        return SimpleNamespace(st_mode=mode, st_uid=uid, st_gid=gid, st_nlink=real.st_nlink,
                               st_dev=self.devices.get(path, real.st_dev))

    def lchown(self, path, uid, gid):
        self.changes.append(("chown", Path(path), uid, gid))
        self.owners[Path(path)] = (uid, gid)

    def chmod(self, path, mode):
        self.changes.append(("chmod", Path(path), mode))
        self.modes[Path(path)] = mode


@pytest.fixture
def data(tmp_path):
    root = tmp_path / "data"
    (root / ".credential-keys").mkdir(parents=True)
    (root / ".credential-keys" / "key").write_text("k")
    (root / ".managed-connection").mkdir()
    (root / ".managed-connection" / "key").write_text("k")
    (root / "postgres" / "data").mkdir(parents=True)
    (root / "postgres" / "password").write_text("p")
    for name in ("managed-postgres.json", "database-mode.json", "preview-access.key"):
        (root / name).write_text("{}")
    (root / "notes").mkdir()
    (root / "notes" / "todo.md").write_text("mine")
    versions = root / "application-updates" / "versions" / ("a" * 40)
    versions.mkdir(parents=True)
    (root / "application-updates" / "state.json").write_text("{}")
    ops = FakeOps(root)
    ops.modes[root] = 0o755
    for path in (root / "application-updates", root / "application-updates" / "versions", versions):
        ops.modes[path] = 0o700
    return root, ops


def _protected(root):
    paths = []
    for name in iso.PROTECTED:
        top = root / name
        paths += [top, *top.rglob("*")]
    return paths


# --- settings ------------------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [("", "off"), ("0", "off"), ("off", "off"), ("FALSE", "off"),
                                          ("on", "on"), (" On ", "on"), ("1", "on"), ("yes", "on"),
                                          ("maybe", "invalid")])
def test_setting(raw, expected):
    assert iso.setting({iso.SETTING: raw}) == expected


def test_setting_defaults_off():
    assert iso.setting({}) == "off"


def test_identity_defaults_and_refusals():
    assert iso.identity(1000, 1000, {}) == iso.Identity(1000, 1000, 64000, 64000)
    assert iso.identity(99, 100, {"AGENT_ISOLATION_SERVICE_UID": "5000",
                                  "AGENT_ISOLATION_SERVICE_GID": "5001"}) == iso.Identity(99, 100, 5000, 5001)
    for environ in ({"AGENT_ISOLATION_SERVICE_UID": "1000"}, {"AGENT_ISOLATION_SERVICE_GID": "1000"},
                    {"AGENT_ISOLATION_SERVICE_UID": "0"}, {"AGENT_ISOLATION_SERVICE_UID": "-5"},
                    {"AGENT_ISOLATION_SERVICE_UID": "shuffle"}):
        with pytest.raises(iso.IsolationError):
            iso.identity(1000, 1000, environ)
    with pytest.raises(iso.IsolationError):
        iso.identity(0, 0, {})


# --- enable ----------------------------------------------------------------------


def test_plan_enable_lists_only_puid_owned_protected_entries(data):
    root, ops = data
    ops.owners[root / "postgres" / "password"] = (SUID, SGID)  # already migrated
    ops.owners[root / "preview-access.key"] = (PUID, 4242)
    plan = iso.plan_enable(root, IDENT, ops)
    paths = {path for path, *_ in plan}
    assert paths == set(_protected(root)) - {root / "postgres" / "password"}
    assert all(uid == SUID and gid == SGID for _p, uid, gid, _old in plan)
    assert (root / "preview-access.key", SUID, SGID, 4242) in plan
    assert not ops.changes and not (root / iso.RECORD_NAME).exists()


def test_enable_records_first_then_migrates(data, monkeypatch):
    root, ops = data
    written = []
    real_write = iso._write_record
    monkeypatch.setattr(iso, "_write_record",
                        lambda d, record, o: (written.append((len(ops.changes), record["state"])),
                                              real_write(d, record, o)))
    ops.owners[root / "preview-access.key"] = (PUID, 4242)
    record = iso.enable(root, IDENT, ops, log=lambda _m: None)
    assert written[0] == (0, "enabling")  # before any ownership change
    assert written[-1][1] == "enabled" and record["state"] == "enabled"
    assert iso.read_record(root, ops) == record
    for path in _protected(root):
        assert ops.lstat(path).st_uid == SUID, path
    for path in (root / "notes", root / "notes" / "todo.md"):
        assert ops.lstat(path).st_uid == PUID
    assert record["groups"] == {"preview-access.key": 4242}
    assert record["data_root"] == {"uid": PUID, "gid": PGID, "mode": 0o755}
    info = ops.lstat(root)
    assert (info.st_uid, info.st_gid) == (SUID, PGID)
    assert stat.S_IMODE(info.st_mode) == 0o755 | stat.S_ISVTX | 0o770
    for path in (root / "application-updates", root / "application-updates" / "versions",
                 root / "application-updates" / "versions" / ("a" * 40)):
        assert ops.lstat(path).st_mode & stat.S_IXOTH
        assert not ops.lstat(path).st_mode & stat.S_IROTH
    assert ops.lstat(root / "application-updates" / "state.json").st_uid == SUID


def test_enable_never_creates_or_deletes_anything(data):
    root, ops = data
    before = sorted(p.relative_to(root) for p in root.rglob("*"))
    iso.enable(root, IDENT, ops, log=lambda _m: None)
    after = sorted(p.relative_to(root) for p in root.rglob("*"))
    assert after == sorted([*before, Path(iso.RECORD_NAME)])
    assert (root / "postgres" / "password").read_text() == "p"
    assert (root / ".credential-keys" / "key").read_text() == "k"


def test_enable_on_a_fresh_data_dir(tmp_path):
    root = tmp_path / "fresh"
    root.mkdir()
    ops = FakeOps(root)
    record = iso.enable(root, IDENT, ops, log=lambda _m: None)
    assert record["state"] == "enabled" and record["groups"] == {}
    assert ops.lstat(root).st_uid == SUID


def test_enable_is_idempotent(data):
    root, ops = data
    first = iso.enable(root, IDENT, ops, log=lambda _m: None)
    count = len(ops.changes)
    second = iso.enable(root, IDENT, ops, log=lambda _m: None)
    assert second == first
    assert len(ops.changes) == count


def test_enable_resumes_after_an_interruption(data, monkeypatch):
    root, ops = data
    real = ops.lchown
    calls = []

    def flaky(path, uid, gid):
        calls.append(path)
        if len(calls) == 3:
            raise OSError("interrupted")
        real(path, uid, gid)

    monkeypatch.setattr(ops, "lchown", flaky)
    with pytest.raises(OSError):
        iso.enable(root, IDENT, ops, log=lambda _m: None)
    assert iso.read_record(root, ops)["state"] == "enabling"
    monkeypatch.setattr(ops, "lchown", real)
    record = iso.enable(root, IDENT, ops, log=lambda _m: None)
    assert record["state"] == "enabled"
    assert record["data_root"] == {"uid": PUID, "gid": PGID, "mode": 0o755}
    assert all(ops.lstat(p).st_uid == SUID for p in _protected(root))


def test_enabled_install_does_not_adopt_planted_entries(data):
    root, ops = data
    iso.enable(root, IDENT, ops, log=lambda _m: None)
    planted = root / ".credential-keys" / "planted"
    planted.write_text("an agent's key")  # PUID-owned in the owner table
    messages = []
    iso.enable(root, IDENT, ops, log=messages.append)
    assert ops.lstat(planted).st_uid == PUID
    assert any("not adopting" in m and str(planted) in m for m in messages)


def test_enable_refuses_a_symlinked_protected_entry_before_changing_anything(data):
    root, ops = data
    target = root.parent / "elsewhere"
    target.mkdir()
    (root / "postgres" / "password").unlink()
    for item in sorted((root / "postgres").rglob("*"), reverse=True):
        item.rmdir()
    (root / "postgres").rmdir()
    (root / "postgres").symlink_to(target)
    with pytest.raises(iso.IsolationError, match="symlink"):
        iso.enable(root, IDENT, ops, log=lambda _m: None)
    assert not ops.changes and not (root / iso.RECORD_NAME).exists()


def test_symlinks_inside_protected_entries_are_not_followed(data):
    root, ops = data
    outside = root.parent / "outside"
    outside.mkdir()
    (outside / "victim").write_text("x")
    (root / "postgres" / "link").symlink_to(outside)
    iso.enable(root, IDENT, ops, log=lambda _m: None)
    assert ops.lstat(outside / "victim").st_uid == PUID
    assert ("chown", root / "postgres" / "link", SUID, SGID) in ops.changes  # the link itself


# --- the record --------------------------------------------------------------


def test_record_is_trusted_only_when_root_owned(data):
    root, ops = data
    iso.enable(root, IDENT, ops, log=lambda _m: None)
    assert iso.read_record(root, ops)
    ops.owners[root / iso.RECORD_NAME] = (PUID, PGID)
    assert iso.read_record(root, ops) is None
    ops.owners[root / iso.RECORD_NAME] = (0, 0)
    (root / iso.RECORD_NAME).write_text("not json")
    assert iso.read_record(root, ops) is None


def test_a_planted_record_is_replaced_not_written_through(data):
    root, ops = data
    victim = root.parent / "victim"
    victim.write_text("keep")
    (root / iso.RECORD_NAME).symlink_to(victim)
    ops.owners[root / iso.RECORD_NAME] = (PUID, PGID)
    iso.enable(root, IDENT, ops, log=lambda _m: None)
    assert victim.read_text() == "keep"
    assert not (root / iso.RECORD_NAME).is_symlink()


# --- rollback ------------------------------------------------------------------


def test_needs_rollback(data):
    root, ops = data
    assert not iso.needs_rollback(root, IDENT, ops)
    iso.enable(root, IDENT, ops, log=lambda _m: None)
    assert iso.needs_rollback(root, IDENT, ops)
    (root / iso.RECORD_NAME).unlink()
    assert iso.needs_rollback(root, IDENT, ops)  # the owners alone say so


def test_disable_restores_owners_groups_and_the_data_root(data):
    root, ops = data
    ops.owners[root / "preview-access.key"] = (PUID, 4242)
    before = {p: ops.lstat(p) for p in [root, *root.rglob("*")]}
    iso.enable(root, IDENT, ops, log=lambda _m: None)
    iso.disable(root, IDENT, ops, log=lambda _m: None)
    for path, info in before.items():
        after = ops.lstat(path)
        assert (after.st_uid, after.st_gid, stat.S_IMODE(after.st_mode)) == (
            info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)), path
    assert not (root / iso.RECORD_NAME).exists()
    assert not iso.needs_rollback(root, IDENT, ops)


def test_disable_without_a_record_still_hands_everything_back(data):
    root, ops = data
    iso.enable(root, IDENT, ops, log=lambda _m: None)
    (root / iso.RECORD_NAME).unlink()
    iso.disable(root, IDENT, ops, log=lambda _m: None)
    assert all(ops.lstat(p).st_uid == PUID for p in _protected(root))
    info = ops.lstat(root)
    assert (info.st_uid, info.st_gid) == (PUID, PGID)
    assert not stat.S_IMODE(info.st_mode) & stat.S_ISVTX


def test_disable_ignores_an_untrusted_record(data):
    root, ops = data
    iso.enable(root, IDENT, ops, log=lambda _m: None)
    (root / iso.RECORD_NAME).write_text(
        '{"version": 1, "groups": {"postgres": 4242}, "data_root": {"uid": 5, "gid": 5, "mode": 4095}}')
    ops.owners[root / iso.RECORD_NAME] = (PUID, PGID)
    iso.disable(root, IDENT, ops, log=lambda _m: None)
    assert ops.lstat(root / "postgres").st_gid == PGID
    assert ops.lstat(root).st_uid == PUID


@pytest.mark.parametrize("value", [None, 0, True, SGID, "4242", -1])
def test_safe_gid_rejects_unusable_values(value):
    assert iso._safe_gid(value, IDENT) == PGID


# --- image code ----------------------------------------------------------------


@pytest.fixture
def app_dir(tmp_path):
    app = tmp_path / "app"
    (app / "app" / "core").mkdir(parents=True)
    (app / "app" / "core" / "x.py").write_text("")
    (app / "scripts").mkdir()
    (app / ".env.example").write_text("")
    (app / "frontend").mkdir()
    return app, FakeOps(app)


def test_protect_and_release_code(app_dir):
    app, ops = app_dir
    messages = []
    iso.protect_code(app, IDENT, ops, log=messages.append)
    for path in (app, app / "app", app / "app" / "core" / "x.py", app / "scripts", app / ".env.example"):
        assert ops.lstat(path).st_uid == 0, path
    assert ops.lstat(app / "frontend").st_uid == PUID
    assert any("/app/frontend" in m for m in messages)
    iso.release_code(app, IDENT, ops)
    assert all(ops.lstat(p).st_uid == PUID for p in [app, *app.rglob("*")])


def test_a_mounted_source_tree_is_never_chowned(app_dir):
    app, ops = app_dir
    ops.devices[app / "app"] = -1
    with pytest.raises(iso.IsolationError, match="mount"):
        iso.protect_code(app, IDENT, ops, log=lambda _m: None)
    ops.owners[app] = (0, 0)
    iso.release_code(app, IDENT, ops)
    assert all(change[1] != app / "app" and app / "app" not in change[1].parents for change in ops.changes)


# --- bootstrap -------------------------------------------------------------------


def test_bootstrap_off_changes_nothing(data, app_dir, monkeypatch):
    root, ops = data
    monkeypatch.delenv(iso.SETTING, raising=False)
    monkeypatch.setattr(iso, "ensure_account", lambda *a: pytest.fail("no account work when off"))
    assert iso.bootstrap(root, IDENT, app_dir=app_dir[0], ops=ops, log=lambda _m: None) is None
    assert not ops.changes
    assert not (root / iso.RECORD_NAME).exists()


def test_bootstrap_without_an_identity_is_a_no_op(data, monkeypatch):
    monkeypatch.setenv(iso.SETTING, "on")
    assert iso.bootstrap(data[0], None, ops=data[1]) is None
    assert not data[1].changes


def test_bootstrap_on_then_off(data, app_dir, monkeypatch):
    root, ops = data
    monkeypatch.setattr(iso, "ensure_account", lambda *a: None)
    monkeypatch.setenv(iso.SETTING, "on")
    assert iso.bootstrap(root, IDENT, app_dir=app_dir[0], ops=ops, log=lambda _m: None) == IDENT
    assert ops.lstat(root / "managed-postgres.json").st_uid == SUID
    assert ops.lstat(app_dir[0] / "app").st_uid == 0
    monkeypatch.setenv(iso.SETTING, "off")
    assert iso.bootstrap(root, IDENT, app_dir=app_dir[0], ops=ops, log=lambda _m: None) is None
    assert all(ops.lstat(p).st_uid == PUID for p in _protected(root))
    assert ops.lstat(app_dir[0] / "app").st_uid == PUID
    assert not (root / iso.RECORD_NAME).exists()


def test_bootstrap_failure_rolls_back_and_runs_unisolated(data, app_dir, monkeypatch):
    root, ops = data
    monkeypatch.setattr(iso, "ensure_account", lambda *a: None)
    monkeypatch.setattr(iso, "enable", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    monkeypatch.setenv(iso.SETTING, "on")
    messages = []
    assert iso.bootstrap(root, IDENT, app_dir=app_dir[0], ops=ops, log=messages.append) is None
    assert any("could NOT be enabled" in m for m in messages)
    assert ops.lstat(app_dir[0] / "app").st_uid == PUID  # code protection undone


def test_bootstrap_ignores_an_invalid_setting(data, monkeypatch):
    root, ops = data
    monkeypatch.setenv(iso.SETTING, "perhaps")
    messages = []
    assert iso.bootstrap(root, IDENT, ops=ops, log=messages.append) is None
    assert not ops.changes and any("ignoring" in m for m in messages)


def test_ensure_account_requires_the_image_account(monkeypatch):
    monkeypatch.setattr(iso.pwd, "getpwnam", lambda name: (_ for _ in ()).throw(KeyError(name)))
    with pytest.raises(iso.IsolationError):
        iso.ensure_account(IDENT, run=lambda *a, **k: pytest.fail("no usermod"))


def test_ensure_account_moves_the_account_to_the_configured_ids(monkeypatch):
    monkeypatch.setattr(iso.pwd, "getpwnam", lambda name: SimpleNamespace(pw_uid=64000, pw_gid=64000))
    monkeypatch.setattr(iso.pwd, "getpwuid", lambda uid: SimpleNamespace(pw_name="appuser"))
    monkeypatch.setattr(iso.grp, "getgrnam", lambda name: SimpleNamespace(gr_gid=64000))
    monkeypatch.setattr(iso.grp, "getgrgid", lambda gid: SimpleNamespace(gr_mem=[]))
    calls = []
    iso.ensure_account(iso.Identity(99, 100, 5000, 5001), run=lambda argv, check: calls.append(argv))
    assert calls == [["groupmod", "-o", "-g", "5001", "shuffle"],
                     ["usermod", "-o", "-u", "5000", "-g", "5001", "shuffle"]]
    monkeypatch.setattr(iso.grp, "getgrgid", lambda gid: SimpleNamespace(gr_mem=["appuser"]))
    with pytest.raises(iso.IsolationError, match="share a group"):
        iso.ensure_account(iso.Identity(99, 100, 64000, 64000), run=lambda *a, **k: None)


# --- process helpers ---------------------------------------------------------------


def test_helpers_are_empty_while_inactive(monkeypatch):
    monkeypatch.setattr(iso, "ACTIVE", None)
    assert iso.service_spawn() == {} and iso.agent_spawn() == {} and iso.service_env() == {}
    assert not iso.own_record("/data/agent-isolation.json")
    command = [sys.executable, "-m", "app.main"]
    assert iso.app_command(command) is command
    assert iso.app_environment(SimpleNamespace(), "/usr/bin") == {}
    with iso.agent_files():
        pass
    iso.ensure_traversable("/nonexistent", log=lambda _m: pytest.fail("no work"))


def test_helpers_while_active(monkeypatch, tmp_path):
    monkeypatch.setattr(iso, "ACTIVE", IDENT)
    assert iso.service_spawn() == {"user": SUID, "group": SGID}
    assert iso.agent_spawn() == {"user": PUID, "group": PGID}
    assert iso.service_env() == {"HOME": "/nonexistent"}
    assert iso.own_record(tmp_path / iso.RECORD_NAME) and not iso.own_record(tmp_path / "x")
    assert iso.app_command(["/py", "-m", "app.main"]) == ["/py", "-s", "-c", iso.APP_SHIM]
    with pytest.raises(iso.IsolationError):
        iso.app_command(["/py", "-c", "evil"])
    monkeypatch.setattr(iso.pwd, "getpwuid", lambda uid: SimpleNamespace(pw_dir="/home/appuser"))
    settings = SimpleNamespace(effective_workspace_dir="/workspace", effective_data_dir="/data")
    env = iso.app_environment(settings, "/usr/bin:/home/appuser/.local/bin",
                              {iso.EXTRA_DIRS: "/srv/repos:relative:/workspace"})
    assert env == {"SHUFFLE_AGENT_ISOLATION": "on", "PATH": "/usr/bin:/home/appuser/.local/bin",
                   "SHUFFLE_AGENT_ROOTS": "/workspace:/home/appuser:/data:/tmp:/srv/repos"}


def test_app_shim_compiles():
    compile(iso.APP_SHIM, "<shim>", "exec")


def test_app_shim_refuses_plain_ids():
    # Run without a split (real == effective): it must stop, never run the app.
    result = subprocess.run([sys.executable, "-s", "-c", iso.APP_SHIM], capture_output=True, text=True,
                            timeout=30)
    assert result.returncode != 0 and "unexpected ids" in result.stderr


def test_trusted_path_keeps_only_root_owned_unwritable_directories():
    owners = {"/": (0, 0o755), "/usr": (0, 0o755), "/usr/bin": (0, 0o755), "/home": (0, 0o755),
              "/home/appuser": (1000, 0o755), "/home/appuser/.local": (1000, 0o755),
              "/home/appuser/.local/bin": (1000, 0o755), "/opt": (0, 0o777), "/opt/bin": (0, 0o755)}

    def fake_stat(path):
        if str(path) not in owners:
            raise FileNotFoundError(path)
        uid, mode = owners[str(path)]
        return SimpleNamespace(st_uid=uid, st_mode=stat.S_IFDIR | mode)

    value = "/home/appuser/.local/bin:/usr/bin:relative:/opt/bin:/missing:/usr/bin"
    assert iso.trusted_path(value, stat_fn=fake_stat) == "/usr/bin"


# --- the supervisor with the mode off ------------------------------------------------


@pytest.fixture
def runtime():
    # A private copy: the isolation module it loads is cached per module.
    return _load("shuffle_container_runtime_isolation_test", DOCKER / "container_runtime.py")


def test_start_app_off_is_the_plain_popen(runtime, monkeypatch):
    calls = []
    monkeypatch.setattr(runtime.subprocess, "Popen", lambda *a, **k: calls.append((a, k)) or "proc")
    command = [sys.executable, "-m", "app.main"]
    assert runtime.start_app(command, "/cwd", {"A": "1"}, Path("/data/application-updates")) == "proc"
    assert calls == [((command,), {"cwd": "/cwd", "env": {"A": "1"}, "start_new_session": True})]
    assert calls[0][0][0] is command


def test_supervisor_helpers_are_empty_when_off(runtime):
    module = runtime.isolation()
    assert module.ACTIVE is None
    assert module.service_spawn() == {} and module.agent_spawn() == {}
    env = {"PATH": "/usr/bin", "HOME": "/home/appuser", "LC_ALL": "C", "SECRET": "x"}
    assert runtime.postgres_base_env(env) == {"PATH": "/usr/bin", "HOME": "/home/appuser", "LC_ALL": "C"}


def test_supervisor_without_the_isolation_file_behaves_as_off(runtime, monkeypatch, tmp_path):
    monkeypatch.setattr(runtime, "_ISOLATION_FILE", tmp_path / "missing.py")
    monkeypatch.setattr(runtime, "_isolation", None)
    module = runtime.isolation()
    assert module.ACTIVE is None and module.service_spawn() == {} and not module.own_record("x")
    with module.agent_files():
        pass
    assert runtime.isolate(SimpleNamespace(effective_data_dir=tmp_path), SimpleNamespace(pw_uid=1, pw_gid=1)) is None


def test_isolate_off_leaves_data_untouched(runtime, data, monkeypatch, capsys):
    root, _ops = data
    monkeypatch.delenv("AGENT_ISOLATION", raising=False)
    before = {p: os.lstat(p) for p in [root, *root.rglob("*")]}
    account = SimpleNamespace(pw_uid=os.getuid() or 1000, pw_gid=os.getgid() or 1000)
    assert runtime.isolate(SimpleNamespace(effective_data_dir=root), account) is None
    for path, info in before.items():
        after = os.lstat(path)
        assert (after.st_uid, after.st_gid, after.st_mode, after.st_mtime_ns) == (
            info.st_uid, info.st_gid, info.st_mode, info.st_mtime_ns)
    assert not (root / "agent-isolation.json").exists()
    assert capsys.readouterr().err == ""


def test_isolate_reports_a_bad_service_id_only_when_on(runtime, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AGENT_ISOLATION_SERVICE_UID", "1000")
    account = SimpleNamespace(pw_uid=1000, pw_gid=1000)
    monkeypatch.delenv("AGENT_ISOLATION", raising=False)
    assert runtime.isolate(SimpleNamespace(effective_data_dir=tmp_path), account) is None
    assert capsys.readouterr().err == ""
    monkeypatch.setenv("AGENT_ISOLATION", "on")
    assert runtime.isolate(SimpleNamespace(effective_data_dir=tmp_path), account) is None
    assert "could NOT be enabled" in capsys.readouterr().err


def test_no_handover_while_isolated(runtime, monkeypatch):
    module = runtime.isolation()
    monkeypatch.setattr(module, "ACTIVE", IDENT)
    for name in ("SHUFFLE_HANDED_OVER", "SHUFFLE_SOURCE_SUPERVISOR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(runtime, "load_updater", lambda: pytest.fail("no hand-over while isolated"))
    assert runtime.handover(SimpleNamespace(effective_data_dir="/nonexistent")) is False


def test_start_app_on_uses_the_launcher(runtime, monkeypatch, tmp_path):
    module = runtime.isolation()
    monkeypatch.setattr(module, "ACTIVE", IDENT)
    seen = []
    monkeypatch.setattr(module, "ensure_traversable", lambda root, log: seen.append(root))
    calls = []
    monkeypatch.setattr(runtime.subprocess, "Popen", lambda *a, **k: calls.append((a, k)))
    runtime.start_app(["/py", "-m", "app.main"], None, {}, tmp_path)
    assert calls[0][0][0] == ["/py", "-s", "-c", module.APP_SHIM] and seen == [tmp_path]


# --- the app's side ----------------------------------------------------------------


def test_private_storage_is_unchanged_when_off(monkeypatch, tmp_path):
    from app.core import private_storage

    monkeypatch.delenv(private_storage.AGENT_ISOLATION_FLAG, raising=False)
    assert private_storage.owner_uid() == os.geteuid()
    with private_storage.service_files():
        with private_storage.service_files():
            assert private_storage.owner_uid() == os.geteuid()
    # On, but without a saved service id (tests, a non-Docker install): unchanged too.
    monkeypatch.setenv(private_storage.AGENT_ISOLATION_FLAG, "on")
    assert private_storage._service_ids() is None
    with private_storage.service_files():
        assert private_storage.owner_uid() == os.geteuid()
    path = tmp_path / "secret"
    path.write_text("x")
    private_storage.protect_private_path(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_service_files_switches_and_restores_file_ids(monkeypatch):
    from app.core import private_storage

    monkeypatch.setattr(private_storage, "_service_ids", lambda: (64000, 64001))
    switched = []
    monkeypatch.setattr(private_storage, "_set_file_ids", lambda uid, gid: switched.append((uid, gid)))
    with private_storage.service_files():
        assert private_storage.owner_uid() == 64000
        with private_storage.service_files():
            assert private_storage.owner_uid() == 64000
        assert private_storage.owner_uid() == 64000
    assert private_storage.owner_uid() == os.geteuid()
    assert switched == [(64000, 64001), (os.geteuid(), os.getegid())]


def test_ledger_records_the_command_the_runner_becomes():
    from app.core import process_ledger
    from fluid.tools import agent_runner

    argv, _cwd, _env = agent_runner.wrap(["sh", "-c", "sleep 1"], "/tmp", {"PATH": os.defpath},
                                         environ={agent_runner.FLAG: "on", agent_runner.ROOTS: "/tmp"})
    starting = SimpleNamespace(cmdline=lambda: argv)
    running = SimpleNamespace(cmdline=lambda: ["sh", "-c", "sleep 1"])
    assert process_ledger._cmdline(starting) == process_ledger._cmdline(running) == "sh -c sleep 1"
