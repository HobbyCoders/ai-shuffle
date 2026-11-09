"""Saved Claude subscription sign-ins: one CLI config folder per extra sign-in.

No real CLI, credentials or database: ``claude auth status`` is faked per
folder, the worker-local setting lives in a dict, and every folder is under
``tmp_path``. Nothing here opens a CLI credential file.
"""
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from app.core import claude_auth_mode, claude_cli_login
from app.core import claude_cli_home as home
from app.core import claude_cli_status as cs
from app.core import claude_subscriptions as subs
from app.network.policy import is_shared_setting_key

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink layout")


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A default CLI folder, an app data root and a fake per-folder CLI."""
    base = tmp_path / "home" / ".claude"
    (base / "projects" / "-repo").mkdir(parents=True)
    (base / "projects" / "-repo" / "chat.jsonl").write_text("{}\n")
    (base / "settings.json").write_text('{"model": "opus"}\n')
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    root = tmp_path / "data" / home.ROOT_NAME
    monkeypatch.setattr(home, "root", lambda: root)

    values = {}
    for name in ("get_system_setting", "get_system_setting_cached"):
        monkeypatch.setattr(subs.database, name, lambda key, *a, **k: values.get(key))
    monkeypatch.setattr(subs.database, "set_system_setting", values.__setitem__)
    monkeypatch.setattr(subs, "setting_key", lambda: "claude_cli_logins:test-worker")
    monkeypatch.setattr(subs, "_verified", {})
    monkeypatch.setattr(subs, "_identity_cache", {})
    monkeypatch.setattr(subs, "_last_pending_probe", 0.0)
    monkeypatch.setattr(subs, "_reap_due", False)

    # Who each folder is signed in as; the default folder is keyed by "".
    signed = {"": "default@example.com"}
    probed, ran = [], []

    def probe_home(home_env, resolver=None):
        folder = home_env.get("CLAUDE_CONFIG_DIR", "")
        probed.append(folder)
        email = signed.get(folder)
        return cs.ClaudeCliStatus(installed=True, signed_in=bool(email), email=email,
                                  plan="max" if email else None, checked_at=subs.time.time())

    def run_home(args, home_env):
        ran.append((args, dict(home_env)))
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(cs, "probe_home", probe_home)
    monkeypatch.setattr(cs, "run_home", run_home)
    monkeypatch.setattr(cs, "announce_account_change", lambda: None)
    monkeypatch.setattr(claude_cli_login, "terminal_supported", lambda: False)
    monkeypatch.setattr(claude_auth_mode, "current_mode", lambda: claude_auth_mode.MODE_SUBSCRIPTION)
    return SimpleNamespace(base=base, root=root, values=values, signed=signed, probed=probed, ran=ran)


def _finish_pending(world, email):
    """The owner completes `claude auth login` in the pending folder."""
    pending = subs._load()["pending"]
    world.signed[str(home.account_dir(pending["id"]))] = email
    subs._last_pending_probe = 0.0
    return pending["id"]


def _add(world, email):
    login = subs.start_login()
    account_id = _finish_pending(world, email)
    assert login["id"] == account_id
    subs.payload()
    return account_id


# ─── Folder layout ───────────────────────────────────────────────────────────

def test_folder_links_shared_state_back_to_the_default_folder(world):
    account_id = "0123456789abcdef"
    report = home.ensure(account_id)
    folder = world.root / account_id
    assert report["exists"] and report["shared"]["projects"] and report["shared"]["settings.json"]
    assert os.path.islink(folder / "projects") and os.path.islink(folder / "settings.json")
    # Transcripts and settings resolve to the shared ones; a write through the
    # link lands in the default folder.
    assert (folder / "projects" / "-repo" / "chat.jsonl").read_text() == "{}\n"
    (folder / "projects" / "-repo" / "new.jsonl").write_text("x")
    assert (world.base / "projects" / "-repo" / "new.jsonl").exists()
    assert (folder / "settings.json").read_text() == '{"model": "opus"}\n'
    # Missing shared folders are created in the default folder (no fork later).
    assert (world.base / "agents").is_dir() and os.path.islink(folder / "agents")
    # Absent optional files are not linked yet; nothing else is created.
    assert not os.path.lexists(folder / "CLAUDE.md")
    assert not os.path.lexists(folder / ".claude.json")
    assert home.ensure(account_id) == report  # idempotent
    assert home.intact(account_id)
    assert home.env_for(None) == {}
    assert home.env_for(account_id) == {"CLAUDE_CONFIG_DIR": str(folder)}


def test_settings_file_is_created_shared_when_the_default_has_none(world):
    (world.base / "settings.json").unlink()
    home.ensure("0123456789abcdef")
    assert (world.base / "settings.json").read_text() == "{}\n"
    assert os.path.islink(world.root / "0123456789abcdef" / "settings.json")


def test_windows_without_file_symlinks_keeps_its_own_settings(world, monkeypatch):
    """No copy or hard link that could silently diverge: the file is reported unshared."""
    real_symlink = os.symlink
    monkeypatch.setattr(home.sys, "platform", "win32")
    # Stands in for a junction; the file symlink below is what Windows refuses.
    monkeypatch.setattr(home, "_make_dir_link", lambda link, target: real_symlink(target, link, True))

    def symlink(src, dst, target_is_directory=False):
        if not target_is_directory:
            raise OSError("A required privilege is not held by the client")
        return real_symlink(src, dst, target_is_directory=True)
    monkeypatch.setattr(home.os, "symlink", symlink)
    report = home.ensure("0123456789abcdef")
    folder = world.root / "0123456789abcdef"
    assert report["shared"]["projects"] is True
    assert report["shared"]["settings.json"] is False
    assert not os.path.lexists(folder / "settings.json")


def test_remove_never_follows_links(world, tmp_path):
    account_id = "0123456789abcdef"
    home.ensure(account_id)
    folder = world.root / account_id
    (folder / ".claude.json").write_text("{}")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    (folder / "nested").mkdir()
    os.symlink(outside, folder / "nested" / "escape", target_is_directory=True)
    home.remove(account_id)
    assert not os.path.lexists(folder)
    assert (world.base / "projects" / "-repo" / "chat.jsonl").exists()
    assert (world.base / "settings.json").exists()
    assert (outside / "keep.txt").read_text() == "keep"
    for bad in ("../x", "", "ABCDEF0123456789", "0123456789abcdef/.."):
        with pytest.raises(home.HomeError):
            home.remove(bad)


def test_remove_refuses_a_linked_root_and_a_linked_folder_is_only_unlinked(world, tmp_path):
    account_id = "0123456789abcdef"
    world.root.mkdir(parents=True)
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "data.txt").write_text("keep")
    os.symlink(victim, world.root / account_id, target_is_directory=True)
    with pytest.raises(home.HomeError):
        home.ensure(account_id)
    home.remove(account_id)
    assert not os.path.lexists(world.root / account_id)
    assert (victim / "data.txt").read_text() == "keep"


# ─── Saving, switching, deleting ─────────────────────────────────────────────

def test_add_another_signs_in_to_a_fresh_folder_and_becomes_active(world):
    login = subs.start_login()
    folder = str(world.root / login["id"])
    assert login["terminal"] is False and login["env"] == {"CLAUDE_CONFIG_DIR": folder}
    assert folder in login["commands"]["powershell"] and folder in login["commands"]["posix"]
    assert home.intact(login["id"])
    waiting = subs.payload()
    assert waiting["login_pending"] and waiting["subscription"] == [] and waiting["default_in_use"]
    assert subs.active_home_env() == {}

    _finish_pending(world, "second@example.com")
    done = subs.payload()
    assert done["login_pending"] is False and done["login"] is None
    [row] = done["subscription"]
    assert row["kind"] == "saved" and row["active"] and row["email"] == "second@example.com"
    assert row["plan"] == "max" and row["label"] == "second@example.com"
    assert done["default_in_use"] is False and done["recovery_required"] is False
    assert subs.active_home_env() == {"CLAUDE_CONFIG_DIR": folder}
    assert subs._reap_due is True
    # Identity only ever came from `claude auth status` against the folders.
    assert set(world.probed) <= {folder}


def test_every_claude_spawn_gets_the_active_folder(world, monkeypatch):
    default_fingerprint = claude_auth_mode.spawn_fingerprint()
    account_id = _add(world, "second@example.com")
    folder = str(world.root / account_id)
    expected = {"CLAUDE_CONFIG_DIR": folder}

    from app.core import claude_cli_install
    from app.core.environment import inject_claude_code_auth
    assert inject_claude_code_auth({})["CLAUDE_CONFIG_DIR"] == folder
    # A non-Anthropic provider owns its spawn env; the folder is not forced on it.
    assert "CLAUDE_CONFIG_DIR" not in inject_claude_code_auth({}, "provider-1")
    assert cs._probe_env()["CLAUDE_CONFIG_DIR"] == folder
    assert claude_cli_login._login_env()["CLAUDE_CONFIG_DIR"] == folder
    assert "CLAUDE_CONFIG_DIR" not in claude_cli_login._login_env({})  # explicit default folder
    assert claude_cli_install._installer_env()["CLAUDE_CONFIG_DIR"] == folder
    assert subs.apply_cli_home({"PATH": "/bin"}) == {"PATH": "/bin", **expected}
    assert claude_auth_mode.spawn_fingerprint() != default_fingerprint

    # `claude auth logout` signs out the active folder only.
    from app.core.auth import AuthService
    calls = []
    monkeypatch.setattr(cs, "get_status", lambda *a, **k: cs.ClaudeCliStatus(
        installed=True, signed_in=True, binary_path="/usr/bin/claude"))
    monkeypatch.setattr("app.core.auth.subprocess.run",
                        lambda argv, **kw: calls.append(kw["env"]) or subprocess.CompletedProcess(argv, 0, "", ""))
    assert AuthService().claude_logout()["success"] is True
    assert calls[0]["CLAUDE_CONFIG_DIR"] == folder

    # Back on the default folder, spawns inherit the worker's own setting.
    default_id = subs.save_current("Personal")
    subs.change(default_id, "activate")
    assert "CLAUDE_CONFIG_DIR" not in inject_claude_code_auth({})
    assert "CLAUDE_CONFIG_DIR" not in cs._probe_env()
    assert claude_auth_mode.spawn_fingerprint() == default_fingerprint


def test_transcripts_are_shared_across_sign_ins(world):
    from app.core import cli_sessions
    from app.core.config import settings
    from app.core.provider import fluid_transcript

    account_id = _add(world, "second@example.com")
    env = subs.active_home_env()
    account_root = fluid_transcript.cli_projects_root(env)
    assert account_root == world.root / account_id / "projects"
    assert account_root.resolve() == (world.base / "projects").resolve()
    (account_root / "-repo" / "after-switch.jsonl").write_text("{}\n")
    if settings.claude_projects_dir is None:
        assert (cli_sessions._projects() / "-repo" / "after-switch.jsonl").exists()
    assert (world.base / "projects" / "-repo" / "after-switch.jsonl").exists()


def test_payload_refreshes_expired_credentials_and_invalidates_on_relogin(world, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(subs.time, "monotonic", lambda: now[0])
    account_id = _add(world, "saved@example.com")
    folder = str(world.root / account_id)
    assert subs.payload()["subscription"][0]["needs_sign_in"] is False
    world.signed.pop(folder)
    now[0] += subs._VERIFY_INTERVAL_SECONDS + 1
    row = subs.payload()["subscription"][0]
    assert row["needs_sign_in"] is True
    assert row["signed_in"] is False
    assert subs.payload()["recovery_required"] is True
    world.signed[folder] = "saved@example.com"
    subs.invalidate_identity()
    assert subs.payload()["subscription"][0]["needs_sign_in"] is False


def test_listing_keeps_latest_signed_out_result_after_expiry_then_timeout(world, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(subs.time, "monotonic", lambda: now[0])
    account_id = _add(world, "saved@example.com")
    folder = str(world.root / account_id)
    world.signed.pop(folder)
    now[0] += subs._VERIFY_INTERVAL_SECONDS + 1
    signed_out = subs.payload()
    assert signed_out["subscription"][0]["signed_in"] is False
    assert signed_out["recovery_required"] is True
    # No activation or manually seeded account fields between these listings.
    now[0] += subs._VERIFY_INTERVAL_SECONDS + 1
    monkeypatch.setattr(subs, "_probe", lambda _: cs.ClaudeCliStatus(
        installed=True, signed_in=None, error="timeout"))
    unknown = subs.payload()
    assert unknown["subscription"][0]["signed_in"] is False
    assert unknown["subscription"][0]["needs_sign_in"] is True
    assert unknown["recovery_required"] is True
    assert unknown["subscription"][0]["checked_at"] == signed_out["subscription"][0]["checked_at"]


@pytest.mark.parametrize("signed_in", [False, True])
@pytest.mark.parametrize("active", [False, True])
def test_indeterminate_identity_keeps_last_readable_authentication(world, monkeypatch, signed_in, active):
    account_id = _add(world, "saved@example.com")
    if not signed_in:
        world.signed.pop(str(world.root / account_id))
    monkeypatch.setattr(subs.time, "time", lambda: 123.0)
    # Produce the stored auth state through the real activation/refresh path,
    # rather than seeding the state that the listing will consume.
    subs.change(account_id, "activate")
    if not active:
        _add(world, "other@example.com")
    subs.invalidate_identity()
    monkeypatch.setattr(subs, "_probe", lambda _: cs.ClaudeCliStatus(
        installed=True, signed_in=None, error="timeout"))

    listing = subs.payload()
    row = next(row for row in listing["subscription"] if row["id"] == account_id)
    assert row["signed_in"] is signed_in
    assert row["needs_sign_in"] is (not signed_in)
    assert row["checked_at"] == 123.0
    assert row["email"] == "saved@example.com"
    assert listing["recovery_required"] is (active and not signed_in)


@pytest.mark.parametrize("reader", ["payload", "activate", "save_current", "pending_poll", "pending_cancel"])
def test_login_completion_rejects_an_inflight_identity_probe(world, monkeypatch, reader):
    if reader.startswith("pending_"):
        account_id = subs.start_login()["id"]
    elif reader == "save_current":
        account_id = None
    else:
        account_id = _add(world, "before@example.com")
    subs.invalidate_identity()
    probe_started, release_probe = Event(), Event()
    probes = []

    def probe(_):
        probes.append(1)
        if len(probes) == 1:
            probe_started.set()
            assert release_probe.wait(5), "the old identity probe was not released"
            email = "before@example.com"
        else:
            email = "after@example.com"
        return cs.ClaudeCliStatus(installed=True, signed_in=True, email=email)

    monkeypatch.setattr(subs, "_probe", probe)
    # Complete the real terminal reader path while the listing/activation holds
    # the subscription lock. The terminal must not acquire that lock to invalidate.
    terminal = claude_cli_login.LoginTerminal()
    terminal._proc = SimpleNamespace(take_output=lambda: b"", running=False,
                                     wait=lambda _: None, exit_code=0)

    def read_identity():
        with subs._lock:
            if reader in ("payload", "pending_poll"):
                return subs.payload()
            if reader == "save_current":
                return subs.save_current()
            if reader == "pending_cancel":
                return subs.cancel_login()
            return subs.change(account_id, "activate")

    with ThreadPoolExecutor(max_workers=2) as pool:
        inflight = pool.submit(read_identity)
        try:
            assert probe_started.wait(5), "the old identity probe did not start"
            completed = pool.submit(terminal.read)
            assert completed.result(timeout=5)["succeeded"] is True
        finally:
            release_probe.set()
        result = inflight.result(timeout=5)
        if reader in ("payload", "pending_poll"):
            assert result["subscription"][0]["email"] == "after@example.com"

    row = subs.payload()["subscription"][0]
    assert row["email"] == "after@example.com"
    if reader in ("save_current", "pending_poll", "pending_cancel"):
        # New accounts must derive their labels from the guarded result too.
        assert row["label"] == "after@example.com"
        assert subs._load()["accounts"][0]["email"] == "after@example.com"
    assert subs._identity_cache[account_id][1].email == "after@example.com"
    # Adoption changes the active folder and deliberately invalidates again.
    assert len(probes) == (3 if reader.startswith("pending_") else 2)


def test_cached_listing_keeps_probe_time_without_rewriting_storage(world, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(subs.time, "monotonic", lambda: now[0])
    _add(world, "saved@example.com")
    checked_at = subs.payload()["subscription"][0]["checked_at"]
    original_save = subs._save
    writes = []

    def save(state):
        writes.append(1)
        original_save(state)

    monkeypatch.setattr(subs, "_save", save)
    monkeypatch.setattr(subs.time, "time", lambda: checked_at + 5)
    assert subs.payload()["subscription"][0]["checked_at"] == checked_at
    assert subs.payload()["subscription"][0]["checked_at"] == checked_at
    assert writes == []
    now[0] += subs._VERIFY_INTERVAL_SECONDS + 1
    assert subs.payload()["subscription"][0]["checked_at"] == checked_at + 5
    assert writes == [1]
    assert subs.payload()["subscription"][0]["checked_at"] == checked_at + 5
    assert writes == [1]


def test_inactive_saved_login_commands_never_switch_accounts(world):
    first = _add(world, "first@example.com")
    second = _add(world, "second@example.com")
    before = subs._load()
    login = subs.sign_in_commands(first)
    assert login["terminal"] is False
    assert login["env"] == {"CLAUDE_CONFIG_DIR": str(world.root / first)}
    assert "claude auth login" in login["commands"]["posix"]
    assert subs._load() == before and before["active_id"] == second
    with pytest.raises(subs.SubscriptionError) as missing:
        subs.sign_in_commands("../outside")
    assert missing.value.status == 404


def test_failed_folder_deletion_is_retried_at_startup(world, monkeypatch):
    account_id = _add(world, "saved@example.com")
    real_remove = home.remove
    monkeypatch.setattr(home, "remove", lambda _: (_ for _ in ()).throw(PermissionError("locked")))
    with pytest.raises(subs.SubscriptionError, match="could not be deleted"):
        subs.change(account_id, "delete")
    assert not subs._load()["accounts"]
    assert subs._load()["cleanup"] == [account_id]
    monkeypatch.setattr(home, "remove", real_remove)
    subs.repair_on_startup()
    assert not (world.root / account_id).exists()
    assert subs._load()["cleanup"] == []
    assert (world.base / "projects" / "-repo" / "chat.jsonl").exists()


def test_delete_readonly_directory_does_not_chmod_shared_target(world, monkeypatch):
    account_id = _add(world, "saved@example.com")
    folder = world.root / account_id
    backup = folder / ".claude.json.backup"
    backup.write_text("opaque CLI-owned backup")
    real_rmdir = os.rmdir
    attempted = []

    def readonly_once(path):
        if path == folder and not attempted:
            attempted.append(path)
            raise PermissionError("read-only directory")
        return real_rmdir(path)

    monkeypatch.setattr(home.os, "rmdir", readonly_once)
    subs.change(account_id, "delete")
    assert attempted == [folder]
    assert not folder.exists()
    assert (world.base / "projects" / "-repo" / "chat.jsonl").exists()


def test_one_active_at_a_time_and_delete_rules(world):
    default_id = subs.save_current("Personal")
    listing = subs.payload()
    assert [(r["id"], r["kind"], r["active"]) for r in listing["subscription"]] == [
        (default_id, "default", True)]
    first = _add(world, "first@example.com")
    second = _add(world, "second@example.com")
    rows = {r["id"]: r["active"] for r in subs.payload()["subscription"]}
    assert rows == {default_id: False, first: False, second: True}

    subs.change(first, "activate")
    rows = {r["id"]: r["active"] for r in subs.payload()["subscription"]}
    assert rows == {default_id: False, first: True, second: False}
    subs.change(first, "rename", "Work")
    assert next(r for r in subs.payload()["subscription"] if r["id"] == first)["label"] == "Work"

    # Deleting the active saved sign-in: the CLI signs itself out of that
    # folder, the folder goes, and Claude Code returns to the default folder
    # without activating the other saved sign-in.
    first_folder = world.root / first
    subs.change(first, "delete")
    assert world.ran == [(["auth", "logout"], {"CLAUDE_CONFIG_DIR": str(first_folder)})]
    assert not os.path.lexists(first_folder)
    after = subs.payload()
    assert after["default_in_use"] is True
    rows = {r["id"]: r["active"] for r in after["subscription"]}
    assert rows == {default_id: True, second: False}
    assert subs.active_home_env() == {}

    # Forgetting the default entry never signs the CLI out or touches its folder.
    subs.change(default_id, "delete")
    assert len(world.ran) == 1
    assert (world.base / "settings.json").exists() and (world.base / "projects").is_dir()
    assert [r["id"] for r in subs.payload()["subscription"]] == [second]

    with pytest.raises(subs.SubscriptionError) as missing:
        subs.change("feedfacefeedface", "activate")
    assert missing.value.status == 404


def test_save_current_needs_a_signed_in_default(world):
    world.signed.pop("")
    with pytest.raises(subs.SubscriptionError) as exc:
        subs.save_current()
    assert exc.value.code == "not_signed_in"


def test_cancel_login_removes_the_unfinished_folder(world):
    login = subs.start_login()
    folder = world.root / login["id"]
    assert folder.is_dir()
    subs.cancel_login()
    assert not os.path.lexists(folder)
    assert subs.payload()["login_pending"] is False
    assert (world.base / "projects" / "-repo" / "chat.jsonl").exists()


def test_in_app_terminal_login_targets_the_new_folder(world, monkeypatch):
    started, cancelled = [], []
    run = {"running": True, "succeeded": None}

    class Terminal:
        def start(self, binary=None, *, home_env=None, tag=None):
            started.append((home_env, tag))
            return run

        def read_for(self, tag):
            return dict(run) if started and tag == started[-1][1] else None

        def cancel_for(self, tag):
            cancelled.append(tag)

    monkeypatch.setattr(claude_cli_login, "terminal_supported", lambda: True)
    monkeypatch.setattr(claude_cli_login, "login_terminal", Terminal())
    login = subs.start_login()
    folder = str(world.root / login["id"])
    assert started == [({"CLAUDE_CONFIG_DIR": folder}, "add-subscription:" + login["id"])]
    assert login["terminal"] is True and login["commands"] is None and login["status"] == "running"
    _finish_pending(world, "second@example.com")
    assert subs.payload()["login_pending"] is True  # still running: not probed yet
    run.update(running=False, succeeded=True)
    done = subs.payload()
    assert done["login_pending"] is False and done["subscription"][0]["active"]
    assert subs.active_home_env() == {"CLAUDE_CONFIG_DIR": folder}

    # A failed run is reported and can be cancelled, which cleans up.
    run.update(running=False, succeeded=False)
    login = subs.start_login()
    subs._last_pending_probe = 0.0
    assert subs.payload()["login"]["status"] == "failed"
    subs.cancel_login()
    assert cancelled == ["add-subscription:" + login["id"]]
    assert not os.path.lexists(world.root / login["id"])


def test_pending_login_whose_run_is_gone_fails_and_cancel_cleans_up(world, monkeypatch):
    """The tagged run was stopped elsewhere: the pending sign-in must not wait forever."""
    run = {"state": {"running": True, "succeeded": None}}

    class Terminal:
        def start(self, binary=None, *, home_env=None, tag=None):
            return run["state"]

        def read_for(self, tag):
            return None if run["state"] is None else dict(run["state"])

        def cancel_for(self, tag):
            pass

    monkeypatch.setattr(claude_cli_login, "terminal_supported", lambda: True)
    monkeypatch.setattr(claude_cli_login, "login_terminal", Terminal())
    login = subs.start_login()
    assert subs.payload()["login"]["status"] == "running"
    run["state"] = None  # e.g. the regular view's Stop reset the terminal
    subs._last_pending_probe = 0.0
    listing = subs.payload()
    assert listing["login"]["status"] == "failed" and listing["login"]["error"]
    subs.cancel_login()
    assert not os.path.lexists(world.root / login["id"])
    assert subs.payload()["login_pending"] is False


def test_cancel_keeps_a_sign_in_that_already_finished(world):
    """The CLI finished before the poll adopted it: cancel saves it instead of deleting it."""
    login = subs.start_login()
    folder = world.root / login["id"]
    world.signed[str(folder)] = "late@example.com"
    # The 3 s poll has not run since the sign-in finished.
    subs._last_pending_probe = float("inf")
    assert subs.payload()["login_pending"] is True
    assert subs.cancel_login() == login["id"]
    assert folder.is_dir() and home.intact(login["id"])
    listing = subs.payload()
    assert listing["login_pending"] is False
    [row] = listing["subscription"]
    assert row["id"] == login["id"] and row["active"] and row["email"] == "late@example.com"
    assert subs.active_home_env() == {"CLAUDE_CONFIG_DIR": str(folder)}
    assert subs._reap_due is True
    # With nothing pending, cancel is a no-op.
    assert subs.cancel_login() is None


def test_commands_quote_a_data_dir_with_an_apostrophe(world, tmp_path, monkeypatch):
    root = tmp_path / "Bob's data" / home.ROOT_NAME
    monkeypatch.setattr(home, "root", lambda: root)
    login = subs.start_login()
    folder = str(root / login["id"])
    commands = login["commands"]
    quoted = folder.replace("'", "''")
    assert commands["powershell"] == f"$env:CLAUDE_CONFIG_DIR='{quoted}'; claude auth login"
    assert commands["cmd"] == f'set "CLAUDE_CONFIG_DIR={folder}" && claude auth login'
    # A real POSIX shell gets exactly the folder back.
    probe = commands["posix"].replace("claude auth login", "printenv CLAUDE_CONFIG_DIR")
    out = subprocess.run(["sh", "-c", probe], capture_output=True, text=True, check=True).stdout
    assert out.rstrip("\n") == folder
    # PowerShell's typographic quotes are single quotes too; cmd cannot quote % or ".
    assert subs._quote_powershell("a\u2019b") == "'a\u2019\u2019b'"
    for bad in ('/data/50%/x', '/data/a"b', '/data/a!b'):
        assert subs._cmd_command(bad) is None


def test_delete_drops_the_entry_before_removing_its_folder(world, monkeypatch):
    """A spawn during or after a delete never recreates the deleted folder."""
    account_id = _add(world, "second@example.com")
    folder = world.root / account_id
    seen = []

    def run_home(args, home_env):
        # The folder is still there, but the list no longer has the entry and
        # a spawn now uses the default folder.
        seen.append((account_id in [a["id"] for a in subs._load()["accounts"]],
                     subs._load()["active_id"], subs.active_home_env()))
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(cs, "run_home", run_home)
    subs.change(account_id, "delete")
    assert seen == [(False, None, {})]
    assert not os.path.lexists(folder)
    # A spawn that read the old active entry just before the delete re-checks
    # under the folder lock and does not bring the folder back.
    real = subs.active_home_id
    stale = iter([account_id])
    monkeypatch.setattr(subs, "active_home_id", lambda: next(stale, None) or real())
    assert subs.active_home_env() == {}
    assert not os.path.lexists(folder)


def test_claude_off_mode_does_not_point_spawns_at_a_saved_folder(world, monkeypatch):
    from app.core.environment import inject_claude_code_auth
    _add(world, "second@example.com")
    assert "CLAUDE_CONFIG_DIR" in inject_claude_code_auth({})
    monkeypatch.setattr(claude_auth_mode, "current_mode", lambda: claude_auth_mode.MODE_OFF)
    assert "CLAUDE_CONFIG_DIR" not in inject_claude_code_auth({})


def test_missing_active_folder_is_repaired_and_flagged(world):
    account_id = _add(world, "second@example.com")
    folder = world.root / account_id
    home.remove(account_id)
    world.signed.pop(str(folder))
    subs._verified.clear()
    # The spawn still uses that folder (never another account), rebuilt with
    # its shared links, and the CLI now reports no sign-in there.
    assert subs.active_home_env() == {"CLAUDE_CONFIG_DIR": str(folder)}
    assert home.intact(account_id)
    subs.change(account_id, "activate")
    listing = subs.payload()
    assert listing["recovery_required"] is True
    assert listing["subscription"][0]["needs_sign_in"] is True


def test_list_is_worker_local_and_never_synced(world):
    assert not is_shared_setting_key("claude_cli_logins:any-worker")
    assert not is_shared_setting_key("claude_cli_logins")
    _add(world, "second@example.com")
    assert list(world.values) == ["claude_cli_logins:test-worker"]
    stored = world.values["claude_cli_logins:test-worker"]
    assert "CLAUDE_CONFIG_DIR" not in stored and "token" not in stored.lower()


def test_macos_reports_unavailable_neutrally_and_uses_the_default(world, monkeypatch):
    _add(world, "second@example.com")
    monkeypatch.setattr(home.sys, "platform", "darwin")
    support = subs.support()
    assert support["supported"] is False
    assert "unofficial" not in support["reason"].lower() and "unsupported" not in support["reason"].lower()
    assert subs.active_home_env() == {}
    with pytest.raises(subs.SubscriptionError) as exc:
        subs.start_login()
    assert exc.value.status == 501 and exc.value.code == "unsupported"


@pytest.mark.asyncio
async def test_switch_evicts_only_idle_sessions_on_the_old_sign_in(world, monkeypatch):
    from app.core import session_pool

    pool = session_pool.SessionPool(max_size=4)
    current = claude_auth_mode.spawn_fingerprint()

    def state(fingerprint, busy=False):
        return SimpleNamespace(connect_options=SimpleNamespace(aih_claude_auth_fingerprint=fingerprint),
                               busy=busy)
    pool._pool.update({"old-idle": state("old"), "old-busy": state("old", True),
                       "current": state(current), "provider": state(None)})
    monkeypatch.setattr(pool, "_is_busy", lambda sid, st: st.busy)
    evicted = []

    async def do_evict(sid, st, reason="evicted"):
        evicted.append((sid, reason))
        pool._pool.pop(sid)
        return sid
    monkeypatch.setattr(pool, "_do_evict", do_evict)
    monkeypatch.setattr(session_pool, "get_session_pool", lambda: pool)

    assert await subs.reap_idle_pooled_clis() == 0  # nothing switched yet
    subs._reap_due = True
    assert await subs.reap_idle_pooled_clis() == 1
    assert evicted == [("old-idle", "claude_auth")]
    assert set(pool._pool) == {"old-busy", "current", "provider"}


def test_no_credential_file_is_named_in_the_new_modules():
    root = Path(__file__).resolve().parents[2]
    for rel in ("app/core/claude_cli_home.py", "app/core/claude_subscriptions.py"):
        text = (root / rel).read_text()
        assert ".credentials" not in text and "claudeAiOauth" not in text and "oauthAccount" not in text
