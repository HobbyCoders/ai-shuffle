"""The owner-started Android SDK install: emulator, platform-tools, one system image.

Runs on a worker thread end to end (manifest fetch, download, SHA-1 check,
unzip), so the event loop only ever reads the progress snapshot. Packages
come from Google's repository manifests (``sdk_repo``); each archive is
checked against the manifest's size and SHA-1 before anything is unpacked,
unpacked into a staging folder, and moved into place only when complete. A
package already installed at the same revision is skipped, so a failed or
cancelled install resumes with what is missing.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import stat
import threading
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from app.core.android_emulator import sdk_repo
from app.core.android_emulator.host import HostSupport
from app.core.android_emulator.layout import SdkLayout

logger = logging.getLogger(__name__)

CHUNK = 1024 * 1024
#: Unpacked size per archive byte. Measured: emulator 37.2 2.6x, platform-tools
#: 2.5x, the android-35 google_apis x86_64 image 2.2x (which dominates).
UNPACK_FACTOR = 2.3
#: Room for the device's own disk (a booted AVD holds ~2 GB) and logs. With the
#: default packages the check asks for about 8.6 GB free.
DEVICE_RESERVE_BYTES = 2 * 1024 ** 3
#: An archive claiming to unpack beyond this is refused (zip bomb guard).
MAX_UNPACKED_BYTES = 24 * 1024 ** 3
#: Download share of a package's progress; unpacking is the rest.
DOWNLOAD_WEIGHT = 0.8


class InstallError(Exception):
    """An install step failed; the message is shown to the owner as is."""


class ChecksumMismatch(InstallError):
    pass


class Cancelled(InstallError):
    pass


@dataclass
class InstallProgress:
    """Thread-safe progress, read by status calls while the worker thread writes."""

    state: str = "idle"  # idle | downloading | extracting | done | error
    progress: float = 0.0
    message: str = ""
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def set(self, state: Optional[str] = None, progress: Optional[float] = None,
            message: Optional[str] = None) -> None:
        with self._lock:
            if state is not None:
                self.state = state
            if progress is not None:
                self.progress = max(0.0, min(1.0, progress))
            if message is not None:
                self.message = message

    def snapshot(self) -> Dict[str, object]:
        with self._lock:
            return {"state": self.state, "progress": round(self.progress, 4), "message": self.message}


def license_hash(text: str) -> str:
    """sdkmanager's license hash: SHA-1 of the trimmed license text."""
    return hashlib.sha1(text.strip().encode("utf-8")).hexdigest()


def _free_bytes(path: Path) -> int:
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return shutil.disk_usage(probe).free


def _within(root: Path, candidate: Path) -> bool:
    root_s = os.path.normpath(str(root))
    cand_s = os.path.normpath(str(candidate))
    return cand_s == root_s or cand_s.startswith(root_s + os.sep)


def extract_zip(archive: Path, dest: Path, *, cancel: Optional[threading.Event] = None,
                on_bytes: Optional[Callable[[int, int], None]] = None) -> None:
    """Unpack ``archive`` into ``dest``, dropping its single top-level folder.

    Keeps the executable bits and symlinks the SDK archives rely on (Python's
    ``extractall`` drops both), and refuses any entry or link that would land
    outside ``dest``.
    """
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as zf:
        members = zf.infolist()
        total = sum(m.file_size for m in members)
        if total > MAX_UNPACKED_BYTES:
            raise InstallError(f"{archive.name} would unpack to {total / 1024 ** 3:.1f} GB; refusing it")
        tops = {m.filename.replace("\\", "/").split("/", 1)[0] for m in members}
        strip = len(tops) == 1 and all("/" in m.filename.replace("\\", "/") for m in members)
        done = 0
        for member in members:
            if cancel is not None and cancel.is_set():
                raise Cancelled("Install cancelled")
            name = member.filename.replace("\\", "/")
            if strip:
                name = name.split("/", 1)[1]
            if not name.strip("/"):
                continue
            parts = [p for p in name.split("/") if p]
            if name.startswith("/") or any(p == ".." for p in parts) or ":" in parts[0]:
                raise InstallError(f"{archive.name} has an unsafe entry: {member.filename}")
            target = dest.joinpath(*parts)
            if not _within(dest, target):
                raise InstallError(f"{archive.name} has an unsafe entry: {member.filename}")
            mode = (member.external_attr >> 16) & 0xFFFF
            if member.is_dir() or name.endswith("/"):
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if stat.S_ISLNK(mode):
                link = zf.read(member).decode("utf-8")
                if os.path.isabs(link) or not _within(dest, target.parent / link):
                    raise InstallError(f"{archive.name} has a link out of its folder: {member.filename}")
                if target.is_symlink() or target.exists():
                    target.unlink()
                os.symlink(link, target)
                continue
            with zf.open(member) as src, open(target, "wb") as out:
                while True:
                    block = src.read(CHUNK)
                    if not block:
                        break
                    out.write(block)
                    done += len(block)
                    if on_bytes is not None:
                        on_bytes(done, total)
            perms = stat.S_IMODE(mode)
            os.chmod(target, (perms | 0o600) if perms else 0o644)


