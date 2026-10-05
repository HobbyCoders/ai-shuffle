"""Release check: which changes in a range cannot reach installed apps through the in-app update.

The in-app update replaces the signed runtime source (scripts/source_archive.py)
and rebuilds it on the worker. Some files are only ever read from the Docker
image or the installed Windows launcher, so a change to them needs a new image
or installer to take effect, and some changes stop the update for older
installs altogether. See docs/reviews/in-app-update-coverage-2026-10.md.

Groups that fail the check (each can be acknowledged with --allow GROUP):

  docker-image      The image contract fingerprint (docker/update_contract.py)
                    differs between base and head. Every Docker worker on the
                    base image shows "A new Docker image is required" and stops
                    taking in-app updates until its image is replaced.
  docker-image-copy A file the image copies and runs from /app or /usr/local/bin
                    that is neither fingerprinted nor in the update archive. The
                    change never reaches an existing container.
  windows-launcher  Code the installed AI Shuffle.exe runs from its app.asar on
                    every launch before it hands over to the updated source. The
                    change never reaches an existing install.
  update-trust-key  The application update public key. Images and installed
                    apps verify updates with the key they shipped; a new key
                    makes them refuse every later update.

Groups that are only reported: owner tooling copied into the image (scripts/),
Windows packaging (icons, NSIS options), Android native code (needs a Play
build, which the owner accepts), and the macOS and Linux desktop apps, which
have no in-place update and need a new installer for every change.

Read-only: runs git diff and git show, never fetches, tags or writes.

Usage::

    python scripts/ci/update_reach.py                      # newest final tag .. HEAD
    python scripts/ci/update_reach.py --base v0.9.0 --head origin/main
    python scripts/ci/update_reach.py --allow docker-image # owner accepted a new base
    python scripts/ci/update_reach.py --json

Exit status: 0 when nothing unacknowledged is out of reach, 1 otherwise.
"""
from __future__ import annotations

import argparse
import ast
import fnmatch
import hashlib
import importlib.util
import json
import posixpath
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable, Iterable, Optional

