"""The worker's emulators: one shared manager for every session and the panel.

``get_manager()`` is the process-wide instance. It owns the SDK install job,
the devices (``avd.PROFILES``; "pixel" is created on its first start), their
emulator processes, and the per-device screen stream. Every subprocess runs
through asyncio and every disk or CPU step through a worker thread, so no
call here holds the event loop.

Input goes over the emulator's gRPC service (``grpc_control``): a tap costs a
few milliseconds there, against 300-500 ms for ``adb shell input``. adb is
the fallback when gRPC is not answering.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
import socket
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.core.android_emulator import adb as adb_mod
from app.core.android_emulator import avd, frames, sdk_repo
from app.core.android_emulator.grpc_control import KEY_VALUES, RGB888, GrpcControl
from app.core.android_emulator.host import LICENSE_URL, check_host
from app.core.android_emulator.installer import InstallProgress, run_install
from app.core.android_emulator.layout import SdkLayout
from app.core.process_tree import kill_process_tree, spawn_kwargs

logger = logging.getLogger(__name__)

BOOT_TIMEOUT_SECONDS = 300.0
STOP_TIMEOUT_SECONDS = 20.0
IDLE_CHECK_SECONDS = 60.0
CONSOLE_PORTS = range(5554, 5586, 2)
GRPC_PORT_BASE = 8554
#: Long edge of the agent's screenshot; taps and UI bounds use the same space.
AGENT_LONG_EDGE = 1280
TOUCH_PRESSURE = 512
SWIPE_STEP_MS = 16
LOG_TAIL_BYTES = 4000
#: Display rotation as Android reports it (Surface.ROTATION_*), for touch mapping.
ROTATION_COMMAND = ("dumpsys input | grep -iE 'SurfaceOrientation|orientation=' | head -n 8; "
                    "dumpsys window displays | grep -iE 'mCurrentRotation|mRotation=' | head -n 3")
_ROTATION_PATTERNS = (
    re.compile(r"SurfaceOrientation:\s*(\d)"),
    re.compile(r"orientation=ROTATION_(\d+)"),
    re.compile(r"(?:mCurrentRotation|mRotation)=ROTATION_(\d+)"),
    re.compile(r"(?:mCurrentRotation|mRotation)=(\d)\b"),
    re.compile(r"\borientation=(\d)\b"),
)

#: Android keycodes for the adb fallback.
_ADB_KEYCODES = {
    "HOME": "KEYCODE_HOME", "BACK": "KEYCODE_BACK", "APP_SWITCH": "KEYCODE_APP_SWITCH",
    "ENTER": "KEYCODE_ENTER", "DEL": "KEYCODE_DEL", "POWER": "KEYCODE_POWER",
    "VOLUME_UP": "KEYCODE_VOLUME_UP", "VOLUME_DOWN": "KEYCODE_VOLUME_DOWN",
    "TAB": "KEYCODE_TAB", "ESCAPE": "KEYCODE_ESCAPE", "MENU": "KEYCODE_MENU",
}
KEYS = tuple(_ADB_KEYCODES)


class EmulatorError(Exception):
    """A refusal or failure the caller shows as is. ``status`` is the HTTP code."""

    def __init__(self, message: str, status: int = 409, code: str = "emulator_error") -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


def _settings():
    from app.core.config import settings

    return settings


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _grpc_error_text(exc: BaseException) -> str:
    """A short reason from a gRPC failure (status code and details)."""
    code = getattr(exc, "code", None)
    details = getattr(exc, "details", None)
    try:
        if callable(code) and callable(details):
            return f"{code().name}: {details()}"[:300]
    except Exception:  # noqa: BLE001
        pass
    return f"{type(exc).__name__}: {exc}"[:300]


@dataclass
class Device:
    profile: avd.DeviceProfile
    layout: SdkLayout
    state: str = "stopped"  # stopped | booting | running | error
    message: str = ""
    api: int = 0
    console_port: int = 0
    proc: Optional[asyncio.subprocess.Process] = None
    grpc: Optional[GrpcControl] = None
    #: Where screen frames and input go: "grpc", "adb" (gRPC unavailable), or
    #: "" while not running. ``grpc_error`` says why gRPC is not used.
    frame_source: str = ""
    grpc_error: str = ""
    #: Current orientation's size in device pixels.
    width: int = 0
    height: int = 0
    #: Android display rotation (0-3, quarter turns). Touches go to the panel
    #: in its natural (portrait) orientation, so they are mapped through it.
    rotation: int = 0
    rotation_task: Optional[asyncio.Task] = None
    last_used: float = field(default_factory=time.monotonic)
    viewers: int = 0
    stopping: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    boot_task: Optional[asyncio.Task] = None
    exit_task: Optional[asyncio.Task] = None
    booted: asyncio.Event = field(default_factory=asyncio.Event)
    #: Called with the device on every state change (panel streams listen).
    listeners: List[Callable[["Device"], None]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.width, self.height = self.profile.width, self.profile.height

    @property
    def id(self) -> str:
        return self.profile.id

    @property
    def serial(self) -> str:
        return f"emulator-{self.console_port}" if self.console_port else ""

    @property
    def adb(self) -> adb_mod.Adb:
        return adb_mod.Adb(self.layout, self.serial)

    @property
    def log_path(self) -> Path:
        return self.layout.logs / f"{self.id}.log"

    def touch(self) -> None:
        self.last_used = time.monotonic()

    def agent_scale(self) -> float:
        longest = max(self.width, self.height)
        return min(1.0, AGENT_LONG_EDGE / longest) if longest else 1.0

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "name": self.profile.name, "state": self.state, "api": self.api,
                "width": self.width, "height": self.height,
                "frame_source": self.frame_source if self.state == "running" else ""}

    def set_state(self, state: str, message: str = "") -> None:
        changed = (state, message) != (self.state, self.message)
        self.state, self.message = state, message
        if state != "running":
            self.booted.clear()
        else:
            self.booted.set()
        if changed:
            for listener in list(self.listeners):
                try:
                    listener(self)
                except Exception:  # noqa: BLE001 — a viewer never breaks the device
                    logger.debug("emulator state listener failed", exc_info=True)


class EmulatorManager:
    def __init__(self, layout: Optional[SdkLayout] = None) -> None:
        self._layout = layout
        self.progress = InstallProgress()
        self._install_task: Optional[asyncio.Task] = None
        self._install_cancel = threading.Event()
        self._devices: Dict[str, Device] = {}
        self._idle_task: Optional[asyncio.Task] = None
        self._adb_started = False

    # ── configuration ──────────────────────────────────────────────────────
    @property
    def layout(self) -> SdkLayout:
        if self._layout is None:
            self._layout = SdkLayout()
        return self._layout

    @property
    def system_image(self) -> str:
        record = self.layout.read_record()
        return record.get("system_image") or _settings().emulator_system_image

    @staticmethod
    def max_devices() -> int:
        return max(1, int(_settings().emulator_max_devices or 1))

    @staticmethod
    def idle_timeout() -> float:
        return float(_settings().emulator_idle_timeout_seconds or 0)

    def device(self, device_id: str) -> Device:
        profile = avd.PROFILES.get(device_id or "")
        if profile is None:
            raise EmulatorError(f"No device named {device_id!r}. Use \"pixel\".", 404, "unknown_device")
        found = self._devices.get(profile.id)
        if found is None:
            found = self._devices[profile.id] = Device(profile, self.layout)
        return found

    # ── status ─────────────────────────────────────────────────────────────
    def _installing(self) -> bool:
        return self._install_task is not None and not self._install_task.done()

    def _status_sync(self) -> Dict[str, Any]:
        host = check_host(emulator_bin=self.layout.emulator_bin, env=self.layout.env())
        image = self.system_image
        installed = host.host_os is not None and self.layout.installed(image)
        install = self.progress.snapshot()
        if host.reason == "unsupported_arch":
            reason, message = host.reason, host.message
        elif self._installing():
            reason, message = "installing", install["message"] or "Installing the Android emulator"
        elif not host.ok and host.reason != "sdk_missing":
            reason, message = host.reason, host.message
            if not installed:
                message += " The Android SDK is not installed yet either."
        elif not installed and install["state"] == "error":
            reason, message = "error", install["message"]
        elif not installed:
            reason, message = "sdk_missing", (
                "The Android emulator is not installed. Installing it downloads about 2 GB "
                "from Google and needs you to accept the Android SDK License.")
        else:
            reason, message = "ready", ""
        try:
            api = sdk_repo.system_image_parts(image)[0]
        except sdk_repo.ManifestError:
            api = 0
        devices = []
        if installed:
            for profile in avd.PROFILES.values():
                dev = self.device(profile.id)
                dev.api = dev.api or api
                devices.append(dev.to_dict())
        return {
            "available": reason == "ready",
            "reason": reason,
            "message": message,
            "install": None if install["state"] == "idle" else install,
            "license_url": LICENSE_URL,
            "devices": devices,
        }

    async def status(self) -> Dict[str, Any]:
        return await asyncio.to_thread(self._status_sync)

    async def _require_ready(self) -> None:
        status = await self.status()
        if not status["available"]:
            raise EmulatorError(status["message"] or "The Android emulator is not available.",
                                409, status["reason"])

    # ── SDK install ────────────────────────────────────────────────────────
    async def setup(self, accept_license: bool, system_image: Optional[str] = None) -> Dict[str, Any]:
        if accept_license is not True:
            raise EmulatorError(
                f"Installing the Android emulator needs the owner to accept the Android SDK "
                f"License ({LICENSE_URL}). Send accept_license: true once they have.",
                400, "license_required")
        host = await asyncio.to_thread(check_host, emulator_bin=self.layout.emulator_bin, env=self.layout.env())
        if host.host_os is None:
            raise EmulatorError(host.message, 409, host.reason)
        image = system_image or _settings().emulator_system_image
        try:
            _api, _tag, abi = sdk_repo.system_image_parts(image)
        except sdk_repo.ManifestError as exc:
            raise EmulatorError(str(exc), 400, "bad_system_image") from None
        if abi not in ("x86_64",):
            raise EmulatorError(f"{image} does not run on this worker; use an x86_64 image.",
                                400, "bad_system_image")
        if self._installing():
            return await self.status()
        if any(dev.state in ("booting", "running") for dev in self._devices.values()):
            raise EmulatorError("Stop the running emulator before installing or updating the SDK.",
                                409, "device_running")
        self.progress = InstallProgress(state="downloading", message="Starting the Android SDK install")
        self._install_cancel = threading.Event()
        progress, cancel, layout = self.progress, self._install_cancel, self.layout

        def job() -> None:
            ok, _message = run_install(layout, host, image, progress, cancel)
            if ok:
                record = layout.read_record()
                record["system_image"] = image
                layout.write_record(record)

        self._install_task = asyncio.create_task(asyncio.to_thread(job), name="android-sdk-install")
        return await self.status()

    # ── start / stop ───────────────────────────────────────────────────────
    async def start(self, device_id: str) -> Device:
        """Begin booting ``device_id``; returns at once (``wait_running`` waits)."""
        dev = self.device(device_id)
        async with dev.lock:
            if dev.state in ("booting", "running"):
                dev.touch()
                return dev
            await self._require_ready()
            busy = [d for d in self._devices.values() if d.state in ("booting", "running")]
            if len(busy) >= self.max_devices():
                raise EmulatorError(
                    f"This worker runs at most {self.max_devices()} emulator(s) at once; "
                    f"{', '.join(d.id for d in busy)} is running.", 409, "device_limit")
            dev.set_state("booting", "Starting the emulator")
            try:
                await self._spawn(dev)
            except EmulatorError as exc:
                dev.set_state("error", exc.message)
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception("Emulator %s failed to start", dev.id)
                dev.set_state("error", f"The emulator failed to start: {exc}")
                raise EmulatorError(dev.message, 500, "start_failed") from None
            dev.touch()
            dev.boot_task = asyncio.create_task(self._boot(dev), name=f"emulator-boot-{dev.id}")
            self._ensure_idle_watch()
        return dev

    async def wait_running(self, dev: Device, timeout: float = BOOT_TIMEOUT_SECONDS) -> Device:
        deadline = time.monotonic() + timeout
        while dev.state == "booting":
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                await asyncio.wait_for(dev.booted.wait(), min(remaining, 2.0))
            except asyncio.TimeoutError:
                pass
        if dev.state != "running":
            raise EmulatorError(dev.message or f"The emulator is {dev.state}.", 409, "not_running")
        return dev

    async def _spawn(self, dev: Device) -> None:
        layout = self.layout
        image = await asyncio.to_thread(lambda: self.system_image)
        dev.api = sdk_repo.system_image_parts(image)[0]
        port = next((p for p in CONSOLE_PORTS if _port_free(p) and _port_free(p + 1)), None)
        if port is None:
            raise EmulatorError("No free emulator console port between 5554 and 5584.", 409, "no_port")
        grpc_port = GRPC_PORT_BASE + (port - CONSOLE_PORTS.start) // 2
        if not _port_free(grpc_port):
            grpc_port = 0

        def prepare() -> None:
            for folder in (layout.avd_home, layout.user_home, layout.logs):
                folder.mkdir(parents=True, exist_ok=True)
            layout.runtime_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            avd.write_avd(layout, dev.profile, image)
            avd.clear_locks(layout, dev.profile)
            return layout.console_token()

        token = await asyncio.to_thread(prepare)
        await adb_mod.start_server(layout)
        self._adb_started = True
        argv = [
            str(layout.emulator_bin), "-avd", dev.id,
            "-no-window", "-no-audio", "-no-boot-anim", "-no-snapshot",
            "-gpu", "swiftshader_indirect", "-no-metrics", "-crash-report-mode", "never",
            "-skip-adb-auth", "-netdelay", "none", "-netspeed", "full",
            "-port", str(port),
        ]
        if sys.platform == "win32":
            # Never fall back to a many-minute software CPU boot on Windows.
            argv += ["-accel", "on"]
        if grpc_port:
            # With a token the emulator listens on loopback only; without one
            # its control port is open on every interface, unauthenticated.
            argv += ["-grpc", str(grpc_port), "-grpc-use-token"]
        log = await asyncio.to_thread(open, dev.log_path, "wb")
        try:
            dev.proc = await asyncio.create_subprocess_exec(
                *argv, stdin=asyncio.subprocess.DEVNULL, stdout=log, stderr=asyncio.subprocess.STDOUT,
                env=layout.env(), **spawn_kwargs(),
            )
        finally:
            log.close()
        dev.console_port = port
        dev.grpc = GrpcControl(grpc_port, token) if grpc_port else None
        dev.frame_source, dev.grpc_error = "", "" if grpc_port else "no free gRPC port"
        dev.width, dev.height = dev.profile.width, dev.profile.height
        dev.rotation = 0
        dev.stopping = False
        try:
            from app.core import process_ledger

            await asyncio.to_thread(process_ledger.record_process, dev.proc.pid, "android-emulator")
        except Exception:  # noqa: BLE001
            logger.debug("process ledger record failed", exc_info=True)
        dev.exit_task = asyncio.create_task(self._watch_exit(dev, dev.proc), name=f"emulator-exit-{dev.id}")
        logger.info("Emulator %s started (pid %s, %s, gRPC %s)", dev.id, dev.proc.pid, dev.serial,
                    grpc_port or "off")

    async def _log_tail(self, dev: Device) -> str:
        def read() -> str:
            try:
                with open(dev.log_path, "rb") as fh:
                    fh.seek(0, os.SEEK_END)
                    fh.seek(max(0, fh.tell() - LOG_TAIL_BYTES))
                    return fh.read().decode("utf-8", "replace")
            except OSError:
                return ""

        text = await asyncio.to_thread(read)
        lines = [line for line in text.splitlines() if "ERROR" in line or "FATAL" in line or "PANIC" in line]
        return " ".join(lines[-3:])[:600]

    async def _watch_exit(self, dev: Device, proc: asyncio.subprocess.Process) -> None:
        code = await proc.wait()
        try:
            from app.core import process_ledger

            await asyncio.to_thread(process_ledger.forget_process, proc.pid)
        except Exception:  # noqa: BLE001
            pass
        if dev.proc is not proc:
            return
        dev.proc = None
        if dev.grpc is not None:
            await dev.grpc.close()
            dev.grpc = None
        if dev.stopping:
            if dev.state != "error":  # a failed boot keeps its reason
                dev.set_state("stopped")
            return
        tail = await self._log_tail(dev)
        logger.warning("Emulator %s exited unexpectedly (code %s): %s", dev.id, code, tail)
        dev.set_state("error", f"The emulator exited unexpectedly (code {code}). {tail}".strip())

    async def _boot(self, dev: Device) -> None:
        started = time.monotonic()
        try:
            await self._wait_boot(dev)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            if dev.state == "booting":
                message = exc.message if isinstance(exc, EmulatorError) else str(exc)
                dev.set_state("error", message)
                await self._terminate(dev)
            return
        logger.info("Emulator %s booted in %.0fs", dev.id, time.monotonic() - started)
        dev.touch()
        dev.set_state("running", "")

    async def _wait_boot(self, dev: Device) -> None:
        deadline = time.monotonic() + BOOT_TIMEOUT_SECONDS
        adb = dev.adb
        while True:
            if dev.state != "booting" or dev.proc is None:
                raise EmulatorError(dev.message or "The emulator stopped while booting.")
            if time.monotonic() > deadline:
                raise EmulatorError(f"The emulator did not finish booting in {BOOT_TIMEOUT_SECONDS:.0f}s.")
            try:
                if await adb.getprop("sys.boot_completed", timeout=5) == "1":
                    break
            except adb_mod.AdbError:
                pass
            await asyncio.sleep(1.0)
        # Keep the screen on, and make UI tests quicker and steadier.
        await adb.shell(
            "svc power stayon true; settings put global window_animation_scale 0; "
            "settings put global transition_animation_scale 0; "
            "settings put global animator_duration_scale 0; input keyevent KEYCODE_WAKEUP; "
            "wm dismiss-keyguard", timeout=20)
        await self._connect_grpc(dev)
        await self.refresh_rotation(dev)

    async def _connect_grpc(self, dev: Device) -> None:
        """Check the gRPC control answers, with the token the emulator expects.

        The emulator writes its gRPC port and token to a discovery file; that
        token wins over the console token it was started with. When gRPC does
        not answer, frames and input fall back to adb (about 2 fps instead of
        10+): that is logged as a warning and shown as ``frame_source``.
        """
        grpc = dev.grpc
        if grpc is None:
            dev.frame_source = "adb"
            if dev.grpc_error:
                logger.warning("Emulator %s: no gRPC (%s); frames and input use adb", dev.id, dev.grpc_error)
            return
        error = ""
        for _ in range(10):
            found = await asyncio.to_thread(self.layout.discovery, dev.proc.pid) if dev.proc else {}
            token, port = found.get("grpc.token", ""), found.get("grpc.port", "")
            if token and token != grpc.token or port.isdigit() and int(port) != grpc.port:
                await grpc.close()
                grpc = dev.grpc = GrpcControl(int(port) if port.isdigit() else grpc.port,
                                              token or grpc.token)
            try:
                await grpc.status()
                dev.frame_source, dev.grpc_error = "grpc", ""
                return
            except Exception as exc:  # noqa: BLE001 — gRPC comes up a little after adb
                error = _grpc_error_text(exc)
                await asyncio.sleep(1.0)
        logger.warning("Emulator %s: gRPC at %s is not answering (%s); frames and input use adb",
                       dev.id, grpc.target, error)
        await grpc.close()
        dev.grpc = None
        dev.frame_source, dev.grpc_error = "adb", error

    async def stop(self, device_id: str) -> Device:
        dev = self.device(device_id)
        async with dev.lock:
            if dev.proc is None:
                if dev.state != "stopped":
                    dev.set_state("stopped")
                return dev
            await self._terminate(dev)
        return dev

    async def _terminate(self, dev: Device) -> None:
        proc = dev.proc
        if proc is None:
            dev.set_state("stopped")
            return
        dev.stopping = True
        if dev.boot_task is not None and dev.boot_task is not asyncio.current_task():
            dev.boot_task.cancel()
        try:
            await dev.adb.emu("kill", timeout=10)
        except Exception:  # noqa: BLE001 — the process signals below still stop it
            pass
        try:
            await asyncio.wait_for(proc.wait(), STOP_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            survivors = await kill_process_tree(proc, own_process_group=True, grace=10)
            if survivors:
                dev.set_state("error", "Could not stop emulator processes: " + ", ".join(map(str, survivors)))
                raise EmulatorError(dev.message, 500, "stop_failed")
        if dev.exit_task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(dev.exit_task), 5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
        if dev.proc is proc:
            dev.proc = None
        await asyncio.to_thread(avd.clear_locks, self.layout, dev.profile)
        if dev.state != "error":
            dev.set_state("stopped")

    # ── idle stop ──────────────────────────────────────────────────────────
    def _ensure_idle_watch(self) -> None:
        if self._idle_task is None or self._idle_task.done():
            self._idle_task = asyncio.create_task(self._idle_watch(), name="emulator-idle-stop")

    async def _idle_watch(self) -> None:
        while any(d.proc is not None for d in self._devices.values()):
            await asyncio.sleep(IDLE_CHECK_SECONDS)
            await self.stop_idle()

    async def stop_idle(self, now: Optional[float] = None) -> List[str]:
        limit = self.idle_timeout()
        if limit <= 0:
            return []
        now = time.monotonic() if now is None else now
        stopped = []
        for dev in list(self._devices.values()):
            if dev.state == "running" and dev.viewers == 0 and now - dev.last_used >= limit:
                logger.info("Emulator %s idle for %.0fs; stopping it", dev.id, now - dev.last_used)
                await self.stop(dev.id)
                stopped.append(dev.id)
        return stopped

    # ── use ────────────────────────────────────────────────────────────────
    def running(self, device_id: str) -> Device:
        dev = self.device(device_id)
        if dev.state != "running":
            hint = "Start it first." if dev.state in ("stopped", "error") else "It is still starting."
            raise EmulatorError(f"The {dev.profile.name} emulator is {dev.state}. {hint}",
                                409, "not_running")
        dev.touch()
        return dev

    async def capture(self, dev: Device, long_edge: int = 0) -> Tuple[bytes, int, int]:
        """RGB888 pixels of the screen, at most ``long_edge`` on the longer side."""
        if dev.grpc is not None:
            try:
                want_w, want_h = frames.fit(dev.width, dev.height, long_edge) if long_edge else (0, 0)
                frame = await dev.grpc.screenshot(want_w, want_h, RGB888)
                if frame.width and frame.height:
                    self._note_orientation(dev, frame.width, frame.height)
                    return frame.pixels, frame.width, frame.height
            except Exception:  # noqa: BLE001
                logger.debug("gRPC screenshot failed; using adb", exc_info=True)
        result = await dev.adb.run("exec-out", "screencap", timeout=20)
        if result.returncode != 0:
            raise EmulatorError(f"screencap failed: {result.error_text[:200]}", 500, "screenshot_failed")
        pixels, width, height = await asyncio.to_thread(frames.raw_screencap_to_rgb, result.stdout)
        self._note_orientation(dev, width, height)
        return pixels, width, height

    def _note_orientation(self, dev: Device, width: int, height: int) -> None:
        """A captured frame's shape; a flip (an app or the user turned the
        phone) re-reads the display rotation that touches are mapped by."""
        landscape = width > height
        base_w, base_h = dev.profile.width, dev.profile.height
        size = (base_h, base_w) if landscape else (base_w, base_h)
        if (dev.width, dev.height) != size:
            dev.width, dev.height = size
        if bool(dev.rotation % 2) != landscape:
            dev.rotation = 1 if landscape else 0  # best guess until Android answers
            if dev.rotation_task is None or dev.rotation_task.done():
                try:
                    dev.rotation_task = asyncio.get_running_loop().create_task(self.refresh_rotation(dev))
                except RuntimeError:
                    pass

    @staticmethod
    def parse_rotation(text: str) -> Optional[int]:
        for pattern in _ROTATION_PATTERNS:
            match = pattern.search(text or "")
            if match:
                value = int(match.group(1))
                return value // 90 if value >= 90 else value % 4
        return None

    async def refresh_rotation(self, dev: Device) -> Optional[int]:
        """Read Android's display rotation; keeps the last value when it cannot."""
        try:
            result = await dev.adb.shell(ROTATION_COMMAND, timeout=10)
        except Exception:  # noqa: BLE001
            return None
        rotation = self.parse_rotation(result.text)
        if rotation is None:
            logger.debug("Emulator %s: display rotation not found in: %s", dev.id, result.text[:300])
            return None
        dev.rotation = rotation
        base_w, base_h = dev.profile.width, dev.profile.height
        dev.width, dev.height = (base_h, base_w) if rotation % 2 else (base_w, base_h)
        return rotation

    @staticmethod
    def natural_point(dev: Device, x: int, y: int) -> Tuple[int, int]:
        """A point on the screen as shown → the same point on the panel's
        natural (portrait) axes, which gRPC touches use. Android's own
        rotation transform (TouchInputMapper), inverted."""
        nw, nh = dev.profile.width, dev.profile.height
        rotation = dev.rotation % 4
        if rotation == 1:
            return nw - 1 - y, x
        if rotation == 2:
            return nw - 1 - x, nh - 1 - y
        if rotation == 3:
            return y, nh - 1 - x
        return x, y

    async def screenshot_jpeg(self, device_id: str, long_edge: int = AGENT_LONG_EDGE,
                              quality: int = 80) -> Tuple[bytes, int, int, Device]:
        dev = self.running(device_id)
        pixels, width, height = await self.capture(dev, long_edge)
        size = frames.fit(width, height, long_edge)
        data, out_w, out_h = await asyncio.to_thread(
            frames.rgb_to_jpeg, pixels, width, height, size=size, quality=quality)
        return data, out_w, out_h, dev

    def _check_point(self, dev: Device, x: int, y: int) -> Tuple[int, int]:
        x, y = int(x), int(y)
        if not (0 <= x < dev.width and 0 <= y < dev.height):
            raise EmulatorError(f"({x}, {y}) is off the {dev.width}x{dev.height} screen.", 400, "bad_point")
        return x, y

    async def tap(self, device_id: str, x: int, y: int) -> None:
        dev = self.running(device_id)
        x, y = self._check_point(dev, x, y)
        if dev.grpc is not None:
            try:
                nx, ny = self.natural_point(dev, x, y)
                await dev.grpc.touch(nx, ny, TOUCH_PRESSURE)
                await asyncio.sleep(0.05)
                await dev.grpc.touch(nx, ny, 0)
                return
            except Exception:  # noqa: BLE001
                logger.debug("gRPC tap failed; using adb", exc_info=True)
        await self._adb_input(dev, f"input tap {x} {y}")

    async def swipe(self, device_id: str, x1: int, y1: int, x2: int, y2: int,
                    duration_ms: int = 300) -> None:
        dev = self.running(device_id)
        x1, y1 = self._check_point(dev, x1, y1)
        x2, y2 = self._check_point(dev, x2, y2)
        duration_ms = max(50, min(int(duration_ms or 300), 5000))
        if dev.grpc is not None:
            try:
                steps = max(2, duration_ms // SWIPE_STEP_MS)
                started = time.monotonic()
                for i in range(steps + 1):
                    fx = x1 + (x2 - x1) * i / steps
                    fy = y1 + (y2 - y1) * i / steps
                    await dev.grpc.touch(*self.natural_point(dev, round(fx), round(fy)), TOUCH_PRESSURE)
                    target = started + duration_ms / 1000 * (i + 1) / (steps + 1)
                    delay = target - time.monotonic()
                    if delay > 0:
                        await asyncio.sleep(delay)
                await dev.grpc.touch(*self.natural_point(dev, x2, y2), 0)
                return
            except Exception:  # noqa: BLE001
                logger.debug("gRPC swipe failed; using adb", exc_info=True)
        await self._adb_input(dev, f"input swipe {x1} {y1} {x2} {y2} {duration_ms}")

    async def type_text(self, device_id: str, text: str) -> None:
        dev = self.running(device_id)
        if not text:
            return
        if len(text) > 2000:
            raise EmulatorError("Type at most 2000 characters at a time.", 400, "text_too_long")
        if dev.grpc is not None and all(32 <= ord(c) < 127 or c == "\n" for c in text):
            try:
                await dev.grpc.text(text)
                return
            except Exception:  # noqa: BLE001
                logger.debug("gRPC text failed; using adb", exc_info=True)
        # `input text` takes %s for spaces and needs shell quoting for the rest.
        await self._adb_input(dev, "input text " + shlex.quote(text.replace(" ", "%s")))

    async def key(self, device_id: str, key: str) -> None:
        dev = self.running(device_id)
        name = (key or "").upper()
        if name not in _ADB_KEYCODES:
            raise EmulatorError(f"Unknown key {key!r}; use one of {', '.join(KEYS)}.", 400, "bad_key")
        if dev.grpc is not None and name in KEY_VALUES:
            try:
                await dev.grpc.key(KEY_VALUES[name])
                return
            except Exception:  # noqa: BLE001
                logger.debug("gRPC key failed; using adb", exc_info=True)
        await self._adb_input(dev, f"input keyevent {_ADB_KEYCODES[name]}")

    async def rotate(self, device_id: str) -> None:
        dev = self.running(device_id)
        result = await dev.adb.emu("rotate")
        if result.returncode != 0:
            raise EmulatorError(f"rotate failed: {result.error_text[:200]}", 500, "rotate_failed")
        before = dev.rotation
        dev.width, dev.height = dev.height, dev.width
        dev.rotation = (before + 1) % 4  # until Android reports the turn it made
        # Android takes a moment to turn the display; read where it landed.
        for _ in range(6):
            await asyncio.sleep(0.5)
            rotation = await self.refresh_rotation(dev)
            if rotation is not None and rotation != before:
                break
        for listener in list(dev.listeners):
            try:
                listener(dev)
            except Exception:  # noqa: BLE001
                pass

    async def _adb_input(self, dev: Device, command: str) -> None:
        result = await dev.adb.shell(command, timeout=20)
        if result.returncode != 0:
            raise EmulatorError(f"{command.split()[1]} failed: {result.error_text[:200]}", 500, "input_failed")

    async def ui(self, device_id: str, scale: float = 1.0) -> str:
        from app.core.android_emulator import ui_tree

        dev = self.running(device_id)
        result = await dev.adb.shell(
            "uiautomator dump /sdcard/window_dump.xml >/dev/null && cat /sdcard/window_dump.xml", timeout=30)
        if result.returncode != 0 or "<hierarchy" not in result.text:
            raise EmulatorError(f"The UI dump failed: {result.error_text[:200] or 'no hierarchy'}",
                                500, "ui_failed")
        try:
            return await asyncio.to_thread(ui_tree.compact, result.text, scale)
        except Exception as exc:  # noqa: BLE001
            raise EmulatorError(f"The UI dump could not be read: {exc}", 500, "ui_failed") from None

    async def logcat(self, device_id: str, filter_spec: str = "", lines: int = 200) -> str:
        dev = self.running(device_id)
        lines = max(1, min(int(lines or 200), 2000))
        # `-t N` counts lines before the filter applies, so a filtered tail
        # came back empty: filter the whole buffer, keep the last N here.
        args = ["logcat", "-d", "-v", "brief"]
        spec = (filter_spec or "").strip()
        if spec:
            if any(c in spec for c in ";|&`$<>\\\"'"):
                raise EmulatorError("filter is a logcat filter spec (Tag:Level ...) or a package name.",
                                    400, "bad_filter")
            if ":" not in spec and "." in spec and " " not in spec:
                pid = (await dev.adb.shell(f"pidof {shlex.quote(spec)}", timeout=10)).text.split()
                if not pid:
                    raise EmulatorError(f"{spec} is not running.", 409, "not_running_package")
                args.insert(1, f"--pid={pid[0]}")
            else:
                args.extend(spec.split())
        result = await dev.adb.run(*args, timeout=30)
        if result.returncode != 0:
            raise EmulatorError(f"logcat failed: {result.error_text[:200]}", 500, "logcat_failed")
        kept = [line for line in result.text.splitlines()
                if line.strip() and not line.startswith("--------- beginning of")]
        return "\n".join(kept[-lines:])[-60_000:]

    async def install(self, device_id: str, apk_path: Path) -> str:
        """Install (or update) an APK; returns its package name."""
        from app.core.android_emulator.apk import ApkError, apk_package

        dev = self.running(device_id)
        try:
            package = await asyncio.to_thread(apk_package, apk_path)
        except ApkError as exc:
            raise EmulatorError(str(exc), 400, "bad_apk") from None
        except OSError as exc:
            raise EmulatorError(f"The APK could not be read: {exc.strerror or exc}", 400, "bad_apk") from None
        result = await dev.adb.run("install", "-r", "-t", "-g", str(apk_path), timeout=240)
        output = (result.stdout + result.stderr).decode("utf-8", "replace")
        if result.returncode != 0 or "Success" not in output:
            failure = next((line.strip() for line in output.splitlines() if "Failure" in line
                            or "INSTALL_FAILED" in line or "error" in line.lower()), output.strip()[:300])
            raise EmulatorError(f"The install failed: {failure}", 400, "install_failed")
        dev.touch()
        return package

    async def launch(self, device_id: str, package: str, activity: str = "") -> str:
        dev = self.running(device_id)
        allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._$/")
        if not package or not set(package) <= allowed or not set(activity or "") <= allowed:
            raise EmulatorError("package and activity are Java names like com.example.app and .MainActivity.",
                                400, "bad_package")
        component = ""
        if activity:
            component = activity if "/" in activity else f"{package}/{activity}"
        else:
            resolved = await dev.adb.shell(
                f"cmd package resolve-activity --brief -c android.intent.category.LAUNCHER {package}",
                timeout=20)
            last = (resolved.text.strip().splitlines() or [""])[-1].strip()
            if "/" not in last:
                raise EmulatorError(f"{package} is not installed or has no launcher activity.",
                                    404, "no_activity")
            component = last
        result = await dev.adb.shell(f"am start -W -n {shlex.quote(component)}", timeout=60)
        text = result.text
        if result.returncode != 0 or "Error" in text:
            line = next((ln for ln in text.splitlines() if "Error" in ln), text.strip()[:300])
            raise EmulatorError(f"Launching {component} failed: {line}", 400, "launch_failed")
        return component

    # ── lifecycle ──────────────────────────────────────────────────────────
    async def shutdown(self) -> None:
        """Stop every emulator this process started (app shutdown)."""
        self._install_cancel.set()
        if self._idle_task is not None:
            self._idle_task.cancel()
        for dev in list(self._devices.values()):
            if dev.proc is not None:
                try:
                    await asyncio.wait_for(self.stop(dev.id), STOP_TIMEOUT_SECONDS + 15)
                except Exception:  # noqa: BLE001
                    logger.warning("Emulator %s did not stop cleanly at shutdown", dev.id, exc_info=True)
        if self._adb_started and self.layout.adb.is_file():
            try:
                await adb_mod.run([str(self.layout.adb), "kill-server"], env=self.layout.env(), timeout=10)
            except Exception:  # noqa: BLE001
                pass


def cleanup_orphans(layout: Optional[SdkLayout] = None) -> int:
    """Stop emulator and adb processes run from this install's SDK that no
    running app owns (a crashed or killed previous run), and clear the AVD
    locks they left. Called once at startup, before anything is started.
    Matched by executable path inside the SDK root and by this user only.
    """
    layout = layout or SdkLayout()
    root = layout.root
    if not root.is_dir():
        return 0
    try:
        import psutil
    except ImportError:  # pragma: no cover
        return 0
    root_s = os.path.realpath(str(root)) + os.sep
    me, uid = os.getpid(), os.getuid() if hasattr(os, "getuid") else None
    victims = []
    for proc in psutil.process_iter(["pid", "exe", "uids"]):
        try:
            info = proc.info
            if info["pid"] == me or not info.get("exe"):
                continue
            if uid is not None and info.get("uids") and info["uids"].real != uid:
                continue
            if os.path.realpath(info["exe"]).startswith(root_s):
                victims.append(proc)
        except (psutil.Error, OSError):
            continue
    for proc in victims:
        try:
            proc.terminate()
        except psutil.Error:
            pass
    _gone, alive = psutil.wait_procs(victims, timeout=5)
    for proc in alive:
        try:
            proc.kill()
        except psutil.Error:
            pass
    for profile in avd.PROFILES.values():
        avd.clear_locks(layout, profile)
    if victims:
        logger.warning("Stopped %d leftover Android emulator/adb process(es) from a previous run", len(victims))
    return len(victims)


_manager: Optional[EmulatorManager] = None


def get_manager() -> EmulatorManager:
    global _manager
    if _manager is None:
        _manager = EmulatorManager()
    return _manager
