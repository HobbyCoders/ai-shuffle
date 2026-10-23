"""Windows emulator paths and lifecycle, simulated without Windows or a real SDK."""
from __future__ import annotations

import asyncio
import os
import subprocess
import threading
from pathlib import PureWindowsPath
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.core import process_tree
from app.core.android_emulator import adb, host, installer, layout as layout_mod, manager as manager_mod
from app.core.android_emulator.layout import SdkLayout
from app.core.android_emulator.manager import EmulatorError, EmulatorManager

IMAGE = "system-images;android-35;google_apis;x86_64"


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(layout_mod, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(manager_mod, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(host.platform, "system", lambda: "Windows")
    monkeypatch.setattr(host.platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(process_tree, "_IS_WINDOWS", True)
    monkeypatch.setattr(host, "_accel_cache", {})


@pytest.fixture
def settings(monkeypatch, tmp_path):
    from app.core.config import settings

    monkeypatch.setattr(settings, "android_sdk_dir", None)
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    for name in ("ANDROID_HOME", "ANDROID_SDK_ROOT", "LOCALAPPDATA"):
        monkeypatch.delenv(name, raising=False)
    return settings


def test_sdk_discovery_precedence(windows, settings, monkeypatch, tmp_path):
    assert layout_mod.sdk_root() == settings.data_dir / "android-sdk"
    local = tmp_path / "Local App Data"
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    # A nonexistent Android Studio SDK does not replace the managed default.
    assert layout_mod.sdk_root() == settings.data_dir / "android-sdk"
    studio = local / "Android" / "Sdk"
    studio.mkdir(parents=True)
    assert layout_mod.discover_sdk() == studio
    root_env = tmp_path / "sdk root"
    root_env.mkdir()
    monkeypatch.setenv("ANDROID_SDK_ROOT", str(root_env))
    assert layout_mod.discover_sdk() == root_env
    home_env = tmp_path / "android home"
    home_env.mkdir()
    monkeypatch.setenv("ANDROID_HOME", str(home_env))
    assert layout_mod.discover_sdk() == home_env
    # Discovery is informational and never adopts a developer-owned SDK.
    assert layout_mod.sdk_root() == settings.data_dir / "android-sdk"
    monkeypatch.setattr(settings, "android_sdk_dir", tmp_path / "explicit")
    assert layout_mod.sdk_root() == tmp_path / "explicit"
    assert not SdkLayout().owns_root


def test_linux_sdk_discovery_unchanged(settings, monkeypatch, tmp_path):
    monkeypatch.setattr(layout_mod, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setenv("ANDROID_HOME", str(tmp_path / "external"))
    assert layout_mod.sdk_root() == settings.data_dir / "android-sdk"
    layout = SdkLayout(tmp_path)
    assert layout.adb.name == "adb" and layout.emulator_bin.name == "emulator"


def test_windows_binaries_env_and_discovery(windows, monkeypatch, tmp_path):
    layout = SdkLayout(tmp_path / "SDK with spaces")
    assert layout.emulator_bin == layout.root / "emulator" / "emulator.exe"
    assert layout.adb == layout.root / "platform-tools" / "adb.exe"
    for key in ("SystemRoot", "WINDIR", "TEMP", "TMP", "LOCALAPPDATA", "USERPROFILE"):
        monkeypatch.setenv(key, str(tmp_path / key))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-reach-child")
    env = layout.env()
    assert env["SystemRoot"] == str(tmp_path / "SystemRoot")
    assert env["TEMP"] == str(tmp_path / "TEMP")
    assert env["ANDROID_HOME"] == str(layout.root)
    assert env["ANDROID_ADB_SERVER_PORT"] == "5038"
    assert "ANTHROPIC_API_KEY" not in env
    assert adb.Adb(layout, "emulator-5554").argv("emu", "kill")[0] == str(layout.adb)
    discovery = tmp_path / "LOCALAPPDATA" / "Temp" / "avd" / "running"
    discovery.mkdir(parents=True)
    (discovery / "pid_42.ini").write_text("grpc.port=8554\ngrpc.token=token\n")
    assert layout.discovery(42) == {"grpc.port": "8554", "grpc.token": "token"}
    temp_discovery = tmp_path / "TEMP" / "avd" / "running"
    temp_discovery.mkdir(parents=True)
    (temp_discovery / "pid_43.ini").write_text("grpc.port=8555\n")
    assert layout.discovery(43) == {"grpc.port": "8555"}


@pytest.mark.parametrize("returncode", [0, 1, 2])
def test_windows_acceleration_uses_sdk_probe(windows, monkeypatch, tmp_path, returncode):
    layout = SdkLayout(tmp_path)
    layout.emulator_bin.parent.mkdir(parents=True)
    layout.emulator_bin.write_bytes(b"exe")
    probe = Mock(return_value=SimpleNamespace(returncode=returncode))
    monkeypatch.setattr(host.subprocess, "run", probe)
    result = host.check_host(emulator_bin=layout.emulator_bin, env=layout.env())
    assert (result.host_os, result.host_arch) == ("windows", "x64")
    assert result.ok == (returncode == 0)
    if returncode:
        assert result.reason == "error"
        assert "Windows Hypervisor Platform" in result.message and "AEHD" in result.message
        assert "reboot" in result.message
    args, kwargs = probe.call_args
    assert args[0] == [str(layout.emulator_bin), "-accel-check"]
    assert kwargs["timeout"] == 10 and kwargs["env"] == layout.env()
    assert not kwargs.get("shell")
    assert kwargs["stdout"] == kwargs["stderr"] == subprocess.DEVNULL
    assert "capture_output" not in kwargs


@pytest.mark.parametrize("error", [OSError("driver"), subprocess.TimeoutExpired("probe", 10)])
def test_acceleration_probe_failure_is_actionable(windows, monkeypatch, tmp_path, error):
    binary = tmp_path / "emulator.exe"
    binary.touch()
    monkeypatch.setattr(host.subprocess, "run", Mock(side_effect=error))
    result = host.check_host(emulator_bin=binary, env={})
    assert result.reason == "error" and "Acceleration check failed" in result.message


def test_missing_sdk_still_allows_windows_setup(windows, monkeypatch, tmp_path):
    probe = Mock()
    monkeypatch.setattr(host.subprocess, "run", probe)
    result = host.check_host(emulator_bin=tmp_path / "missing.exe")
    assert result.reason == "sdk_missing" and result.host_os == "windows"
    probe.assert_not_called()


async def test_missing_sdk_status_offers_setup(windows, tmp_path):
    status = await EmulatorManager(SdkLayout(tmp_path)).status()
    assert status["reason"] == "sdk_missing" and not status["available"]


async def test_no_acceleration_refuses_start_before_spawn(windows, monkeypatch, tmp_path):
    layout = SdkLayout(tmp_path)
    layout.emulator_bin.parent.mkdir(parents=True)
    layout.emulator_bin.touch()
    from tests.core.test_android_emulator import _installed

    _installed(layout)
    monkeypatch.setattr(host.subprocess, "run", Mock(return_value=SimpleNamespace(returncode=1)))
    spawn = AsyncMock()
    monkeypatch.setattr(manager_mod.asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(EmulatorError, match="hardware acceleration") as exc:
        await EmulatorManager(layout).start("pixel")
    assert exc.value.code == "error"
    spawn.assert_not_called()


class Proc:
    pid = 987654321
    returncode = None

    def __init__(self):
        self.exited = asyncio.Event()

    async def wait(self):
        await self.exited.wait()
        return 0

    def kill(self):
        self.returncode = 0
        self.exited.set()


@pytest.mark.parametrize("force_stop", [False, True])
async def test_windows_spawn_and_stop(windows, monkeypatch, tmp_path, force_stop):
    from app.core import process_ledger

    layout = SdkLayout(tmp_path / "SDK with spaces")
    manager = EmulatorManager(layout)
    dev = manager.device("pixel")
    proc = Proc()
    spawn = AsyncMock(return_value=proc)
    monkeypatch.setattr(manager_mod.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(manager_mod, "_port_free", lambda port: True)
    monkeypatch.setattr(adb, "start_server", AsyncMock())
    monkeypatch.setattr(process_ledger, "record_process", Mock())
    monkeypatch.setattr(process_ledger, "forget_process", Mock())
    await manager._spawn(dev)
    argv = spawn.call_args.args
    kwargs = spawn.call_args.kwargs
    assert argv[0] == str(layout.emulator_bin)
    assert argv[argv.index("-accel") + 1] == "on"
    assert "-no-window" in argv and "-no-audio" in argv and "-grpc-use-token" in argv
    assert kwargs["creationflags"] & process_tree._CREATE_NEW_PROCESS_GROUP
    assert "start_new_session" not in kwargs and "shell" not in kwargs
    assert kwargs["env"]["ANDROID_HOME"] == str(layout.root)
    assert (layout.avd_home / "pixel.avd" / "config.ini").is_file()

    async def emu(*args, **kwargs):
        if not force_stop:
            proc.kill()
        return adb.Result(0, b"", b"")

    monkeypatch.setattr(adb, "run", emu)
    calls = []
    if force_stop:
        monkeypatch.setattr(manager_mod, "STOP_TIMEOUT_SECONDS", 0.001)
        monkeypatch.setattr(process_tree, "descendant_pids", lambda pid: [])
        # Exercise a successful CTRL_BREAK delivery, then taskkill escalation.
        # Linux's signal module has no CTRL_BREAK_EVENT, so provide it explicitly.
        monkeypatch.setattr(process_tree.signal, "CTRL_BREAK_EVENT", 1, raising=False)
        group_signal = Mock()
        monkeypatch.setattr(process_tree.os, "kill", group_signal)
        proc.terminate = Mock()

        def taskkill(argv, **kwargs):
            calls.append(argv)
            proc.kill()

        monkeypatch.setattr(process_tree.subprocess, "run", taskkill)
        real_kill = process_tree.kill_process_tree

        async def fast_kill(proc, *, own_process_group, grace):
            assert own_process_group and grace == 10
            return await real_kill(proc, own_process_group=True, grace=0.001)

        monkeypatch.setattr(manager_mod, "kill_process_tree", fast_kill)
    await manager.stop("pixel")
    assert dev.proc is None and dev.state == "stopped"
    if force_stop:
        group_signal.assert_called_once_with(proc.pid, process_tree.signal.CTRL_BREAK_EVENT)
        proc.terminate.assert_not_called()
        assert calls == [["taskkill", "/F", "/T", "/PID", str(proc.pid)]]


def test_windows_install_selects_archives_and_writes_layout(windows, settings, monkeypatch, tmp_path):
    from app.core.android_emulator import sdk_repo
    from tests.core.test_android_emulator import FILE, _archive, _manifest, _package, _zip

    studio = tmp_path / "Android Studio SDK"
    studio.mkdir()
    sentinel = studio / "emulator" / "package.xml"
    sentinel.parent.mkdir()
    sentinel.write_bytes(b"developer-package")
    monkeypatch.setenv("ANDROID_HOME", str(studio))
    before = {str(path.relative_to(studio)): path.read_bytes() for path in studio.rglob("*") if path.is_file()}
    layout = SdkLayout()
    assert layout.root == settings.data_dir / "android-sdk"
    archives = {
        "emulator-windows.zip": _zip([("emulator/emulator.exe", b"MZ", FILE)]),
        "tools-windows.zip": _zip([("platform-tools/adb.exe", b"MZ", FILE)]),
        "image.zip": _zip([("x86_64/system.img", b"system", FILE)]),
    }
    repo = _manifest([
        _package("emulator", (1,), "channel-0", [
            _archive("emulator-linux.zip", b"wrong-host", host_os="linux"),
            _archive("emulator-windows.zip", archives["emulator-windows.zip"], host_os="windows"),
        ]),
        _package("platform-tools", (1,), "channel-0", [
            _archive("tools-windows.zip", archives["tools-windows.zip"], host_os="windows", host_arch=None),
        ]),
    ])
    images = _manifest([_package(IMAGE, (1,), "channel-0", [
        _archive("image.zip", archives["image.zip"], host_os=None, host_arch=None),
    ])])
    downloaded = []

    def download(url, dest, on_block):
        name = url.rsplit("/", 1)[-1]
        downloaded.append(name)
        data = archives[name]
        dest.write_bytes(data)
        on_block(data)

    monkeypatch.setattr(installer, "_free_bytes", lambda path: 100 * 1024 ** 3)
    progress = installer.InstallProgress()
    ok, message = installer.run_install(
        layout, host.HostSupport("sdk_missing", "", "windows", "x64"), IMAGE,
        progress, threading.Event(),
        fetch=lambda url: repo if url == sdk_repo.REPOSITORY_MANIFEST else images,
        download=download,
    )
    assert ok, message
    assert downloaded == list(archives)
    assert progress.snapshot()["state"] == "done"
    assert layout.installed(IMAGE)
    assert layout.emulator_bin.read_bytes() == b"MZ" and layout.adb.read_bytes() == b"MZ"
    assert (layout.licenses / "android-sdk-license").is_file()
    assert {str(path.relative_to(studio)): path.read_bytes() for path in studio.rglob("*") if path.is_file()} == before
    assert not (studio / layout_mod.RECORD_NAME).exists()


def test_windows_tool_registration_does_not_probe_acceleration(windows, monkeypatch):
    from app.mcp.emulator_tools import emulator_tools_supported

    probe = Mock(side_effect=AssertionError("registration must not spawn"))
    monkeypatch.setattr(host.subprocess, "run", probe)
    assert emulator_tools_supported()
    monkeypatch.setattr(host.platform, "machine", lambda: "ARM64")
    assert not emulator_tools_supported()
    probe.assert_not_called()


async def test_stop_reports_surviving_processes(windows, monkeypatch, tmp_path):
    manager = EmulatorManager(SdkLayout(tmp_path))
    dev = manager.device("pixel")
    dev.proc = proc = Proc()
    monkeypatch.setattr(adb, "run", AsyncMock(return_value=adb.Result(0, b"", b"")))
    monkeypatch.setattr(manager_mod, "STOP_TIMEOUT_SECONDS", 0.001)
    monkeypatch.setattr(manager_mod, "kill_process_tree", AsyncMock(return_value=[123]))
    with pytest.raises(EmulatorError, match="Could not stop emulator processes: 123") as exc:
        await manager.stop("pixel")
    assert exc.value.code == "stop_failed"
    assert dev.proc is proc and dev.state == "error"



def test_windows_native_path_layout(windows, monkeypatch):
    monkeypatch.setattr(layout_mod, "Path", PureWindowsPath)
    layout = SdkLayout(r"C:\Users\Developer\App Data\AI Shuffle\android-sdk")
    assert layout.emulator_bin == PureWindowsPath(r"C:\Users\Developer\App Data\AI Shuffle\android-sdk\emulator\emulator.exe")
    assert layout.adb == PureWindowsPath(r"C:\Users\Developer\App Data\AI Shuffle\android-sdk\platform-tools\adb.exe")
    assert layout.env()["ANDROID_HOME"] == str(layout.root)


@pytest.mark.parametrize("executable,expected", [
    (r"c:\sdk\emulator\emulator.exe", True),
    (r"C:/SDK/PLATFORM-TOOLS/adb.exe", True),
    (r"C:\Sdk-old\emulator.exe", False),
    (r"D:\Sdk\emulator.exe", False),
    (r"C:\Sdk\..\Other\adb.exe", False),
])
def test_windows_cleanup_path_comparison(windows, executable, expected):
    assert manager_mod._path_in_sdk(executable, r"C:\Sdk") is expected


@pytest.mark.parametrize("explicit", [False, True])
def test_external_sdk_processes_and_locks_are_not_cleaned(windows, settings, monkeypatch, tmp_path, explicit):
    import psutil

    studio = tmp_path / "Android Studio SDK"
    lock = studio / "avd" / "pixel.avd" / "hardware-qemu.ini.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text("developer-lock")
    monkeypatch.setenv("ANDROID_HOME", str(studio))
    if explicit:
        monkeypatch.setattr(settings, "android_sdk_dir", studio)
    layout = SdkLayout()
    layout.root.mkdir(parents=True, exist_ok=True)
    developer_proc = Mock(info={"pid": 987654322, "exe": str(studio / "emulator" / "emulator.exe"), "uids": None})
    processes = Mock(return_value=[developer_proc])
    monkeypatch.setattr(psutil, "process_iter", processes)
    monkeypatch.setattr(psutil, "wait_procs", lambda victims, timeout: (victims, []))
    assert manager_mod.cleanup_orphans() == 0
    developer_proc.terminate.assert_not_called()
    developer_proc.kill.assert_not_called()
    assert lock.read_text() == "developer-lock"
    if explicit:
        processes.assert_not_called()


def test_acceleration_success_is_cached_until_binary_changes(windows, monkeypatch, tmp_path):
    binary = tmp_path / "emulator.exe"
    binary.write_bytes(b"one")
    probe = Mock(return_value=SimpleNamespace(returncode=0))
    monkeypatch.setattr(host.subprocess, "run", probe)
    assert host.check_host(emulator_bin=binary, env={}).ok
    assert host.check_host(emulator_bin=binary, env={}).ok
    assert probe.call_count == 1
    revision = binary.stat()
    # NTFS keeps 100 ns ticks (FAT 2 s), so a 1 ns bump would not change mtime.
    os.utime(binary, ns=(revision.st_atime_ns, revision.st_mtime_ns + 2_000_000_000))
    assert host.check_host(emulator_bin=binary, env={}).ok
    assert probe.call_count == 2
    other = tmp_path / "other-emulator.exe"
    other.write_bytes(b"one")
    assert host.check_host(emulator_bin=other, env={}).ok
    assert probe.call_count == 3


def test_acceleration_failure_is_retried(windows, monkeypatch, tmp_path):
    binary = tmp_path / "emulator.exe"
    binary.touch()
    probe = Mock(side_effect=[SimpleNamespace(returncode=1), SimpleNamespace(returncode=0)])
    monkeypatch.setattr(host.subprocess, "run", probe)
    assert not host.check_host(emulator_bin=binary, env={}).ok
    assert host.check_host(emulator_bin=binary, env={}).ok
    assert probe.call_count == 2


@pytest.mark.parametrize("state", ["booting", "running"])
async def test_status_does_not_probe_an_active_emulator(windows, monkeypatch, tmp_path, state):
    from tests.core.test_android_emulator import _installed

    layout = SdkLayout(tmp_path)
    _installed(layout)
    manager = EmulatorManager(layout)
    manager.device("pixel").state = state
    probe = Mock(side_effect=AssertionError("active emulator must not probe"))
    monkeypatch.setattr(host.subprocess, "run", probe)
    assert (await manager.status())["available"]
    probe.assert_not_called()


async def test_partial_sdk_with_no_acceleration_still_offers_setup(windows, monkeypatch, tmp_path):
    layout = SdkLayout(tmp_path)
    layout.emulator_bin.parent.mkdir(parents=True)
    layout.emulator_bin.touch()
    monkeypatch.setattr(host.subprocess, "run", Mock(return_value=SimpleNamespace(returncode=1)))
    status = await EmulatorManager(layout).status()
    assert status["reason"] == "sdk_missing" and not status["available"]
    assert "Windows Hypervisor Platform" in status["message"]


async def test_adb_start_and_shutdown_use_private_port(windows, monkeypatch, tmp_path):
    layout = SdkLayout(tmp_path)
    layout.adb.parent.mkdir(parents=True)
    layout.adb.touch()
    monkeypatch.setenv("ANDROID_ADB_SERVER_PORT", "5037")
    run = AsyncMock(return_value=adb.Result(0, b"", b""))
    monkeypatch.setattr(adb, "run", run)
    await adb.start_server(layout)
    manager = EmulatorManager(layout)
    manager._adb_started = True
    await manager.shutdown()
    assert [call.args[0][-1] for call in run.call_args_list] == ["start-server", "kill-server"]
    assert all(call.kwargs["env"]["ANDROID_ADB_SERVER_PORT"] == "5038" for call in run.call_args_list)


async def test_status_does_not_probe_during_setup(windows, monkeypatch, tmp_path):
    layout = SdkLayout(tmp_path)
    layout.emulator_bin.parent.mkdir(parents=True)
    layout.emulator_bin.touch()
    manager = EmulatorManager(layout)
    manager._install_task = SimpleNamespace(done=lambda: False)
    probe = Mock(side_effect=AssertionError("installation must not probe"))
    monkeypatch.setattr(host.subprocess, "run", probe)
    assert (await manager.status())["reason"] == "installing"
    probe.assert_not_called()
