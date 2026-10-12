"""Durably attach exact-tag Linux packages: --tag vX.Y.Z --directory PATH.

Verifies produced provenance, package digests and SHA256SUMS, uploads without
clobber, then reads GitHub asset digests/sizes back. Uses the existing gh login.
"""
import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.ci.release_preflight import REPOSITORY, github, verify_tag, verify_merged_pr


def verified_files(directory, tag, revision):
    provenance = directory / "linux-build-provenance.json"
    proof = json.loads(provenance.read_text())
    if proof.get("sourceCommit") != revision or proof.get("version") != tag.removeprefix("v"):
        raise ValueError("Linux artifacts do not match the exact release tag")
    files = []
    sums = []
    for artifact in proof.get("artifacts", []):
        name = artifact["name"]
        if not re.fullmatch(r"[A-Za-z0-9_.-]+\.(?:AppImage|deb)", name):
            raise ValueError("Invalid Linux artifact name")
        path = directory / name
        if path.is_symlink():
            raise ValueError("Artifact symlinks are not allowed")
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if artifact.get("sha256") != digest or artifact.get("bytes") != len(data):
            raise ValueError("Linux artifact bytes differ from build provenance")
        files.append(path)
        sums.append(f"{digest}  {name}")
    if len(files) != 2 or {p.suffix for p in files} != {".AppImage", ".deb"}:
        raise ValueError("Exactly one AppImage and deb are required")
    if set((directory / "SHA256SUMS").read_text().splitlines()) != set(sums):
        raise ValueError("Linux checksum file does not match package bytes")
    # Do not collide with Windows' SHA256SUMS asset on the same release.
    checksum = directory / "SHA256SUMS-linux"
    checksum.write_text("\n".join(sums) + "\n")
    return [*files, checksum, provenance]


def attach(root, directory, tag):
    revision = verify_tag(root, tag)
    verify_merged_pr(revision)
    files = verified_files(directory, tag, revision)
    def assets():
        release = github(f"releases/tags/{tag}")
        # Never trust the embedded release assets array (it may be truncated).
        result = []
        for page in range(1, 101):
            batch = github(f"releases/{release['id']}/assets?per_page=100&page={page}")
            if not isinstance(batch, list): raise ValueError("Malformed asset inventory")
            result.extend(batch)
            if len(batch) < 100: return {row["name"]:row for row in result}
        raise ValueError("Incomplete release asset inventory")
    existing = assets()
    for path in files:
        data = path.read_bytes()
        expected = (len(data), "sha256:" + hashlib.sha256(data).hexdigest(), "uploaded")
        row = existing.get(path.name)
        if row is None:
            subprocess.run(["gh", "release", "upload", tag, str(path), "--repo", REPOSITORY], check=True)
            row = assets().get(path.name)
        if row is None or (row.get("size"), row.get("digest"), row.get("state")) != expected:
            raise ValueError(f"GitHub Linux asset read-back mismatch: {path.name}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    attach(Path.cwd(), args.directory, args.tag)
    print("PASS Linux packages/checksums retained and verified on GitHub release")


if __name__ == "__main__":main()
