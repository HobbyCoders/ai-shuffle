"""Container acceptance must use the same identity as the app reader."""
from unittest.mock import Mock

from scripts.verify_container_postgres import Check


def test_removed_env_probe_runs_as_managed_app_user():
    harness = Mock()
    harness.sql.side_effect = ["ai_shuffle", "127.0.0.1"]
    Check.removed_environment_regression(harness)
    args = harness.docker.call_args.args
    assert args[:3] == ("exec", "--user", "99:100")
    assert args[3] is harness.launch.return_value
    assert args[4:6] == ("python", "-c")


def test_isolation_probe_materializes_lazy_keys_through_app_producers():
    from scripts.ci.verify_agent_isolation import IsolationCheck

    harness = Mock()
    IsolationCheck.prepare_protected_keys(harness, "disposable-container")
    args = harness.docker.call_args.args
    assert args[:6] == ("exec", "--user", "99:100", "--workdir", "/app", "disposable-container")
    assert args[6:8] == ("python", "-c")
    assert "preview_access._key()" in args[8]
    assert "managed_credentials._key(create=True)" in args[8]


def test_required_probe_prepares_the_bypassed_entrypoint_account(tmp_path, monkeypatch):
    """The inspection container has image UID 1000, not entrypoint PUID 99."""
    import importlib.util
    import os
    from pathlib import Path
    import subprocess
    import sys
    from types import SimpleNamespace

    import pytest
    from scripts.ci.verify_agent_isolation import IsolationCheck

    source = Path(__file__).resolve().parents[1] / "docker/agent_isolation.py"
    spec = importlib.util.spec_from_file_location("agent_isolation", source)
    iso = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(iso)
    monkeypatch.setitem(sys.modules, "agent_isolation", iso)
    account = SimpleNamespace(pw_uid=1000, pw_gid=1000)
    monkeypatch.setattr(iso.pwd, "getpwnam", lambda name: account)
    calls = []

    def remap(argv, **kwargs):
        assert kwargs.get("check") is True
        calls.append(argv)
        if argv[0] == "groupmod":
            account.pw_gid = int(argv[argv.index("-g") + 1])
        elif argv[0] == "usermod":
            if "-u" in argv:
                # Match the entrypoint: suppress usermod's implicit home walk.
                assert argv[argv.index("-d") + 1] == "/"
                account.pw_uid = int(argv[argv.index("-u") + 1])
        else:
            pytest.fail(f"unexpected account setup command: {argv[0]}")

    monkeypatch.setattr(subprocess, "run", remap)
    monkeypatch.setattr(os, "chown", lambda *args, **kwargs: None)

    class ReachedMigration(Exception):
        pass

    def bootstrap(root, ident, **kwargs):
        assert (account.pw_uid, account.pw_gid) == (ident.puid, ident.pgid)
        assert (ident.puid, ident.pgid) == (99, 100)
        assert calls, "inspection must explicitly prepare the image account"
        # Retained database metadata must not consume the key-failure counter.
        ops = kwargs["ops"]
        keys = root / ".credential-keys"
        for path in (root / "database-mode.json", keys):
            ops.lchown(path, 64000, 64000)
        assert ops.moved == 0
        ops.lchown(keys / "first", 64000, 64000)
        assert ops.moved == 1
        with pytest.raises(OSError, match="injected disposable migration failure"):
            ops.lchown(keys / "second", 64000, 64000)
        assert ops.moved == 2
        raise ReachedMigration

    monkeypatch.setattr(iso, "bootstrap", bootstrap)
    harness = Mock()

    def inspect_volume(volume, code):
        # The interrupted migration must start from a real retained database;
        # a keys-only volume correctly fails the runtime's legacy-data guard.
        seed = harness.launch.return_value
        harness.launch.assert_called_once_with(volume, env={"AGENT_ISOLATION": "off"})
        harness.ready.assert_called_once_with(seed)
        harness.prepare_protected_keys.assert_called_once_with(seed)
        harness.stop.assert_called_once_with(seed)
        assert "CREATE TABLE required_migration_check" in harness.sql.call_args.args[1]
        # Execute the actual producer up to migration, confined to scratch data.
        code = code.replace("Path('/data')", f"Path({str(tmp_path)!r})")
        with pytest.raises(ReachedMigration):
            exec(compile(code, "<required-isolation-probe>", "exec"), {})
        raise ReachedMigration

    harness.inspect_volume.side_effect = inspect_volume
    with pytest.raises(ReachedMigration):
        IsolationCheck.required_mode(harness)
