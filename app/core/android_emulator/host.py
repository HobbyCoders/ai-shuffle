"""Can this worker run the Android emulator? Detection with reason codes.

Linux x86_64 uses KVM; Windows x86_64 uses the emulator's WHPX/AEHD probe.
Each backend returns the manifest's ``host-os``/``host-arch`` archive pair,
or an actionable reason the panel and the agent can show as it is.
"""

from __future__ import annotations

import errno
import os
import platform
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

#: The Android Software Development Kit License Agreement the owner accepts.
LICENSE_URL = "https://developer.android.com/studio/terms"

KVM_PATH = "/dev/kvm"

NO_KVM = (
    "This worker has no /dev/kvm, and the Android emulator needs hardware "
    "virtualization. In Docker, start the container with --device /dev/kvm. "
    "On a Linux desktop, turn on virtualization (Intel VT-x or AMD-V) in the "
    "firmware settings and load the kvm module."
)
KVM_PERMISSION = (
    "/dev/kvm exists, but AI Shuffle cannot open it for reading and writing. "
    "Add the account that runs AI Shuffle to the kvm group (in Docker the image "
    "does this at start when the device belongs to a group), or give the "
    "device read/write permission on the host."
)


@dataclass(frozen=True)
class HostSupport:
    """A panel reason; Windows acceleration failures use ``error`` with guidance."""

    reason: str
    message: str = ""
    #: The manifest's ``host-os`` / ``host-arch`` for this host's archives.
    host_os: Optional[str] = None
    host_arch: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.reason == "ready"


def _linux_x86_64(kvm_path: str = KVM_PATH) -> HostSupport:
    # The archives are known even without KVM, so the SDK can be installed
    # before the device is passed through.
    host = {"host_os": "linux", "host_arch": "x64"}
    if not os.path.exists(kvm_path):
        return HostSupport("no_kvm", NO_KVM, **host)
    try:
        fd = os.open(kvm_path, os.O_RDWR | getattr(os, "O_CLOEXEC", 0))
    except OSError as exc:
        if exc.errno in (errno.EACCES, errno.EPERM):
            return HostSupport("kvm_permission", KVM_PERMISSION, **host)
        if exc.errno in (errno.ENOENT, errno.ENXIO, errno.ENODEV):
            return HostSupport("no_kvm", NO_KVM, **host)
        return HostSupport("kvm_permission", f"{KVM_PERMISSION} ({exc.strerror})", **host)
    os.close(fd)
    return HostSupport("ready", "", **host)


WINDOWS_ACCEL_HELP = (
    "Android emulator hardware acceleration is unavailable. Enable Intel VT-x or AMD-V "
    "in BIOS/UEFI, enable Windows Hypervisor Platform in Windows Features, then reboot. "
    "Alternatively, install the Android Emulator Hypervisor Driver (AEHD) using Android "
    "Studio's SDK Manager if Hyper-V is disabled. Run emulator.exe -accel-check from "
    "the SDK's emulator folder to diagnose the driver."
)


# Cache successes only. Replacing/updating the binary invalidates its entry;
# failures are retried after a driver/feature fix without restarting the app.
_accel_cache: dict[str, tuple[int, int]] = {}
_accel_lock = threading.Lock()


def _windows_x86_64(emulator_bin: Optional[Path], env: Optional[dict],
                    skip_acceleration: bool = False) -> HostSupport:
    host = {"host_os": "windows", "host_arch": "x64"}
    if emulator_bin is None:
        from app.core.android_emulator.layout import SdkLayout

        layout = SdkLayout()
        emulator_bin, env = layout.emulator_bin, layout.env()
    if skip_acceleration:
        return HostSupport("ready", "", **host)
    if not emulator_bin.is_file():
        return HostSupport("sdk_missing", "Install the Android SDK from the Phone panel first.", **host)
    try:
        stat = emulator_bin.stat()
        key, revision = str(emulator_bin.resolve()), (stat.st_mtime_ns, stat.st_size)
        with _accel_lock:
            if _accel_cache.get(key) == revision:
                return HostSupport("ready", "", **host)
            # DEVNULL avoids a grandchild inheriting captured pipes and keeping
            # subprocess.run's post-timeout communicate() blocked indefinitely.
            result = subprocess.run(
                [str(emulator_bin), "-accel-check"], stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, env=env,
            )
            if result.returncode != 0:
                _accel_cache.pop(key, None)
                return HostSupport("error", WINDOWS_ACCEL_HELP, **host)
            _accel_cache[key] = revision
    except (OSError, subprocess.SubprocessError) as exc:
        return HostSupport("error", f"{WINDOWS_ACCEL_HELP} Acceleration check failed: {type(exc).__name__}.", **host)
    return HostSupport("ready", "", **host)


def supports_platform() -> bool:
    """Backend eligibility only; tool registration must not run an SDK process."""
    return platform.system() in ("Linux", "Windows") and platform.machine().lower() in ("x86_64", "amd64")


def check_host(kvm_path: str = KVM_PATH, *, emulator_bin: Optional[Path] = None,
               env: Optional[dict] = None, skip_acceleration: bool = False) -> HostSupport:
    """Worker support; Windows runs a bounded acceleration probe (call off-loop)."""
    system = platform.system()
    machine = platform.machine().lower()
    if system == "Linux" and machine in ("x86_64", "amd64"):
        return _linux_x86_64(kvm_path)
    if system == "Windows" and machine in ("x86_64", "amd64"):
        return _windows_x86_64(emulator_bin, env, skip_acceleration)
    return HostSupport(
        "unsupported_arch",
        f"The Android emulator needs Linux x86_64 with KVM or Windows x86_64 with WHPX/AEHD; "
        f"this worker is {system or 'unknown'} {machine or 'unknown'}.",
    )
