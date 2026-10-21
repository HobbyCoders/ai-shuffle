"""Optional Codex voice runtime: no native import or installation at app startup.

Invoked explicitly by installers as a standalone script. Failure to install
this separately hash-pinned feature must never fail an app install/update.
"""
import importlib
from pathlib import Path
import platform
import subprocess
import sys

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


def install(root):
    root = Path(root)
    reason = platform_reason()
    lock = root / "requirements-voice.lock"
    if reason or not lock.is_file():
        print(reason or UNAVAILABLE + " Optional dependency lock is missing.")
        return False
    try:
        result = subprocess.run([
            sys.executable, "-m", "pip", "install", "--only-binary=:all:",
            "--require-hashes", "--no-deps", "--timeout", "30", "--retries", "0",
            "-r", str(lock),
        ], cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180)
        installed = result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        installed = False
    if not installed:
        print(UNAVAILABLE + " Optional audio libraries were not installed; the app can still run.")
    return installed


if __name__ == "__main__":
    if sys.argv[1:] == ["--install"]:
        install(Path(__file__).resolve().parents[2])
    else:
        raise SystemExit("Use --install to install optional voice libraries.")