ROOT = Path(__file__).resolve().parents[2]
FINAL_TAG = re.compile(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")

TRUST_KEY = "desktop/build/application-update-key.pem"
CONTRACT = "docker/update_contract.py"
LAUNCHER_ENTRY = "desktop/src/main/managed-entry.ts"
# The installed launcher loads the full app (./index) only on a first run with
# no source update yet; after the first update, ./index comes from the source.
LAUNCHER_STOP = {"desktop/src/main/index.ts"}
WINDOWS_PACKAGING = ("desktop/electron-builder.yml", "desktop/build/*")
# Owner maintenance tools the image carries (python -m scripts.credential_keys).
IMAGE_TOOLING = ("scripts/*",)
ANDROID_NATIVE = ("android/*",)
# Cloudflare Workers deployed by their own workflows, not part of the Play app.
ANDROID_SERVER = ("android/hub/*", "android/site/*")

FAILING = ("docker-image", "docker-image-copy", "windows-launcher", "update-trust-key")
GROUPS = FAILING + ("image-tooling", "windows-packaging", "android-native", "macos-linux-desktop")

Reader = Callable[[str], Optional[bytes]]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git(*args: str, root: Path = ROOT) -> str:
    return subprocess.check_output(["git", *args], cwd=root, text=True).strip()


def reader(revision: str, root: Path = ROOT) -> Reader:
    """Bytes of a path at a revision, or None when it does not exist there."""
    def read(path: str) -> Optional[bytes]:
        result = subprocess.run(["git", "show", f"{revision}:{path}"], cwd=root, capture_output=True)
        return result.stdout if result.returncode == 0 else None
    return read


def matches(path: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def contract_files(read: Reader) -> Optional[tuple]:
    """The FILES tuple of docker/update_contract.py at a revision, without running it."""
    text = read(CONTRACT)
    if text is None:
        return None
    for node in ast.parse(text.decode()).body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "FILES" for t in node.targets):
            return tuple(ast.literal_eval(node.value))
    return None


def contract_fingerprint(read: Reader) -> Optional[str]:
    """The same digest docker/update_contract.py computes, over committed bytes.

    The image bakes it into /app/docker/image.json; publish-source.mjs signs it
    into every manifest. A mismatch makes the container updater refuse the
    update ("A new Docker image is required").
    """
    files = contract_files(read)
    if files is None:
        return None
    digest = hashlib.sha256()
    for name in files:
        data = read(name)
        if data is None:
            return None
        digest.update(name.encode() + b"\0" + data + b"\0")
    return digest.hexdigest()


def dockerfile_copies(dockerfile: str) -> list:
    """Build-context sources of the runtime stage's COPY lines (not --from= stages)."""
    stage = dockerfile.split("\nFROM ")[-1]
    sources = []
    for line in stage.splitlines():
        words = line.split()
        if not words or words[0] != "COPY" or any(w.startswith("--from=") for w in words):
            continue
        args = [w for w in words[1:] if not w.startswith("--")]
        sources.extend(source.rstrip("/").removeprefix("./") for source in args[:-1])
    return sources


def image_only_copies(dockerfile: str, fingerprinted: Iterable[str], runtime_paths: Iterable[str]) -> list:
    """Image files no update replaces: copied, not fingerprinted, not in the update archive.

    A directory the archive carries in part (fluid/: the package, not its
    tests) counts as carried: the update rebuilds everything that runs from it.
    """
    fingerprinted = set(fingerprinted)
    runtime_paths = tuple(runtime_paths)
    result = []
    for source in dockerfile_copies(dockerfile):
        if source in fingerprinted or source == TRUST_KEY:
            continue
        if any(source == p or source.startswith(p + "/") or p.startswith(source + "/") for p in runtime_paths):
            continue
        result.append(source)
    return result


_IMPORT = re.compile(r"""(?:^|\n)\s*import\s+(?!type\b)[^'";]*?from\s+['"](\.{1,2}/[^'"]+)['"]"""
                     r"""|require\(\s*['"](\.{1,2}/[^'"]+)['"]\s*\)""")


def launcher_files(read: Reader, entry: str = LAUNCHER_ENTRY) -> set:
    """Files the installed Windows launcher runs: the relative import closure of
    managed-entry.ts, stopping at the full app (index.ts)."""
    seen, queue = set(), [entry]
    while queue:
        path = queue.pop()
        if path in seen or path in LAUNCHER_STOP:
            continue
        data = read(path)
        if data is None:
            continue
        seen.add(path)
        if not path.endswith((".ts", ".js", ".cjs", ".mjs")):
            continue
        for match in _IMPORT.finditer(data.decode("utf8", "replace")):
            target = posixpath.normpath(posixpath.join(posixpath.dirname(path), match.group(1) or match.group(2)))
            candidates = [target] if posixpath.splitext(target)[1] else [target + ".ts", target + ".js"]
            for candidate in candidates:
                if candidate not in LAUNCHER_STOP and read(candidate) is not None:
                    queue.append(candidate)
                    break
    return seen


def classify(changed: Iterable[str], base: Reader, head: Reader) -> dict:
    """Group the changed paths of base..head by how far the in-app update reaches them."""
    changed = sorted(set(changed))
    archive = _load("source_archive", "scripts/source_archive.py")
    groups = {name: [] for name in GROUPS}

    base_files, head_files = contract_files(base) or (), contract_files(head) or ()
    if contract_fingerprint(base) != contract_fingerprint(head):
        contract = set(base_files) | set(head_files) | {CONTRACT}
        groups["docker-image"] = [p for p in changed if p in contract] or [CONTRACT]

    dockerfile = (head("Dockerfile") or b"").decode()
    copies = image_only_copies(dockerfile, head_files, archive.RUNTIME_PATHS)
    for path in changed:
        for source in copies:
            if path == source or path.startswith(source + "/"):
                groups["image-tooling" if matches(path, IMAGE_TOOLING) else "docker-image-copy"].append(path)
                break

    launcher = launcher_files(base) | launcher_files(head)
    groups["windows-launcher"] = [p for p in changed if p in launcher]
    groups["update-trust-key"] = [p for p in changed if p == TRUST_KEY]
    groups["windows-packaging"] = [p for p in changed if matches(p, WINDOWS_PACKAGING) and p != TRUST_KEY]
    groups["android-native"] = [p for p in changed if matches(p, ANDROID_NATIVE) and not matches(p, ANDROID_SERVER)]
    groups["macos-linux-desktop"] = [p for p in changed if in_archive(p, archive.RUNTIME_PATHS, archive.EXCLUDED)]
    return groups


def in_archive(path: str, runtime_paths: Iterable[str], excluded: Iterable[str]) -> bool:
    """Whether the update archive carries this path (source_archive.pathspec, read as paths and globs)."""
    if not any(path == r or path.startswith(r + "/") for r in runtime_paths):
        return False
    return not any(path == item or path.startswith(item + "/") or fnmatch.fnmatchcase(path, item) for item in excluded)


def verdict(groups: dict, allowed: Iterable[str] = ()) -> list:
    """Failing groups with changes that the owner has not acknowledged."""
    allowed = set(allowed)
    return [name for name in FAILING if groups.get(name) and name not in allowed]


EXPLAIN = {
    "docker-image": "Docker workers on the base image will refuse every later in-app update until their image is replaced.",
    "docker-image-copy": "The image runs its own copy; existing containers never see this change.",
    "windows-launcher": "Installed Windows apps run their installer's copy before every hand-over; they never see this change.",
    "update-trust-key": "Images and installed apps verify updates with the key they shipped; they will refuse every later update.",
    "image-tooling": "Owner tools inside the image stay at the image's version (not used by the running app).",
    "windows-packaging": "Only new Windows installs get packaging changes (icons, installer options).",
    "android-native": "Needs a Play build.",
    "macos-linux-desktop": "macOS and Linux desktop apps have no in-place update; these need a new installer.",
}


def latest_final_tag(root: Path = ROOT) -> Optional[str]:
    tags = [t for t in git("tag", "--list", root=root).splitlines() if FINAL_TAG.fullmatch(t)]
    return max(tags, key=lambda t: tuple(int(p) for p in t[1:].split("."))) if tags else None


def render(base: str, head: str, groups: dict, failing: list, allowed: Iterable[str]) -> str:
    lines = [f"In-app update reach for {base}..{head}"]
    for name in GROUPS:
        paths = groups[name]
        if not paths:
            continue
        if name in FAILING:
            label = "ACKNOWLEDGED" if name in set(allowed) else "OUT OF REACH"
        else:
            label = "note"
        lines.append(f"{label}: {name} ({len(paths)} file{'s' if len(paths) != 1 else ''}) - {EXPLAIN[name]}")
        lines.extend(f"    {p}" for p in paths[:8])
        if len(paths) > 8:
            lines.append(f"    ... and {len(paths) - 8} more")
    if failing:
        lines.append("FAILED: move the change into the updated source, add a migration path, "
                     "or acknowledge it with --allow " + " --allow ".join(failing) + ".")
    else:
        lines.append("Passed: every change that existing Docker and Windows installs need reaches them in-app.")
    return "\n".join(lines)


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--base", help="revision installs are on (default: newest final release tag)")
    parser.add_argument("--head", default="HEAD", help="candidate revision (default: HEAD)")
    parser.add_argument("--allow", action="append", default=[], choices=FAILING,
                        help="acknowledge an out-of-reach group the owner accepted (repeatable)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    base = args.base or latest_final_tag()
    if not base:
        parser.error("no final release tag found; pass --base")
    changed = git("diff", "--name-only", f"{base}...{args.head}").splitlines()
    groups = classify(changed, reader(base), reader(args.head))
    failing = verdict(groups, args.allow)
    if args.json:
        print(json.dumps({"base": base, "head": args.head, "groups": groups, "failing": failing}, indent=2))
    else:
        print(render(base, args.head, groups, failing, args.allow))
    return 1 if failing else 0


if __name__ == "__main__":
    sys.exit(main())
