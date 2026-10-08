"""This worker's device identity: two private keys under local custody.

The identity file lives beside the credential key under
``<data_dir>/.credential-keys/device-identity.key`` and uses the same custody
primitives (owner-only file, DPAPI on Windows, exclusive publication, fsync).
It is created once at first start and never synced. Its loss means this
worker re-enrolls as a new device.

File format (JSON, DPAPI-wrapped on Windows):

    {"v": 1, "signing": <b64u seed>, "encryption": <b64u private>,
     "incarnation": <int>, "created_at": <iso>}

``incarnation`` bumps when a peer detects that this device's origin sequence
went backwards (a restore from backup); see ``engine``.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import stat
import tempfile
import threading
from dataclasses import dataclass
from functools import cached_property
from datetime import datetime, timezone
from typing import Optional

from app.core.config import settings
from app.core import managed_credentials as custody
from app.network import crypto
from app.network.membership import device_id_for

logger = logging.getLogger(__name__)

IDENTITY_FILE = "device-identity.key"
_lock = threading.Lock()
_cached: Optional["DeviceIdentity"] = None


class IdentityError(RuntimeError):
    """The identity file is missing, unreadable or corrupt."""


@dataclass(frozen=True)
class DeviceIdentity:
    signing_private: bytes
    encryption_private: bytes
    incarnation: int
    created_at: str

    # Cached: every published op reads the device id more than once, and
    # deriving the public key from the private one each time cost ~2.5 s per
    # 20,000 ops (#1978). The fields are frozen, so the result cannot go stale.
    @cached_property
    def signing_public(self) -> bytes:
        return crypto.signing_public_key(self.signing_private)

    @property
    def encryption_public(self) -> bytes:
        return crypto.encryption_public_key(self.encryption_private)

    @cached_property
    def device_id(self) -> str:
        return device_id_for(self.signing_public)

    @property
    def fingerprint(self) -> str:
        return crypto.fingerprint(self.signing_public)

    def public_summary(self) -> dict:
        return {
            "device_id": self.device_id,
            "fingerprint": self.fingerprint,
            "signing_public": crypto.b64u_encode(self.signing_public),
            "encryption_public": crypto.b64u_encode(self.encryption_public),
            "incarnation": self.incarnation,
        }


def _directory():
    directory = settings.effective_data_dir / ".credential-keys"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.lstat()
    if (not stat.S_ISDIR(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
        raise IdentityError("Identity directory must be local storage")
    if os.name != "nt":
        if info.st_uid != os.geteuid():
            raise IdentityError("Identity directory has another owner")
        if info.st_mode & 0o077:
            directory.chmod(0o700)
    else:
        custody.protect_private_path(directory, directory=True)
    return directory


def _path():
    return _directory() / IDENTITY_FILE


def _encode(identity: DeviceIdentity) -> bytes:
    payload = json.dumps({
        "v": 1,
        "signing": crypto.b64u_encode(identity.signing_private),
        "encryption": crypto.b64u_encode(identity.encryption_private),
        "incarnation": identity.incarnation,
        "created_at": identity.created_at,
    }).encode("utf-8")
    if os.name == "nt":
        return b"dpapi:" + base64.b64encode(custody._windows_protect(payload))
    return payload


def _decode(raw: bytes) -> DeviceIdentity:
    if os.name == "nt":
        if not raw.startswith(b"dpapi:"):
            raise IdentityError("Identity file is not protected")
        raw = custody._windows_protect(base64.b64decode(raw[6:], validate=True), decrypt=True)
    try:
        data = json.loads(raw.decode("utf-8"))
        if data.get("v") != 1:
            raise ValueError("version")
        signing = crypto.b64u_decode(data["signing"])
        encryption = crypto.b64u_decode(data["encryption"])
        incarnation = int(data.get("incarnation", 1))
        if len(signing) != 32 or len(encryption) != 32 or incarnation < 1:
            raise ValueError("shape")
        return DeviceIdentity(signing_private=signing, encryption_private=encryption,
                              incarnation=incarnation, created_at=str(data.get("created_at", "")))
    except (ValueError, KeyError, TypeError, crypto.CryptoError) as exc:
        raise IdentityError("Identity file is corrupt") from exc


def _read(path) -> DeviceIdentity:
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
        raise IdentityError("Identity file is not a regular file")
    if os.name == "nt":
        custody.protect_private_path(path)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as stream:
        fstat = os.fstat(stream.fileno())
        if os.name != "nt" and (fstat.st_uid != os.geteuid() or fstat.st_mode & 0o077):
            raise IdentityError("Identity file permissions are unsafe")
        raw = stream.read(8193)
    if len(raw) > 8192:
        raise IdentityError("Identity file is too large")
    return _decode(raw)


def _write_new(path, identity: DeviceIdentity, *, replace: bool = False) -> DeviceIdentity:
    raw = _encode(identity)
    fd, temporary = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            custody.protect_private_path(temporary)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(temporary, path)
        else:
            try:
                custody._publish_key(temporary, path)
            except FileExistsError:
                return _read(path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    custody._sync_key_location(path)
    return identity


def load(*, create: bool = True) -> DeviceIdentity:
    """Return this worker's identity, creating it on first use."""
    global _cached
    with _lock:
        if _cached is not None:
            return _cached
        path = _path()
        try:
            _cached = _read(path)
        except FileNotFoundError:
            if not create:
                raise IdentityError("Device identity is missing. Restore this worker's .credential-keys backup; refusing to create a replacement device.") from None
            fresh = DeviceIdentity(
                signing_private=crypto.generate_signing_key(),
                encryption_private=crypto.generate_encryption_key(),
                incarnation=1,
                created_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            )
            _cached = _write_new(path, fresh)
            logger.info("Created device identity %s (%s)", _cached.device_id, _cached.fingerprint)
        return _cached


def bump_incarnation() -> DeviceIdentity:
    """Start a new origin-sequence stream after a restore from backup."""
    global _cached
    with _lock:
        current = _cached or _read(_path())
        bumped = DeviceIdentity(signing_private=current.signing_private,
                                encryption_private=current.encryption_private,
                                incarnation=current.incarnation + 1, created_at=current.created_at)
        _cached = _write_new(_path(), bumped, replace=True)
        logger.warning("Device %s moved to incarnation %d", bumped.device_id, bumped.incarnation)
        return _cached


def reset_cache_for_tests() -> None:
    global _cached
    with _lock:
        _cached = None


def platform_name() -> str:
    from app.core.platform import detect_deployment_mode, DeploymentMode
    if os.environ.get("SHUFFLE_DESKTOP") == "1":
        return "desktop"
    if detect_deployment_mode() == DeploymentMode.DOCKER:
        return "docker"
    import sys
    return {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux")
