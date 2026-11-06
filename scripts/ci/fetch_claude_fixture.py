"""Fetch a checksum-verified stock CLI for offline delegated contract tests.

This never installs or updates a user's CLI. The destination is a CI/scratch
file, executed only with isolated configuration and the scripted local API.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.ci import publication_read_retry as read_retry

BASE = "https://downloads.claude.ai/claude-code-releases"


def _temporary_download_error(error: Exception) -> bool:
    # urlopen wraps connect/TLS errors, while response.read can raise directly.
    if isinstance(error, urllib.error.HTTPError):
        return 500 <= error.code < 600
    reason = error.reason if isinstance(error, urllib.error.URLError) else error
    return isinstance(reason, (ConnectionResetError, TimeoutError))


def fetch(version: str, destination: Path) -> None:
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise ValueError("a release version is required")

    def manifest():
        with urllib.request.urlopen(f"{BASE}/{version}/manifest.json", timeout=30) as response:
            return json.load(response)["platforms"]["linux-x64"]

    platform = read_retry.retry_read(manifest, _temporary_download_error)
    destination.parent.mkdir(parents=True, exist_ok=True)

    def binary():
        digest = hashlib.sha256()
        size = 0
        # Exclusive create is outside cleanup: never remove a pre-existing file.
        output = destination.open("xb")
        try:
            with output:
                with urllib.request.urlopen(f"{BASE}/{version}/linux-x64/claude", timeout=90) as response:
                    while chunk := response.read(1024 * 1024):
                        digest.update(chunk)
                        size += len(chunk)
                        output.write(chunk)
            if digest.hexdigest() != platform["checksum"] or size != platform["size"]:
                raise ValueError("CLI fixture checksum or size mismatch")
        except BaseException:
            # Close before unlinking (also on Windows). Every retry starts empty,
            # with its own digest; exhausted/permanent failures leave no artifact.
            destination.unlink()
            raise

    read_retry.retry_read(binary, _temporary_download_error)
    destination.chmod(0o700)
    print(f"Claude Code {version}: checksum and size verified")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    fetch(args.version, args.output)