@dataclass(frozen=True)
class Planned:
    package: sdk_repo.Package
    archive: sdk_repo.Archive


class SdkInstaller:
    """One install run. ``run`` is blocking; call it from a worker thread."""

    def __init__(self, layout: SdkLayout, host: HostSupport, system_image: str,
                 progress: InstallProgress, cancel: threading.Event,
                 fetch: Optional[Callable[[str], bytes]] = None,
                 download: Optional[Callable[[str, Path, Callable[[bytes], None]], None]] = None) -> None:
        self.layout = layout
        self.host = host
        self.system_image = system_image
        self.progress = progress
        self.cancel = cancel
        self._fetch = fetch or self._http_fetch
        self._download = download or self._http_download

    # ── HTTP (replaced in tests) ───────────────────────────────────────────
    @staticmethod
    def _client():
        import httpx

        return httpx.Client(timeout=httpx.Timeout(60.0, connect=30.0), follow_redirects=True)

    def _http_fetch(self, url: str) -> bytes:
        with self._client() as client:
            with client.stream("GET", url) as response:
                response.raise_for_status()
                data = bytearray()
                for block in response.iter_bytes(CHUNK):
                    data.extend(block)
                    if len(data) > sdk_repo.MAX_MANIFEST_BYTES:
                        raise InstallError(f"{url} is too large to be a manifest")
                return bytes(data)

    def _http_download(self, url: str, dest: Path, on_block: Callable[[bytes], None]) -> None:
        with self._client() as client:
            with client.stream("GET", url) as response:
                response.raise_for_status()
                with open(dest, "wb") as out:
                    for block in response.iter_bytes(CHUNK):
                        if self.cancel.is_set():
                            raise Cancelled("Install cancelled")
                        out.write(block)
                        on_block(block)

    # ── Plan ───────────────────────────────────────────────────────────────
    def plan(self) -> List[Planned]:
        """The newest stable packages this host needs, with their archives."""
        host_os, host_arch = self.host.host_os, self.host.host_arch
        if not host_os:
            raise InstallError(self.host.message or "This worker cannot run the Android emulator.")
        try:
            repo = sdk_repo.parse_manifest(self._fetch(sdk_repo.REPOSITORY_MANIFEST), sdk_repo.REPOSITORY_BASE)
            image_manifest = sdk_repo.system_image_manifest(self.system_image)
            images = sdk_repo.parse_manifest(self._fetch(image_manifest), image_manifest.rsplit("/", 1)[0] + "/")
            planned = [
                Planned(*sdk_repo.resolve(repo, "emulator", host_os, host_arch)),
                Planned(*sdk_repo.resolve(repo, "platform-tools", host_os, host_arch)),
                Planned(*sdk_repo.resolve(images, self.system_image, host_os, host_arch)),
            ]
        except sdk_repo.ManifestError as exc:
            raise InstallError(f"Could not read Google's Android SDK repository: {exc}") from None
        for item in planned:
            if item.package.license_id != sdk_repo.SDK_LICENSE_ID:
                raise InstallError(
                    f"{item.package.path} needs the license '{item.package.license_id}', which "
                    "AI Shuffle does not accept on your behalf. Pick another system image.")
        return planned

    def _needed(self, planned: List[Planned]) -> List[Planned]:
        record = self.layout.read_record().get("packages") or {}
        needed = []
        for item in planned:
            have = record.get(item.package.path) or {}
            if have.get("revision") == item.package.revision_text and have.get("sha1") == item.archive.sha1 \
                    and self.layout.package_dir(item.package.path).is_dir():
                continue
            needed.append(item)
        return needed

    def required_bytes(self, needed: List[Planned]) -> int:
        if not needed:
            return 0
        unpacked = sum(int(item.archive.size * UNPACK_FACTOR) for item in needed)
        # The largest archive sits next to its unpacked copy before it is deleted.
        return unpacked + max(item.archive.size for item in needed) + DEVICE_RESERVE_BYTES

    # ── Run ────────────────────────────────────────────────────────────────
    def run(self) -> None:
        self.progress.set("downloading", 0.0, "Reading Google's Android SDK repository")
        planned = self.plan()
        needed = self._needed(planned)
        required = self.required_bytes(needed)
        free = _free_bytes(self.layout.root)
        if required and free < required:
            raise InstallError(
                f"Not enough free disk space for the Android emulator: it needs about "
                f"{required / 1024 ** 3:.1f} GB in {self.layout.root}, and "
                f"{free / 1024 ** 3:.1f} GB is free.")
        total = sum(item.archive.size for item in needed) or 1
        base = 0
        for item in needed:
            self._install(item, base, total)
            base += item.archive.size
        self._accept_license(planned)
        self.progress.set("done", 1.0, "Android emulator installed")

    def _install(self, item: Planned, base: int, total: int) -> None:
        package, archive = item.package, item.archive
        label = f"{package.path} {package.revision_text}"
        self.layout.downloads.mkdir(parents=True, exist_ok=True)
        part = self.layout.downloads / (archive.url.rsplit("/", 1)[-1] + ".part")
        digest = hashlib.sha1()
        received = 0
        started = time.monotonic()

        def on_block(block: bytes) -> None:
            nonlocal received
            digest.update(block)
            received += len(block)
            if received > archive.size:
                raise ChecksumMismatch(f"{label}: the download is larger than the manifest says")
            share = (base + received * DOWNLOAD_WEIGHT) / total
            self.progress.set(progress=share)

        self.progress.set("downloading", base / total,
                          f"Downloading {label} ({archive.size / 1024 ** 2:.0f} MB)")
        try:
            self._download(archive.url, part, on_block)
            if received != archive.size:
                raise ChecksumMismatch(
                    f"{label}: downloaded {received} bytes, the manifest says {archive.size}")
            if digest.hexdigest() != archive.sha1:
                raise ChecksumMismatch(
                    f"{label}: the archive's SHA-1 does not match Google's manifest; it was discarded")
            logger.info("Android SDK: downloaded %s in %.0fs", label, time.monotonic() - started)
            self.progress.set("extracting", (base + archive.size * DOWNLOAD_WEIGHT) / total, f"Unpacking {label}")
            staging = self.layout.staging / package.path.replace(";", "_")
            shutil.rmtree(staging, ignore_errors=True)

            def on_bytes(done: int, size: int) -> None:
                share = archive.size * (DOWNLOAD_WEIGHT + (1 - DOWNLOAD_WEIGHT) * done / max(size, 1))
                self.progress.set(progress=(base + share) / total)

            extract_zip(part, staging, cancel=self.cancel, on_bytes=on_bytes)
            target = self.layout.package_dir(package.path)
            if target.exists() or target.is_symlink():
                old = target.with_name(target.name + ".old")
                shutil.rmtree(old, ignore_errors=True)
                os.replace(target, old)
                shutil.rmtree(old, ignore_errors=True)
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staging, target)
        finally:
            try:
                part.unlink()
            except FileNotFoundError:
                pass
        record = self.layout.read_record()
        record.setdefault("packages", {})[package.path] = {
            "revision": package.revision_text, "sha1": archive.sha1,
            "archive": archive.url.rsplit("/", 1)[-1], "installed_at": time.time(),
        }
        self.layout.write_record(record)

    def _accept_license(self, planned: List[Planned]) -> None:
        """Record the owner's acceptance the way sdkmanager does, so builds see it too."""
        text = next((item.package.license_text for item in planned if item.package.license_text), "")
        if not text:
            return
        digest = license_hash(text)
        self.layout.licenses.mkdir(parents=True, exist_ok=True)
        path = self.layout.licenses / sdk_repo.SDK_LICENSE_ID
        try:
            existing = path.read_text(encoding="utf-8").split()
        except FileNotFoundError:
            existing = []
        if digest not in existing:
            path.write_text("\n".join([*existing, digest]) + "\n", encoding="utf-8")
        record = self.layout.read_record()
        record["license"] = {"id": sdk_repo.SDK_LICENSE_ID, "sha1": digest, "accepted_at": time.time()}
        self.layout.write_record(record)


def run_install(layout: SdkLayout, host: HostSupport, system_image: str,
                progress: InstallProgress, cancel: threading.Event, **seams) -> Tuple[bool, str]:
    """Blocking install with every failure folded into ``progress``. Returns (ok, message)."""
    try:
        SdkInstaller(layout, host, system_image, progress, cancel, **seams).run()
        return True, "Android emulator installed"
    except Cancelled as exc:
        progress.set("error", None, str(exc))
        return False, str(exc)
    except InstallError as exc:
        logger.warning("Android SDK install failed: %s", exc)
        progress.set("error", None, str(exc))
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001 — network, disk: report, never crash the app
        logger.exception("Android SDK install failed")
        message = f"The Android SDK install failed: {type(exc).__name__}: {exc}"[:500]
        progress.set("error", None, message)
        return False, message
    finally:
        shutil.rmtree(layout.staging, ignore_errors=True)
