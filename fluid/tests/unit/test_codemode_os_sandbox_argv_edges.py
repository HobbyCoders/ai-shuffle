"""Edge cases for the code-mode bwrap argv builder and its self-test seams.

Pure argv / path logic: nothing here launches bwrap, so the tests run the same
on hosts with and without bubblewrap.
"""

from __future__ import annotations

import os
import stat
import sys

import pytest

from fluid.codemode import os_sandbox

NO_LINKS: dict[str, str] = {}


@pytest.fixture(autouse=True)
def _fresh_cache():
    os_sandbox.reset_cache()
    yield
    os_sandbox.reset_cache()


def _binds(argv: list[str], flag: str = "--ro-bind") -> list[tuple[str, str]]:
    return [(argv[i + 1], argv[i + 2]) for i, a in enumerate(argv) if a == flag]


def _argv(**kw):
    kw.setdefault("ro_paths", [])
    kw.setdefault("link_dirs", NO_LINKS)
    return os_sandbox.build_bwrap_argv("/usr/bin/bwrap", **kw)


# ── build_bwrap_argv ─────────────────────────────────────────────────────────


def test_path_with_spaces_is_one_argv_element_each_side():
    path = "/opt/my python/lib dir"
    argv = _argv(ro_paths=[path])
    assert (path, path) in _binds(argv)
    assert all(" " not in a for a in argv if a != path)


def test_prefix_under_usr_is_left_to_the_usr_bind_but_a_sibling_is_not():
    argv = _argv(ro_paths=["/usr/lib/python3.12", "/usr", "/usrlocal/py", "/usr2"])
    binds = _binds(argv)
    assert binds.count(("/usr", "/usr")) == 1
    assert ("/usr/lib/python3.12", "/usr/lib/python3.12") not in binds
    assert ("/usrlocal/py", "/usrlocal/py") in binds
    assert ("/usr2", "/usr2") in binds


def test_duplicates_nested_and_empty_ro_paths_are_bound_once():
    argv = _argv(ro_paths=["", "/opt/venv", "/opt/venv", "/opt/venv/lib", "/opt/venv2"])
    binds = [b for b in _binds(argv) if b[0].startswith("/opt")]
    assert binds == [("/opt/venv", "/opt/venv"), ("/opt/venv2", "/opt/venv2")]


def test_ro_paths_follow_the_private_tmp():
    argv = _argv(ro_paths=["/tmp/runner dir"])
    assert argv.index("/tmp/runner dir") > argv.index("--tmpfs")
    assert argv[-2:] == ["--chdir", "/tmp"]


def test_optional_system_files_use_bind_try():
    # A host without /etc/ld.so.cache (musl, some minimal images) must not make
    # bwrap fail: every optional file is --ro-bind-try, never --ro-bind.
    argv = _argv()
    tried = [src for src, _ in _binds(argv, "--ro-bind-try")]
    assert "/etc/ld.so.cache" in tried and "/etc/localtime" in tried
    assert all(not src.startswith("/etc") for src, _ in _binds(argv))


def test_network_is_off_by_default_and_resolver_files_hidden():
    argv = _argv()
    assert "--share-net" not in argv
    tried = [src for src, _ in _binds(argv, "--ro-bind-try")]
    assert "/etc/resolv.conf" not in tried and "/etc/ssl" not in tried


def test_allow_network_shares_net_and_exposes_resolver_files():
    argv = _argv(allow_network=True)
    assert "--share-net" in argv
    assert argv.index("--share-net") > argv.index("--unshare-all")
    tried = [src for src, _ in _binds(argv, "--ro-bind-try")]
    for f in ("/etc/resolv.conf", "/etc/hosts", "/etc/nsswitch.conf", "/etc/ssl"):
        assert f in tried


def test_disable_userns_flags_and_their_fallback():
    with_flag = _argv()
    assert with_flag[1:4] == ["--unshare-all", "--unshare-user", "--disable-userns"]
    without = _argv(disable_userns=False)
    assert "--unshare-all" in without
    assert "--disable-userns" not in without and "--unshare-user" not in without
    # The rest of the isolation is identical either way.
    assert [a for a in with_flag if a not in ("--unshare-user", "--disable-userns")] == without


def test_explicit_link_dirs_become_symlinks_and_nothing_else_is_bound():
    argv = _argv(link_dirs={"/bin": "usr/bin", "/lib64": "usr/lib64"})
    links = _binds(argv, "--symlink")
    assert ("usr/bin", "/bin") in links and ("usr/lib64", "/lib64") in links
    assert not any(src in ("/sbin", "/lib", "/lib32", "/libx32") for src, _ in _binds(argv))


def test_isolation_flags_always_present():
    argv = _argv(allow_network=True, disable_userns=False)
    for flag in ("--unshare-all", "--die-with-parent", "--new-session"):
        assert flag in argv
    assert argv[argv.index("--proc") + 1] == "/proc"
    assert argv[argv.index("--dev") + 1] == "/dev"


# ── python_roots ─────────────────────────────────────────────────────────────


def _copied_venv(root, home: str | None):
    (root / "bin").mkdir(parents=True)
    exe = root / "bin" / "python"
    exe.write_text("")
    if home is not None:
        (root / "pyvenv.cfg").write_text(f"include-system-site-packages = false\nhome = {home}\n")
    return str(exe)


def test_copied_venv_binds_itself_and_its_base_outside_usr(tmp_path):
    venv = tmp_path / "my venv"
    exe = _copied_venv(venv, "/opt/py 3.12/bin")
    roots = os_sandbox.python_roots(exe)
    assert roots == [str(venv), "/opt/py 3.12"]


