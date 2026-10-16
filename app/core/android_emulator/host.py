"""Can this worker run the Android emulator? Detection with reason codes.

Linux x86_64 with KVM is the only backend today. The host check is the seam
where Windows (WHPX/AEHD) and macOS (HVF) backends plug in later: each returns
the manifest's ``host-os``/``host-arch`` pair the SDK archives are chosen by,
or a reason the panel and the agent can show as it is.
"""

from __future__ import annotations

import errno
import os
import platform
from dataclasses import dataclass
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
    """``reason`` is ``ready``, ``no_kvm``, ``kvm_permission`` or ``unsupported_arch``."""

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


def check_host(kvm_path: str = KVM_PATH) -> HostSupport:
    """This worker's emulator support. Cheap (one open of /dev/kvm); never raises."""
    system = platform.system()
    machine = platform.machine().lower()
    if system == "Linux" and machine in ("x86_64", "amd64"):
        return _linux_x86_64(kvm_path)
    # Windows (WHPX/AEHD) and macOS (HVF) backends will answer here.
    return HostSupport(
        "unsupported_arch",
        f"The Android emulator runs on Linux x86_64 workers with KVM for now; "
        f"this worker is {system or 'unknown'} {machine or 'unknown'}.",
    )
