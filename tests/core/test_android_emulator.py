"""app.core.android_emulator: manifests, install, AVD, host checks, device control.

Nothing here downloads or boots anything: manifests and archives are built in
the test, adb is a fake ``adb.run`` and gRPC is a fake control object.
"""

from __future__ import annotations

import asyncio
import errno
import hashlib
import io
import os
import stat
import struct
import threading
import zipfile
from pathlib import Path

import pytest

from app.core.android_emulator import adb as adb_mod
from app.core.android_emulator import apk, avd, frames, grpc_control, host, installer, sdk_repo, ui_tree
from app.core.android_emulator.layout import SdkLayout
from app.core.android_emulator.manager import Device, EmulatorError, EmulatorManager

IMAGE = "system-images;android-35;google_apis;x86_64"
BASE = sdk_repo.REPOSITORY_BASE
SYS_BASE = BASE + "sys-img/google_apis/"
LICENSE_TEXT = "\nTerms and Conditions\n\nThis is the Android Software Development Kit License Agreement\n"


# ── fixtures: archives and manifests ─────────────────────────────────────────
def _zip(entries) -> bytes:
    """entries: (name, data, mode). A mode with S_IFLNK makes a symlink."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data, mode in entries:
            info = zipfile.ZipInfo(name)
            info.external_attr = (mode & 0xFFFF) << 16
            zf.writestr(info, data)
    return buf.getvalue()


EXEC = stat.S_IFREG | 0o755
FILE = stat.S_IFREG | 0o644
LINK = stat.S_IFLNK | 0o777

ARCHIVES = {
    "emulator-linux_x64-2.zip": _zip([
        ("emulator/emulator", b"#!/bin/sh\n", EXEC),
        ("emulator/lib64/libfoo.so.1", b"lib", FILE),
        ("emulator/lib64/libfoo.so", b"libfoo.so.1", LINK),
    ]),
    "platform-tools_r37-linux.zip": _zip([("platform-tools/adb", b"adb", EXEC)]),
    "x86_64-35_r09.zip": _zip([("x86_64/system.img", b"system", FILE),
                               ("x86_64/source.properties", b"x", FILE)]),
}


def _sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def _archive(url, data=None, host_os="linux", host_arch="x64", sha1=None):
    data = ARCHIVES[url] if data is None else data
    hosts = ""
    if host_os:
        hosts += f"<host-os>{host_os}</host-os>"
    if host_arch:
        hosts += f"<host-arch>{host_arch}</host-arch>"
    return (f"<archive><complete><size>{len(data)}</size><checksum type=\"sha1\">{sha1 or _sha1(data)}"
            f"</checksum><url>{url}</url></complete>{hosts}</archive>")


def _package(path, rev, channel, archives, license_id="android-sdk-license", obsolete=False):
    major, minor, micro = (rev + (0, 0))[:3]
    attrs = ' obsolete="true"' if obsolete else ""
    return (f'<remotePackage path="{path}"{attrs}><revision><major>{major}</major><minor>{minor}</minor>'
            f'<micro>{micro}</micro></revision><channelRef ref="{channel}"/>'
            f'<uses-license ref="{license_id}"/><archives>{"".join(archives)}</archives></remotePackage>')


def _manifest(packages) -> bytes:
    return (
        '<?xml version="1.0" ?><sdk:sdk-repository xmlns:sdk="http://schemas.android.com/sdk/android/repo/repository2/03">'
        f'<license id="android-sdk-license" type="text">{LICENSE_TEXT}</license>'
        '<license id="android-sdk-preview-license" type="text">preview</license>'
        '<channel id="channel-0">stable</channel><channel id="channel-3">canary</channel>'
        + "".join(packages) + "</sdk:sdk-repository>"
    ).encode()


def _repo(emulator_sha1=None) -> bytes:
    return _manifest([
        _package("emulator", (2, 0, 0), "channel-0", [
            _archive("emulator-linux_x64-2.zip", sha1=emulator_sha1),
            _archive("emulator-windows_x64-2.zip", b"win", host_os="windows"),
        ]),
        # Newer, but canary: never chosen.
        _package("emulator", (3, 0, 0), "channel-3", [_archive("emulator-linux_x64-3.zip", b"canary")],
                 license_id="android-sdk-preview-license"),
        _package("platform-tools", (37, 0, 1), "channel-0",
                 [_archive("platform-tools_r37-linux.zip", host_arch=None)]),
    ])


def _sysimg() -> bytes:
    return _manifest([
        _package(IMAGE, (9,), "channel-0", [_archive("x86_64-35_r09.zip", host_os=None, host_arch=None)]),
    ])


LINUX = host.HostSupport("ready", "", "linux", "x64")


def _fetch(repo: bytes):
    manifests = {sdk_repo.REPOSITORY_MANIFEST: repo, SYS_BASE + "sys-img2-3.xml": _sysimg()}
    return lambda url: manifests[url]


def _download(served=None):
    calls = []

    def download(url, dest, on_block):
        calls.append(url)
        name = url.rsplit("/", 1)[-1]
        data = (served or {}).get(name, ARCHIVES.get(name, b""))
        Path(dest).write_bytes(data)
        for i in range(0, len(data), 7):
            on_block(data[i:i + 7])

    download.calls = calls
    return download


@pytest.fixture
def layout(tmp_path) -> SdkLayout:
    return SdkLayout(tmp_path / "android-sdk")


# ── manifests ────────────────────────────────────────────────────────────────
def test_resolve_picks_newest_stable_archive_for_this_host():
    packages = sdk_repo.parse_manifest(_repo(), BASE)
    package, archive = sdk_repo.resolve(packages, "emulator", "linux", "x64")
    assert package.revision == (2, 0, 0) and package.channel == "stable"
    assert archive.url == BASE + "emulator-linux_x64-2.zip"
    assert package.license_text.strip().startswith("Terms and Conditions")
    # An archive that names no host serves every host.
    _pkg, tools = sdk_repo.resolve(packages, "platform-tools", "linux", "x64")
    assert tools.host_arch is None
    with pytest.raises(sdk_repo.ManifestError):
        sdk_repo.resolve(packages, "emulator", "macosx", "aarch64")


def test_manifest_refuses_dtds_and_foreign_archive_paths():
    with pytest.raises(sdk_repo.ManifestError):
        sdk_repo.parse_manifest(b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><x/>', BASE)
    evil = _manifest([_package("platform-tools", (1,), "channel-0",
                               [_archive("platform-tools_r37-linux.zip").replace(
                                   "<url>platform-tools_r37-linux.zip", "<url>../../evil.zip")])])
    packages = sdk_repo.parse_manifest(evil, BASE)
    assert packages[0].archives == ()


def test_system_image_paths():
    assert sdk_repo.system_image_manifest(IMAGE) == SYS_BASE + "sys-img2-3.xml"
    assert sdk_repo.system_image_parts(IMAGE) == (35, "google_apis", "x86_64")
    with pytest.raises(sdk_repo.ManifestError):
        sdk_repo.system_image_manifest("system-images;android-35;../x;x86_64")


# ── install ──────────────────────────────────────────────────────────────────
def _run(layout, repo=None, download=None, cancel=None):
    progress = installer.InstallProgress()
    ok, message = installer.run_install(
        layout, LINUX, IMAGE, progress, cancel or threading.Event(),
        fetch=_fetch(repo or _repo()), download=download or _download())
    return ok, message, progress


def test_install_verifies_extracts_and_records(layout, monkeypatch):
    monkeypatch.setattr(installer, "_free_bytes", lambda path: 100 * 1024 ** 3)
    download = _download()
    ok, message, progress = _run(layout, download=download)
    assert ok, message
    assert progress.snapshot() == {"state": "done", "progress": 1.0, "message": "Android emulator installed"}
    assert layout.emulator_bin.read_bytes() == b"#!/bin/sh\n"
    assert os.stat(layout.emulator_bin).st_mode & 0o111  # executable bits kept
    link = layout.root / "emulator/lib64/libfoo.so"
    assert link.is_symlink() and os.readlink(link) == "libfoo.so.1"
    assert (layout.package_dir(IMAGE) / "system.img").read_bytes() == b"system"
    assert layout.installed(IMAGE)
    assert not list(layout.downloads.glob("*")), "archives are deleted after unpacking"
    # The license is recorded where sdkmanager records it.
    expected = installer.license_hash(LICENSE_TEXT)
    assert expected in (layout.licenses / "android-sdk-license").read_text().split()
    assert layout.read_record()["license"]["sha1"] == expected
    # A second run downloads nothing: every package is current.
    again = _download()
    assert _run(layout, download=again)[0]
    assert again.calls == []


def test_checksum_mismatch_discards_the_archive(layout, monkeypatch):
    monkeypatch.setattr(installer, "_free_bytes", lambda path: 100 * 1024 ** 3)
    ok, message, progress = _run(layout, repo=_repo(emulator_sha1="0" * 40))
    assert not ok
    assert "SHA-1 does not match" in message
    assert progress.snapshot()["state"] == "error"
    assert not layout.emulator_bin.exists()
    assert not list(layout.downloads.glob("*"))
    assert not layout.installed(IMAGE)


def test_a_download_longer_than_the_manifest_is_refused(layout, monkeypatch):
    monkeypatch.setattr(installer, "_free_bytes", lambda path: 100 * 1024 ** 3)
    padded = ARCHIVES["emulator-linux_x64-2.zip"] + b"extra"
    ok, message, _ = _run(layout, download=_download({"emulator-linux_x64-2.zip": padded}))
    assert not ok and "larger than the manifest" in message


def test_not_enough_disk_fails_before_downloading(layout, monkeypatch):
    monkeypatch.setattr(installer, "_free_bytes", lambda path: 1024 ** 3)
    download = _download()
    ok, message, _ = _run(layout, download=download)
    assert not ok
    assert "Not enough free disk space" in message and "1.0 GB is free" in message
    assert download.calls == []


def test_other_licenses_are_never_accepted(layout, monkeypatch):
    monkeypatch.setattr(installer, "_free_bytes", lambda path: 100 * 1024 ** 3)
    repo = _repo().replace(b'<uses-license ref="android-sdk-license"/><archives><archive><complete><size>'
                           + str(len(ARCHIVES["platform-tools_r37-linux.zip"])).encode(),
                           b'<uses-license ref="android-sdk-preview-license"/><archives><archive><complete><size>'
                           + str(len(ARCHIVES["platform-tools_r37-linux.zip"])).encode())
    ok, message, _ = _run(layout, repo=repo)
    assert not ok and "android-sdk-preview-license" in message


def test_cancel_stops_the_download(layout, monkeypatch):
    monkeypatch.setattr(installer, "_free_bytes", lambda path: 100 * 1024 ** 3)
    cancel = threading.Event()

    def download(url, dest, on_block):
        cancel.set()
        raise installer.Cancelled("Install cancelled")

    ok, message, progress = _run(layout, download=download, cancel=cancel)
    assert not ok and message == "Install cancelled" and progress.snapshot()["state"] == "error"


@pytest.mark.parametrize("entries", [
    [("pkg/../../escape", b"x", FILE)],
    [("pkg/ok", b"x", FILE), ("/abs/path", b"x", FILE)],
    [("pkg/link", b"../../../etc/passwd", LINK)],
    [("pkg/link", b"/etc/passwd", LINK)],
])
def test_extract_refuses_entries_outside_the_package(tmp_path, entries):
    archive = tmp_path / "a.zip"
    archive.write_bytes(_zip(entries))
    with pytest.raises(installer.InstallError):
        installer.extract_zip(archive, tmp_path / "out")
    assert not (tmp_path / "escape").exists()


# ── AVD ──────────────────────────────────────────────────────────────────────
def test_avd_definition_is_a_headless_pixel(layout):
    folder = avd.write_avd(layout, avd.PROFILES["pixel"], IMAGE)
    config = dict(line.split("=", 1) for line in (folder / "config.ini").read_text().splitlines())
    assert config["image.sysdir.1"] == "system-images/android-35/google_apis/x86_64/"
    assert (config["hw.lcd.width"], config["hw.lcd.height"], config["hw.lcd.density"]) == ("1080", "2400", "420")
    assert config["hw.gpu.mode"] == "swiftshader_indirect"
    assert config["abi.type"] == "x86_64" and config["hw.cpu.arch"] == "x86_64"
    assert config["fastboot.forceColdBoot"] == "yes"
    assert config["PlayStore.enabled"] == "false"
    pointer = (layout.avd_home / "pixel.ini").read_text()
    assert f"path={folder}\n" in pointer and "target=android-35\n" in pointer
    (folder / "hardware-qemu.ini.lock").write_text("1")
    (folder / "multiinstance.lock").mkdir()
    (folder / "multiinstance.lock" / "pid").write_text("1")
    assert avd.clear_locks(layout, avd.PROFILES["pixel"]) == 2
    assert not list(folder.glob("*.lock"))


def test_emulator_env_carries_no_app_secrets(layout, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret")
    env = layout.env()
    assert "ANTHROPIC_API_KEY" not in env
    assert env["ANDROID_AVD_HOME"] == str(layout.avd_home)
    assert env["ANDROID_USER_HOME"] == str(layout.user_home)


# ── host detection ───────────────────────────────────────────────────────────
def test_host_reasons(monkeypatch, tmp_path):
    monkeypatch.setattr(host.platform, "system", lambda: "Linux")
    monkeypatch.setattr(host.platform, "machine", lambda: "x86_64")
    assert host.check_host(str(tmp_path / "missing")).reason == "no_kvm"
    device = tmp_path / "kvm"
    device.write_text("")
    assert host.check_host(str(device)).reason == "ready"

    def denied(path, flags):
        raise PermissionError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(host.os, "open", denied)
    result = host.check_host(str(device))
    assert result.reason == "kvm_permission" and result.host_os == "linux"
    monkeypatch.setattr(host.platform, "machine", lambda: "aarch64")
    assert host.check_host(str(device)).reason == "unsupported_arch"
    monkeypatch.setattr(host.platform, "system", lambda: "Windows")
    monkeypatch.setattr(host.platform, "machine", lambda: "AMD64")
    assert host.check_host(str(device)).reason == "unsupported_arch"


# ── APK and UI parsing ───────────────────────────────────────────────────────
def _axml(package: str, utf8: bool = False) -> bytes:
    strings = ["package", "manifest", package]

    def enc(s):
        if utf8:
            data = s.encode()
            return bytes([len(s), len(data)]) + data + b"\0"
        return struct.pack("<H", len(s)) + s.encode("utf-16-le") + b"\0\0"

    offsets, blob = [], b""
    for s in strings:
        offsets.append(len(blob))
        blob += enc(s)
    blob += b"\0" * (-len(blob) % 4)
    start = 28 + 4 * len(strings)
    pool = (struct.pack("<HHIIIIII", 0x0001, 28, start + len(blob), len(strings), 0,
                        0x100 if utf8 else 0, start, 0)
            + struct.pack(f"<{len(strings)}I", *offsets) + blob)
    attr = struct.pack("<IIIHBBI", 0xFFFFFFFF, 0, 2, 8, 0, 0x03, 2)
    body = struct.pack("<IIHHHHHH", 0xFFFFFFFF, 1, 20, 20, 1, 0, 0, 0) + attr
    element = struct.pack("<HHIII", 0x0102, 16, 16 + len(body), 1, 0xFFFFFFFF) + body
    return struct.pack("<HHI", 0x0003, 8, 8 + len(pool) + len(element)) + pool + element


@pytest.mark.parametrize("utf8", [False, True])
def test_apk_package_name(tmp_path, utf8):
    path = tmp_path / "app.apk"
    path.write_bytes(_zip([("AndroidManifest.xml", _axml("com.example.todo", utf8), FILE),
                           ("classes.dex", b"dex", FILE)]))
    assert apk.apk_package(path) == "com.example.todo"


def test_non_apks_are_refused(tmp_path):
    text = tmp_path / "notes.apk"
    text.write_text("hello")
    with pytest.raises(apk.ApkError, match="not a zip"):
        apk.apk_package(text)
    plain_zip = tmp_path / "plain.apk"
    plain_zip.write_bytes(_zip([("readme.txt", b"x", FILE)]))
    with pytest.raises(apk.ApkError, match="no AndroidManifest"):
        apk.apk_package(plain_zip)
    text_manifest = tmp_path / "text.apk"
    text_manifest.write_bytes(_zip([("AndroidManifest.xml", b"<manifest package='x'/>", FILE)]))
    with pytest.raises(apk.ApkError):
        apk.apk_package(text_manifest)


DUMP = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?><hierarchy rotation="0">
<node index="0" text="" resource-id="" class="android.widget.FrameLayout" package="com.example.todo" content-desc="" clickable="false" bounds="[0,0][1080,2400]">
  <node index="0" text="Sign in" resource-id="com.example.todo:id/login" class="android.widget.Button" package="com.example.todo" content-desc="" clickable="true" enabled="true" bounds="[100,1000][980,1200]"/>
  <node index="1" text="" resource-id="com.example.todo:id/email" class="android.widget.EditText" package="com.example.todo" content-desc="Email" clickable="true" focused="true" bounds="[100,800][980,950]"/>
  <node index="2" text="" resource-id="" class="android.view.View" package="com.example.todo" content-desc="" clickable="false" bounds="[0,0][10,10]"/>
</node></hierarchy>UI hierchary dumped to: /dev/tty"""


