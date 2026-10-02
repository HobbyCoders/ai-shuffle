"""Build the runtime source archive served as an in-app update, and its tree digest.

The Docker and desktop updaters rebuild the application from this archive, so
it carries only what they build and run: the backend, fluid, the Node
packages (including mobile/, the phone UI the frontend build emits at
/mobile/), the desktop launcher, the in-app docs and the dependency lists.
Internal docs, reviews, mockups, videos, tests, the Android app, CI and owner
tooling (scripts/, including scripts/unraid-lab) never leave the repository
through the update feed.

The archive comes from `git archive`, so it is byte-for-byte reproducible for a
revision and a retried publish reuses the same asset. The tree digest is the
SHA-256 of one "path NUL sha256 LF" line per regular file, sorted by path. It
is signed into the manifest, and docker/application_updates.py recomputes it
over the extracted tree before any build and again before every launch.

Usage: python scripts/source_archive.py <revision> <output.zip>
Prints {"sha256", "size", "tree", "files"} as JSON. Stdlib only: CI runs it
before any dependency is installed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]

# Allow-list, not deny-list: a new top-level folder stays private until it is
# deliberately added here.
RUNTIME_PATHS = (
    "app",
    "fluid/fluid",
    "fluid/pyproject.toml",
    "fluid/README.md",
    "frontend",
    # The phone UI: the frontend build compiles it into build/mobile, which the
    # worker serves at /mobile/ to the Android and iOS apps.
    "mobile",
    "tools",
    "browser-mcp",
    "desktop",
    "docs/content",
    "requirements.txt",
    "requirements.lock",
    "requirements-build.txt",
    "requirements-native.txt",
    "requirements-native.lock",
    ".env.example",
    "LICENSE",
    "THIRD_PARTY_NOTICES.md",
)

# Inside the allowed folders: tests, developer probes and local harnesses.
EXCLUDED = (
    "frontend/tests",
    "frontend/notification-sound-harness.html",
    "frontend/src/lib/test-stubs",
    "mobile/tests",
    "mobile/src/shell/test-fixtures",
    "**/*.test.ts",
    "**/*.test.js",
    "**/*.spec.ts",
    "**/*.test.svelte",
    "desktop/test",
    "browser-mcp/test",
    "tools/stub_upstream",
    "tools/benchmark_sdk_latency.py",
    "tools/research",
    "tools/backup-db.sh",
    "tools/restore-db.sh",
    "tools/ws_*_probe.py",
)


def pathspec(paths=RUNTIME_PATHS) -> list[str]:
    return [*paths, *(f":(exclude,glob){item}" for item in EXCLUDED)]


def present_paths(repo: Path, revision: str) -> list[str]:
    """git archive rejects a pathspec that matches nothing; skip absent optional files."""
    return [path for path in RUNTIME_PATHS
            if subprocess.run(["git", "cat-file", "-e", f"{revision}:{path}"], cwd=repo,
                              capture_output=True).returncode == 0]


def root_name(revision: str) -> str:
    # docker/application_updates.py and the desktop updater accept this root.
    return f"ai-shuffle-{revision}"


def tree_lines(files: list[tuple[str, str]]) -> bytes:
    return b"".join(f"{path}\0{digest}\n".encode() for path, digest in sorted(files))


def tree_digest(files: list[tuple[str, str]]) -> str:
    return hashlib.sha256(tree_lines(files)).hexdigest()


def archive_files(archive: Path) -> list[tuple[str, str]]:
    """(path below the archive root, sha256) for every regular file."""
    files = []
    with zipfile.ZipFile(archive) as zipped:
        for item in zipped.infolist():
            if item.is_dir():
                continue
            relative = item.filename.split("/", 1)[1]
            with zipped.open(item) as stream:
                files.append((relative, hashlib.file_digest(stream, "sha256").hexdigest()))
    return files


def build(revision: str, output: Path, repo: Path = ROOT) -> dict:
    if not re.fullmatch(r"[a-f0-9]{40}", revision):
        raise ValueError("revision must be a full commit SHA")
    output = Path(output)
    if output.exists():
        raise ValueError(f"{output} already exists")
    subprocess.run(["git", "archive", "--format=zip", f"--prefix={root_name(revision)}/",
                    "-o", str(output), revision, "--", *pathspec(present_paths(repo, revision))], cwd=repo, check=True)
    files = archive_files(output)
    present = {path.split("/", 1)[0] for path, _ in files}
    # The desktop updater installs both locks with --require-hashes.
    missing = {"app", "fluid", "frontend", "requirements.lock", "requirements-native.lock"} - present
    if missing:
        raise ValueError(f"runtime archive is missing {', '.join(sorted(missing))}")
    data = output.read_bytes()
    return {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
            "tree": tree_digest(files), "files": len(files)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("revision")
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(build(args.revision, args.output)))
    except (ValueError, subprocess.CalledProcessError) as exc:
        print(f"source_archive: {exc}", file=sys.stderr)
        sys.exit(1)
