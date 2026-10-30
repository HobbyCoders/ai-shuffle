"""Build the runtime source archive served as an in-app update, and its tree digest.

The Docker and desktop updaters rebuild the application from this archive, so
it carries only what they build and run: the backend, fluid, the Node
packages (including mobile/, the phone UI the frontend build emits at
/mobile/), the desktop launcher, the container supervisor (docker/), the in-app
docs and the dependency lists.
Internal docs, reviews, mockups, videos, tests, the Android app, CI and owner
tooling (scripts/) never leave the repository
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
    # The container supervisor, updater and Chromium scripts. Images that hand
    # over (docker/update_contract.py "source-supervisor") run them from the
    # verified update instead of their own copies (#1821).
    "docker",
    "docs/content",
    "requirements.txt",
    "requirements.lock",
    "requirements-build.txt",
    "requirements-native.txt",
    "requirements-native.lock",
    "requirements-voice.txt",
    "requirements-voice.lock",
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


UI_MAX_SIZE = 64 * 1024**2
UI_MAX_EXPANDED = 256 * 1024**2


def describe_ui_bundle(archive: Path, revision: str, version: str) -> dict:
    """Trusted publisher reads an artifact as bounded data, never candidate code."""
    if not re.fullmatch(r"[a-f0-9]{40}", revision) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[.+-][A-Za-z0-9.+-]{1,60})?", version or ""):
        raise ValueError("invalid UI build revision/version")
    if not 0 < archive.stat().st_size <= UI_MAX_SIZE:
        raise ValueError("UI archive exceeds size limit")
    prefix = root_name(revision) + "/"
    with zipfile.ZipFile(archive) as zipped:
        entries = zipped.infolist()
        if len(entries) > 20000 or sum(item.file_size for item in entries) > UI_MAX_EXPANDED:
            raise ValueError("UI archive expansion exceeds limits")
        seen, files = set(), []
        for item in entries:
            name = item.filename
            relative = name[len(prefix):] if name.startswith(prefix) else ""
            parts = relative.rstrip("/").split("/")
            mode = (item.external_attr >> 16) & 0xf000
            if (not relative or any(not part or part in (".", "..") or part.endswith((" ", "."))
                                    or re.search(r'[\\:<>"|?*\x00-\x1f]', part)
                                    or re.match(r"(?i)^(con|prn|aux|nul|com[0-9]|lpt[0-9])(?:\.|$)", part) for part in parts)
                    or mode not in (0, 0x8000, 0x4000) or item.flag_bits & 1 or name.casefold() in seen):
                raise ValueError("unsafe UI archive entry")
            seen.add(name.casefold())
            if not item.is_dir():
                files.append((relative, hashlib.sha256(zipped.read(item)).hexdigest()))
        expected = {"format": 1, "revision": revision, "version": version}
        if json.loads(zipped.read(prefix + "ui-build.json")) != expected:
            raise ValueError("UI bundle build constants do not match the source release")
        if not {"index.html", "mobile/index.html"}.issubset({name for name, _ in files}):
            raise ValueError("UI bundle must contain both desktop and phone shells")
    return {**expected, "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
            "size": archive.stat().st_size, "tree": tree_digest(files)}


def build_ui_bundle(directory: Path, revision: str, version: str, output: Path) -> dict:
    """Reproducible asset ZIP. The stamp binds all build-time version constants."""
    if output.exists() or output.resolve().is_relative_to(directory.resolve()):
        raise ValueError("UI output must be a new file outside the build directory")
    paths = sorted(directory.rglob("*"))
    if any(path.is_symlink() for path in paths):
        raise ValueError("UI build cannot contain symlinks")
    stamp = json.dumps({"format": 1, "revision": revision, "version": version}, sort_keys=True).encode()
    with output.open("xb") as stream, zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as zipped:
        files = [(path.relative_to(directory).as_posix(), path.read_bytes()) for path in paths
                 if path.is_file() and path.relative_to(directory).as_posix() != "ui-build.json"]
        for relative, data in sorted([*files, ("ui-build.json", stamp)]):
            info = zipfile.ZipInfo(root_name(revision) + "/" + relative, (1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            zipped.writestr(info, data)
    return describe_ui_bundle(output, revision, version)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("revision")
    parser.add_argument("output", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--ui-build", type=Path)
    mode.add_argument("--inspect-ui", action="store_true")
    parser.add_argument("--version")
    args = parser.parse_args()
    try:
        result = (build_ui_bundle(args.ui_build, args.revision, args.version, args.output) if args.ui_build else
                  describe_ui_bundle(args.output, args.revision, args.version) if args.inspect_ui else
                  build(args.revision, args.output))
        print(json.dumps(result))
    except (ValueError, subprocess.CalledProcessError) as exc:
        print(f"source_archive: {exc}", file=sys.stderr)
        sys.exit(1)