def test_ui_tree_is_short_and_scaled():
    tree = ui_tree.compact(DUMP, scale=0.5)
    lines = tree.splitlines()
    assert lines[0] == "package=com.example.todo"
    assert lines[1] == 'Button "Sign in" #login @(270,550) [50,500,490,600] clickable'
    assert lines[2].startswith('EditText desc="Email" #email @(270,437)')
    assert "editable" in lines[2] and "focused" in lines[2]
    assert len(lines) == 3  # the plain layout views are dropped
    with pytest.raises(ValueError):
        ui_tree.compact('<!DOCTYPE x [<!ENTITY a "b">]><hierarchy/>')


# ── gRPC wire format ─────────────────────────────────────────────────────────
def test_grpc_messages_round_trip():
    touch = grpc_control.decode(grpc_control.touch_event([(540, 1200, 0, 512)]))
    inner = grpc_control.decode(touch[1][0])
    assert inner == {1: [540], 2: [1200], 4: [512]}
    key = grpc_control.decode(grpc_control.keyboard_event(key="GoHome"))
    assert key == {2: [grpc_control.KEYPRESS], 4: [b"GoHome"]}
    fmt = grpc_control.image_format(grpc_control.RGB888, 1280, 1280) + b"\x12\x02\x08\x01"  # rotation=LANDSCAPE
    image = b"".join([grpc_control._len(1, fmt), grpc_control._len(4, b"\x01\x02\x03"),
                      grpc_control._int(5, 7)])
    frame = grpc_control.parse_image(image)
    assert (frame.width, frame.height, frame.pixels, frame.seq, frame.rotation) == (1280, 1280, b"\x01\x02\x03", 7, 1)
    assert grpc_control._varint(-1) == b"\xff" * 9 + b"\x01"