def test_copied_venv_with_a_base_under_usr_binds_only_itself(tmp_path):
    exe = _copied_venv(tmp_path / "v", "/usr/bin")
    assert os_sandbox.python_roots(exe) == [str(tmp_path / "v")]


def test_venv_without_cfg_or_blank_home(tmp_path):
    assert os_sandbox.python_roots(_copied_venv(tmp_path / "a", None)) == [str(tmp_path / "a")]
    b = tmp_path / "b"
    exe = _copied_venv(b, None)
    (b / "pyvenv.cfg").write_text("home =   \nversion = 3.12\n")
    assert os_sandbox.python_roots(exe) == [str(b)]


def test_symlinked_venv_resolves_to_the_target_prefix(tmp_path):
    base = tmp_path / "base"
    (base / "bin").mkdir(parents=True)
    real = base / "bin" / "python3"
    real.write_text("")
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    os.symlink(real, venv / "bin" / "python")
    (venv / "pyvenv.cfg").write_text(f"home = {base / 'bin'}\n")
    assert os_sandbox.python_roots(str(venv / "bin" / "python")) == [str(base)]


def test_system_python_under_usr_needs_no_extra_root(monkeypatch):
    monkeypatch.setattr(os_sandbox.os.path, "realpath", lambda p: p)
    monkeypatch.setattr(os_sandbox.sys, "executable", "/elsewhere/python")
    assert os_sandbox.python_roots("/usr/bin/python3") == []


def test_too_broad_roots(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home" / "me"))
    (tmp_path / "home" / "me").mkdir(parents=True)
    assert os_sandbox._too_broad("/")
    assert os_sandbox._too_broad(str(tmp_path / "home" / "me"))
    assert os_sandbox._too_broad(str(tmp_path / "home"))
    assert not os_sandbox._too_broad(str(tmp_path / "home" / "me" / "venv"))
    assert not os_sandbox._too_broad("/opt/python")


# ── self-test seams ──────────────────────────────────────────────────────────


def _fake_bwrap(directory, help_text: str) -> str:
    directory.mkdir()
    path = directory / "bwrap"
    path.write_text(f"#!/bin/sh\ncat <<'EOF'\n{help_text}\nEOF\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


@pytest.mark.skipif(sys.platform != "linux", reason="shell script stand-in for bwrap")
def test_supports_disable_userns_reads_help(tmp_path):
    new = _fake_bwrap(tmp_path / "new", "usage: bwrap\n    --disable-userns  Disable ...")
    old = _fake_bwrap(tmp_path / "old", "usage: bwrap\n    --unshare-user")
    assert os_sandbox._supports_disable_userns(new) is True
    assert os_sandbox._supports_disable_userns(old) is False
    assert os_sandbox._supports_disable_userns(str(tmp_path / "missing")) is False


def test_old_bwrap_probes_once_without_the_flag(monkeypatch):
    calls = []

    def probe(bwrap, python, disable_userns):
        calls.append(disable_userns)
        return os_sandbox.SelfTest(True, "", disable_userns)

    monkeypatch.setattr(os_sandbox, "_supports_disable_userns", lambda b: False)
    monkeypatch.setattr(os_sandbox, "_probe_once", probe)
    monkeypatch.setattr(os_sandbox, "python_roots", lambda p: ["/opt/py"])
    result = os_sandbox.self_test("/opt/py/bin/python3", bwrap="/fake/bwrap")
    assert result.ok and result.disable_userns is False
    assert calls == [False]


def test_argv_fallback_follows_the_self_test(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    plan = os_sandbox.plan_spawn(
        "required", python=sys.executable, runner=os.path.join(os_sandbox._CODEMODE_DIR, "_runner.py"),
        base_env={}, tester=lambda p: os_sandbox.SelfTest(True, "", False),
    )
    assert plan.sandboxed
    assert "--disable-userns" not in plan.argv and "--unshare-user" not in plan.argv
    assert "--unshare-all" in plan.argv


def test_reset_cache_forgets_results_and_warnings(monkeypatch, caplog):
    probes = []

    def probe(bwrap, python, disable_userns):
        probes.append(1)
        return os_sandbox.SelfTest(True, "", disable_userns)

    monkeypatch.setattr(os_sandbox, "_supports_disable_userns", lambda b: True)
    monkeypatch.setattr(os_sandbox, "_probe_once", probe)
    monkeypatch.setattr(os_sandbox, "python_roots", lambda p: ["/opt/py"])
    os_sandbox.self_test("/opt/py/bin/python3", bwrap="/fake/bwrap")
    os_sandbox.self_test("/opt/py/bin/python3", bwrap="/fake/bwrap")
    assert len(probes) == 1  # cached
    os_sandbox._warn_once("reason-x")
    assert "reason-x" in os_sandbox._warned
    os_sandbox.reset_cache()
    assert not os_sandbox._warned
    os_sandbox.self_test("/opt/py/bin/python3", bwrap="/fake/bwrap")
    assert len(probes) == 2


def test_too_broad_prefix_is_refused_without_probing(monkeypatch):
    monkeypatch.setattr(os_sandbox, "python_roots", lambda p: ["/"])
    monkeypatch.setattr(os_sandbox, "_probe_once", lambda *a: pytest.fail("probed"))
    result = os_sandbox.self_test("/x/bin/python", bwrap="/fake/bwrap")
    assert not result.ok and "expose the host" in result.reason
