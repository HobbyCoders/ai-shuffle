"""Optional Codex voice runtime: no native import or installation at app startup.

Invoked by installers or an explicit, authorized voice-call start, never by
passive settings reads. Failure of this separately hash-pinned feature must
never fail an app install/update.
"""
import asyncio
import importlib
import os
from pathlib import Path
import platform
import subprocess
import sys
import threading
import time

_INSTALL_LOCK = threading.Lock()
UNAVAILABLE = "Unofficial ChatGPT voice is not available on this system."


def platform_reason():
    if platform.system() == "Darwin":
        try:
            if int(platform.mac_ver()[0].split(".")[0]) < 14:
                return UNAVAILABLE + " Requires macOS 14 or later."
        except (ValueError, IndexError):
            return UNAVAILABLE + " Could not determine the macOS version."
    if platform.system() == "Linux":
        libc, version = platform.libc_ver()
        try:
            if libc != "glibc" or tuple(map(int, version.split(".")[:2])) < (2, 28):
                return UNAVAILABLE + " Requires glibc 2.28 or later."
        except ValueError:
            return UNAVAILABLE + " Could not determine the glibc version."
    return None


def load_native():
    reason = platform_reason()
    if reason:
        raise ValueError(reason)
    try:
        return importlib.import_module("aiortc"), importlib.import_module("av")
    except (ImportError, OSError):
        # Native loader messages can contain private filesystem paths.
        raise ValueError(UNAVAILABLE + " Optional audio libraries could not be loaded.") from None


def availability():
    try:
        load_native()
    except ValueError as error:
        return False, str(error)
    return True, None


async def ensure_available():
    """Retry missing optional libraries only on an authorized call start, not GETs.

    Serialize attempts without queueing more installers behind a slow download.
    A failed attempt is not cached: the next explicit call can try again.
    """
    available, reason = availability()
    if available or platform_reason():
        return available, reason

    def retry():
        if not _INSTALL_LOCK.acquire(blocking=False):
            return availability()
        try:
            if not availability()[0]:
                install(Path(__file__).resolve().parents[2])
                importlib.invalidate_caches()
            return availability()
        finally:
            _INSTALL_LOCK.release()

    return await asyncio.to_thread(retry)


def install(root):
    root = Path(root)
    reason = platform_reason()
    lock = root / "requirements-voice.lock"
    if reason or not lock.is_file():
        print(reason or UNAVAILABLE + " Optional dependency lock is missing.")
        return False
    try:
        try:  # Standalone installer runs may not have the harness on the path.
            from fluid.tools.child_env import scrub_child_env
            env = scrub_child_env()
        except ImportError:
            # Before Fluid is installed, this is the standalone build/installer
            # environment, not an agent tool's environment with injected secrets.
            env = dict(os.environ)
        deadline = time.monotonic() + 180
        result = subprocess.run([
            sys.executable, "-m", "pip", "install", "--only-binary=:all:",
            "--require-hashes", "--no-deps", "--timeout", "30", "--retries", "0",
            "-r", str(lock),
        ], cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180, env=env)
        # Verify in the target interpreter, not this process's import cache.
        # The pip install and native-import check share one 180-second budget.
        remaining = deadline - time.monotonic()
        installed = False
        if result.returncode == 0 and remaining > 0:
            probe = subprocess.run(
                [sys.executable, "-c", "import aiortc, av"], cwd=root,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=remaining, env=env,
            )
            installed = probe.returncode == 0
    except (OSError, subprocess.SubprocessError):
        installed = False
    if not installed:
        print(UNAVAILABLE + " Optional audio libraries were not installed; the app can still run.")
    return installed


if __name__ == "__main__":
    if sys.argv[1:] == ["--install"]:
        raise SystemExit(0 if install(Path(__file__).resolve().parents[2]) else 1)
    else:
        raise SystemExit("Use --install to install optional voice libraries.")