def test_frames_scale_and_encode():
    assert frames.fit(1080, 2400, 1280) == (576, 1280)
    assert frames.fit(2400, 1080, 1280) == (1280, 576)
    assert frames.fit(500, 800, 1280) == (500, 800)
    data, w, h = frames.rgb_to_jpeg(b"\x80" * (4 * 6 * 3), 4, 6, size=(2, 3))
    assert data[:2] == b"\xff\xd8" and (w, h) == (2, 3)
    raw = struct.pack("<IIII", 2, 1, 1, 0) + b"\x01\x02\x03\xff\x04\x05\x06\xff"
    assert frames.raw_screencap_to_rgb(raw) == (b"\x01\x02\x03\x04\x05\x06", 2, 1)


# ── manager ──────────────────────────────────────────────────────────────────
class FakeAdb:
    """Stands in for ``adb.run``: answers by command and records each call."""

    def __init__(self):
        self.calls = []
        self.replies = {}

    def reply(self, needle, stdout=b"", returncode=0, stderr=b""):
        self.replies[needle] = adb_mod.Result(returncode, stdout, stderr)

    async def __call__(self, argv, *, env, timeout=30.0, stdin=None):
        line = " ".join(str(part) for part in argv[1:])
        self.calls.append(line)
        matches = [needle for needle in self.replies if needle in line]
        if matches:
            return self.replies[max(matches, key=len)]  # the most specific reply
        return adb_mod.Result(0, b"", b"")


