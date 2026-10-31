"""Fetch a checksum-verified stock CLI for offline delegated contract tests.

This never installs or updates a user's CLI. The destination is a CI/scratch
file, executed only with isolated configuration and the scripted local API.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import urllib.request
from pathlib import Path

BASE = "https://downloads.claude.ai/claude-code-releases"


def fetch(version: str, destination: Path) -> None:
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise ValueError("a release version is required")
    with urllib.request.urlopen(f"{BASE}/{version}/manifest.json", timeout=30) as response:
        platform = json.load(response)["platforms"]["linux-x64"]
    digest = hashlib.sha256()
    size = 0
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Refuse to overwrite an installed CLI or any previous artifact.
    with destination.open("xb") as output:
        with urllib.request.urlopen(f"{BASE}/{version}/linux-x64/claude", timeout=90) as response:
            while chunk := response.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
                output.write(chunk)
    if digest.hexdigest() != platform["checksum"] or size != platform["size"]:
        destination.unlink()
        raise ValueError("CLI fixture checksum or size mismatch")
    destination.chmod(0o700)
    print(f"Claude Code {version}: checksum and size verified")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    fetch(args.version, args.output)