class FakeGrpc:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail
        self.target = "127.0.0.1:8554"

    async def touch(self, x, y, pressure, ident=0, timeout=3.0):
        if self.fail:
            raise RuntimeError("grpc down")
        self.calls.append(("touch", x, y, pressure))

    async def key(self, key, timeout=3.0):
        self.calls.append(("key", key))

    async def text(self, text, timeout=5.0):
        self.calls.append(("text", text))

    async def screenshot(self, width=0, height=0, fmt=2, timeout=10.0):
        w, h = (width, height) if width else (1080, 2400)
        w, h = frames.fit(1080, 2400, max(w, h))
        return grpc_control.Frame(w, h, b"\x10" * (w * h * 3), 1, fmt)

    async def close(self):
        pass


@pytest.fixture
def fake_adb(monkeypatch):
    fake = FakeAdb()
    monkeypatch.setattr(adb_mod, "run", fake)
    return fake


def _installed(layout):
    layout.write_record({"packages": {"emulator": {}, "platform-tools": {}, IMAGE: {}}})
    for path in (layout.emulator_bin, layout.adb, layout.package_dir(IMAGE) / "system.img"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")


@pytest.fixture
def manager(layout, monkeypatch):
    from app.core.android_emulator import manager as manager_mod

    monkeypatch.setattr(manager_mod, "check_host", lambda: LINUX)
    _installed(layout)
    return EmulatorManager(layout)


def _running(manager, grpc=None) -> Device:
    dev = manager.device("pixel")
    dev.state, dev.console_port, dev.grpc = "running", 5554, grpc
    dev.booted.set()
    return dev


async def test_status_contract(manager, layout, monkeypatch):
    from app.core.android_emulator import manager as manager_mod

    status = await manager.status()
    assert status == {
        "available": True, "reason": "ready", "message": "", "install": None,
        "license_url": "https://developer.android.com/studio/terms",
        "devices": [{"id": "pixel", "name": "Pixel", "state": "stopped", "api": 35,
                     "width": 1080, "height": 2400}],
    }
    monkeypatch.setattr(manager_mod, "check_host",
                        lambda: host.HostSupport("no_kvm", host.NO_KVM, "linux", "x64"))
    status = await manager.status()
    assert (status["available"], status["reason"]) == (False, "no_kvm")
    layout.record_path.unlink()
    status = await manager.status()
    assert status["reason"] == "no_kvm" and status["devices"] == []
    monkeypatch.setattr(manager_mod, "check_host", lambda: LINUX)
    assert (await manager.status())["reason"] == "sdk_missing"
    manager.progress.set("error", 0.4, "Not enough free disk space")
    status = await manager.status()
    assert status["reason"] == "error" and status["install"]["state"] == "error"
    monkeypatch.setattr(manager_mod, "check_host", lambda: host.HostSupport("unsupported_arch", "no"))
    assert (await manager.status())["reason"] == "unsupported_arch"


async def test_setup_needs_the_license_and_reports_installing(manager, monkeypatch):
    from app.core.android_emulator import manager as manager_mod

    with pytest.raises(EmulatorError) as exc:
        await manager.setup(False)
    assert exc.value.status == 400 and exc.value.code == "license_required"
    with pytest.raises(EmulatorError) as exc:
        await manager.setup(True, "system-images;android-35;google_apis;arm64-v8a")
    assert exc.value.code == "bad_system_image"
    release = threading.Event()
    seen = {}

    def fake_install(layout, host_support, image, progress, cancel):
        seen["image"] = image
        progress.set("downloading", 0.5, "Downloading emulator")
        release.wait(5)
        progress.set("done", 1.0, "Android emulator installed")
        return True, "ok"

    monkeypatch.setattr(manager_mod, "run_install", fake_install)
    status = await manager.setup(True)
    assert status["reason"] == "installing"
    await asyncio.sleep(0.05)
    assert (await manager.status())["install"]["progress"] == 0.5
    assert (await manager.setup(True))["reason"] == "installing"  # idempotent while it runs
    release.set()
    await manager._install_task
    assert seen["image"] == IMAGE
    assert manager.layout.read_record()["system_image"] == IMAGE


async def test_start_spawns_a_headless_emulator_and_waits_for_boot(manager, fake_adb, monkeypatch):
    from app.core.android_emulator import manager as manager_mod

    spawned = {}

    class Proc:
        pid = 4242
        returncode = None

        def __init__(self):
            self.exited = asyncio.Event()

        async def wait(self):
            await self.exited.wait()
            return 0

    proc = Proc()

    async def fake_exec(*argv, **kwargs):
        spawned["argv"], spawned["env"] = argv, kwargs["env"]
        return proc

    monkeypatch.setattr(manager_mod.asyncio, "create_subprocess_exec", fake_exec)
    tokens = []
    monkeypatch.setattr(manager_mod, "GrpcControl", lambda port, token: tokens.append(token) or FakeGrpc())
    monkeypatch.setattr(manager_mod, "_port_free", lambda port: True)
    fake_adb.reply("getprop sys.boot_completed", b"1\n")

    async def fake_status(self, timeout=3.0):
        return {}

    FakeGrpc.status = fake_status
    dev = await manager.start("pixel")
    assert dev.state == "booting"
    await manager.wait_running(dev, timeout=10)
    assert dev.state == "running" and dev.serial == "emulator-5554"
    argv = spawned["argv"]
    for flag in ("-no-window", "-no-audio", "-no-boot-anim", "-no-snapshot", "-no-metrics"):
        assert flag in argv
    assert argv[argv.index("-gpu") + 1] == "swiftshader_indirect"
    assert argv[argv.index("-avd") + 1] == "pixel"
    assert argv[argv.index("-grpc") + 1] == "8554"
    # The token keeps gRPC on loopback and authenticated; the client sends it.
    assert "-grpc-use-token" in argv
    token_file = manager.layout.console_token_path
    assert tokens == [token_file.read_text()] and len(tokens[0]) >= 16
    assert stat.S_IMODE(os.stat(token_file).st_mode) == 0o600
    assert spawned["env"]["HOME"] == str(manager.layout.user_home)
    assert "start-server" in fake_adb.calls[0]
    assert any("svc power stayon true" in call for call in fake_adb.calls)
    assert (manager.layout.avd_home / "pixel.avd" / "config.ini").is_file()
    # One emulator per worker: starting it again returns the same device.
    assert await manager.start("pixel") is dev

    async def kill_on_emu(argv, *, env, timeout=30.0, stdin=None):
        line = " ".join(argv[1:])
        fake_adb.calls.append(line)
        if line.endswith("emu kill"):
            proc.exited.set()
        return adb_mod.Result(0, b"", b"")

    monkeypatch.setattr(adb_mod, "run", kill_on_emu)
    await manager.stop("pixel")
    assert dev.state == "stopped" and dev.proc is None
    assert any(call.endswith("emu kill") for call in fake_adb.calls)


async def test_start_is_refused_when_unavailable_or_full(manager, monkeypatch):
    from app.core.android_emulator import manager as manager_mod

    with pytest.raises(EmulatorError) as exc:
        await manager.start("nexus")
    assert exc.value.status == 404
    monkeypatch.setattr(manager_mod, "check_host",
                        lambda: host.HostSupport("kvm_permission", host.KVM_PERMISSION, "linux", "x64"))
    with pytest.raises(EmulatorError) as exc:
        await manager.start("pixel")
    assert exc.value.code == "kvm_permission"
    monkeypatch.setattr(manager_mod, "check_host", lambda: LINUX)
    other = avd.DeviceProfile(id="tablet", name="Tablet", width=1600, height=2560, density=320)
    monkeypatch.setitem(avd.PROFILES, "tablet", other)
    manager.device("tablet").state = "running"
    with pytest.raises(EmulatorError) as exc:
        await manager.start("pixel")
    assert exc.value.code == "device_limit"


async def test_boot_failure_keeps_its_reason(manager, fake_adb, monkeypatch):
    from app.core.android_emulator import manager as manager_mod

    monkeypatch.setattr(manager_mod, "BOOT_TIMEOUT_SECONDS", 0.2)
    dev = manager.device("pixel")
    dev.state, dev.console_port = "booting", 5554

    class Proc:
        pid, returncode = 1, None

        async def wait(self):
            return 0

    dev.proc = Proc()
    monkeypatch.setattr(manager_mod.os, "killpg", lambda pid, sig: None)
    await manager._boot(dev)
    assert dev.state == "error" and "did not finish booting" in dev.message


async def test_input_goes_over_grpc_and_falls_back_to_adb(manager, fake_adb):
    grpc = FakeGrpc()
    _running(manager, grpc)
    await manager.tap("pixel", 540, 1200)
    assert grpc.calls[:2] == [("touch", 540, 1200, 512), ("touch", 540, 1200, 0)]
    grpc.calls.clear()
    await manager.swipe("pixel", 100, 2000, 100, 400, 100)
    assert grpc.calls[0] == ("touch", 100, 2000, 512) and grpc.calls[-1] == ("touch", 100, 400, 0)
    await manager.key("pixel", "home")
    await manager.type_text("pixel", "hello world")
    assert ("key", "GoHome") in grpc.calls and ("text", "hello world") in grpc.calls
    # Text gRPC cannot type goes through adb.
    await manager.type_text("pixel", "héllo wörld")
    assert any("input text" in call and "h%sllo" not in call and "%s" in call for call in fake_adb.calls)
    with pytest.raises(EmulatorError) as exc:
        await manager.tap("pixel", 2000, 10)
    assert exc.value.code == "bad_point"
    with pytest.raises(EmulatorError):
        await manager.key("pixel", "SELFDESTRUCT")
    grpc.fail = True
    await manager.tap("pixel", 10, 20)
    assert fake_adb.calls[-1].endswith("exec-out input tap 10 20")


async def test_rotate_swaps_the_reported_size(manager, fake_adb, monkeypatch):
    from app.core.android_emulator import manager as manager_mod

    monkeypatch.setattr(manager_mod.asyncio, "sleep", _no_sleep)
    dev = _running(manager)
    seen = []
    dev.listeners.append(lambda d: seen.append((d.width, d.height)))
    fake_adb.reply("dumpsys input", b"    SurfaceOrientation: 3\n")
    await manager.rotate("pixel")
    assert (dev.width, dev.height) == (2400, 1080) and seen == [(2400, 1080)]
    assert dev.rotation == 3
    assert any(call.endswith("emu rotate") for call in fake_adb.calls)


async def _no_sleep(delay, result=None):
    return result


@pytest.mark.parametrize("text, expected", [
    ("    SurfaceOrientation: 1\n", 1),
    ("Viewport INTERNAL: displayId=0, orientation=ROTATION_270, logicalFrame=[0, 0, 2400, 1080]", 3),
    ("  mCurrentRotation=ROTATION_180", 2),
    ("mRotation=0 mAltOrientation=false", 0),
    ("nothing useful", None),
])
def test_parse_rotation(text, expected):
    assert EmulatorManager.parse_rotation(text) == expected


def test_touches_map_to_the_panels_natural_axes(manager):
    dev = manager.device("pixel")
    # Corners of the screen as shown, per rotation → natural portrait pixels.
    dev.rotation = 0
    assert EmulatorManager.natural_point(dev, 10, 20) == (10, 20)
    dev.rotation = 1  # ROTATION_90: shown 2400x1080
    assert EmulatorManager.natural_point(dev, 0, 0) == (1079, 0)
    assert EmulatorManager.natural_point(dev, 2399, 1079) == (0, 2399)
    dev.rotation = 2
    assert EmulatorManager.natural_point(dev, 0, 0) == (1079, 2399)
    dev.rotation = 3  # ROTATION_270
    assert EmulatorManager.natural_point(dev, 0, 0) == (0, 2399)
    assert EmulatorManager.natural_point(dev, 2399, 1079) == (1079, 0)


async def test_landscape_taps_are_sent_on_natural_axes(manager, fake_adb):
    grpc = FakeGrpc()
    dev = _running(manager, grpc)
    dev.rotation, dev.width, dev.height = 1, 2400, 1080
    await manager.tap("pixel", 2000, 100)
    assert grpc.calls[0] == ("touch", 979, 2000, 512)


async def test_commands_need_a_running_device(manager):
    with pytest.raises(EmulatorError) as exc:
        await manager.tap("pixel", 1, 1)
    assert exc.value.code == "not_running" and "Start it first" in exc.value.message


async def test_install_and_launch(manager, fake_adb, tmp_path):
    _running(manager)
    path = tmp_path / "app.apk"
    path.write_bytes(_zip([("AndroidManifest.xml", _axml("com.example.todo"), FILE)]))
    fake_adb.reply(" install -r -t -g", b"Performing Streamed Install\nSuccess\n")
    assert await manager.install("pixel", path) == "com.example.todo"
    fake_adb.reply("resolve-activity", b"priority=0\ncom.example.todo/.MainActivity\n")
    fake_adb.reply("am start -W", b"Status: ok\n")
    assert await manager.launch("pixel", "com.example.todo") == "com.example.todo/.MainActivity"
    assert any("am start -W -n com.example.todo/.MainActivity" in call for call in fake_adb.calls)
    fake_adb.reply(" install -r -t -g", b"Failure [INSTALL_FAILED_OLDER_SDK]\n", returncode=1)
    with pytest.raises(EmulatorError, match="INSTALL_FAILED_OLDER_SDK"):
        await manager.install("pixel", path)
    with pytest.raises(EmulatorError) as exc:
        await manager.launch("pixel", "com.x; reboot")
    assert exc.value.code == "bad_package"


async def test_ui_and_logcat(manager, fake_adb):
    _running(manager)
    fake_adb.reply("uiautomator dump", DUMP.encode())
    assert "Sign in" in await manager.ui("pixel", 1.0)
    fake_adb.reply("pidof com.example.todo", b"321\n")
    fake_adb.reply("logcat", b"I/Todo( 321): started\n")
    assert "started" in await manager.logcat("pixel", "com.example.todo", 50)
    assert any(call.endswith("logcat --pid=321 -d -v brief") for call in fake_adb.calls)
    fake_adb.reply("logcat -d -v brief Todo:D *:S",
                   b"--------- beginning of main\n" + b"".join(b"I/Todo: line %d\n" % i for i in range(10)))
    tail = await manager.logcat("pixel", "Todo:D *:S", 3)
    assert tail.splitlines() == ["I/Todo: line 7", "I/Todo: line 8", "I/Todo: line 9"]
    assert "-t" not in fake_adb.calls[-1].split()  # -t counts lines before filtering
    with pytest.raises(EmulatorError):
        await manager.logcat("pixel", "x; rm -rf /")


async def test_idle_devices_stop_unless_watched(manager, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "emulator_idle_timeout_seconds", 60)
    dev = _running(manager)
    stopped = []

    async def fake_stop(device_id):
        stopped.append(device_id)
        manager.device(device_id).state = "stopped"
        return manager.device(device_id)

    monkeypatch.setattr(manager, "stop", fake_stop)
    dev.last_used = 0.0
    dev.viewers = 1
    assert await manager.stop_idle(now=1000.0) == []
    dev.viewers = 0
    assert await manager.stop_idle(now=30.0) == []
    assert await manager.stop_idle(now=1000.0) == ["pixel"]
    monkeypatch.setattr(settings, "emulator_idle_timeout_seconds", 0)
    dev.state = "running"
    assert await manager.stop_idle(now=10 ** 6) == []


def test_cleanup_orphans_only_touches_processes_from_the_sdk(layout, monkeypatch):
    import psutil

    from app.core.android_emulator import manager as manager_mod

    layout.root.mkdir(parents=True)
    terminated = []

    class P:
        def __init__(self, pid, exe):
            self.info = {"pid": pid, "exe": exe, "uids": None}

        def terminate(self):
            terminated.append(self.info["pid"])

        def kill(self):
            pass

    procs = [P(1, str(layout.root / "emulator/qemu/linux-x86_64/qemu-system-x86_64-headless")),
             P(2, "/usr/bin/python3"), P(3, str(layout.root / "platform-tools/adb"))]
    monkeypatch.setattr(psutil, "process_iter", lambda attrs: procs)
    monkeypatch.setattr(psutil, "wait_procs", lambda victims, timeout: (victims, []))
    folder = avd.write_avd(layout, avd.PROFILES["pixel"], IMAGE)
    (folder / "hardware-qemu.ini.lock").write_text("1")
    assert manager_mod.cleanup_orphans(layout) == 2
    assert terminated == [1, 3]
    assert not (folder / "hardware-qemu.ini.lock").exists()
