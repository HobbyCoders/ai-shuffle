"""Release check: which changes in a range cannot reach installed apps through the in-app update.

The in-app update replaces the signed runtime source (scripts/source_archive.py)
and rebuilds it on the worker. Some files are only ever read from the Docker
image or the installed desktop launcher, so a change to them needs a new image
or installer to take effect, and some changes stop the update for older
installs altogether. See the review in the private archive

Groups that fail the check (each can be acknowledged with --allow GROUP, or
with a pull request label named update-reach:GROUP, which scripts/local_ci.py
reads when it runs this check before a push):

  docker-image      The runtime string the release publishes
                    (docker/update_contract.py: REQUIRES) differs between base
                    and head. Docker workers on older images show "Needs a newer
                    Docker image" for this release and keep their version until
                    the image is replaced. Only the owner accepts that.
  image-pin         The image inputs (docker/update_contract.py: IMAGE_INPUTS:
                    the Dockerfile, the root entrypoint and the CLI installer)
                    changed in more than comments and IMAGE_INPUTS_PIN was not
                    updated. Run python docker/update_contract.py --pin to
                    confirm that older images may keep running without it.
  docker-image-copy A file the image copies and runs from /app or /usr/local/bin
                    that is neither an image input nor in the update archive.
                    The change never reaches an existing container.
  windows-launcher  Code the installed desktop app runs from its app.asar on
                    every launch before it hands over to the updated source:
                    AI Shuffle.exe on Windows, and the packaged Linux app (deb,
                    AppImage), which updates in place but never replaces this
                    code. The change never reaches an existing install's launcher.
                    The label update-reach:windows-launcher accepts a deliberate
                    launcher change only together with launcher-version below.
  launcher-version  The launcher closure changed but desktop/launcher.json
                    "version" was not raised (desktop/LAUNCHER.md). Older and
                    newer launchers would report the same version, so neither
                    the launcher swap nor the update's launcher checks can tell
                    them apart. No label accepts this, except the owner's
                    update-reach:no-launcher-change: the change does not alter
                    what the launcher does (a move, a comment, dead code), so no
                    install needs the new launcher. That label also accepts
                    windows-launcher.
  update-trust-key  The application update public key. Images and installed
                    apps verify updates with the key they shipped; a new key
                    makes them refuse every later update.

Groups that are only reported: image inputs whose change was pinned (only new
images get it), the container supervisor and Chromium scripts (images built
before the source-supervisor hand-over run their own copy; newer images run the
updated one), owner tooling copied into the image (scripts/), Windows packaging
(icons, NSIS options), Android native code (needs a Play build, which the owner
accepts), and the macOS desktop app, which has no in-place update and needs a
new installer for every change. Windows and Linux desktop installs take the
updated source in-app; only their launcher (above) stays.

Read-only: runs git diff and git show, never fetches, tags or writes.

Usage::

    python scripts/ci/update_reach.py                      # newest final tag .. HEAD
    python scripts/ci/update_reach.py --base v0.9.0 --head origin/main
    python scripts/ci/update_reach.py --allow docker-image # owner accepted a new base
    python scripts/ci/update_reach.py --allow no-launcher-change  # behaviour-neutral launcher edit
    python scripts/ci/update_reach.py --base origin/main --labels "$LABELS"  # local_ci.py
    python scripts/ci/update_reach.py --json

Exit status: 0 when nothing unacknowledged is out of reach, 1 otherwise.
Only local draft/no-PR checks use --draft to report failures as notes with exit 0;
ready PRs and the main publication attestation keep the strict verdict.
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import importlib.util
import json
import posixpath
import types
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
# {"version": n, "minimum": m}: n must go up whenever the launcher closure changes.
LAUNCHER_JSON = "desktop/launcher.json"
# The installed launcher loads the full app (./index) only on a first run with
# no source update yet; after the first update, ./index comes from the source.
LAUNCHER_STOP = {"desktop/src/main/index.ts"}
WINDOWS_PACKAGING = ("desktop/electron-builder.yml", "desktop/build/*")
# Owner maintenance tools the image carries (python -m scripts.credential_keys).
IMAGE_TOOLING = ("scripts/*",)
# Test tools the image installs for the agent's workbench (pytest, ruff, ...).
# The Dockerfile pip-installs them after the hash-locked runtime closure
# (requirements.lock), and the running app never imports a package only they
# provide (tests/test_update_reach.py keeps that true). An existing container
# keeps its older test tools; the app it runs is unaffected.
IMAGE_DEV_TOOLING = ("requirements-dev.txt",)
ANDROID_NATIVE = ("android/*",)
# Cloud services moved to hobbycoders-cloud. Keep historical paths excluded:
# update_reach_readiness classifies past main commits as well as this deletion.
CLOUD_LEGACY_PATHS = ("android/hub/*", "android/site/*", "android/license/*",
                      "android/uptime/*", "android/ci/test-cloudflare-deploy.py")

# The supervisor, updater and Chromium scripts the image runs. In the update
# archive (docker/), so images that hand over to the active update run the new
# copy; images built before that run their own.
IMAGE_SUPERVISOR = ("docker/*",)
LABEL = "update-reach:"

FAILING = ("docker-image", "image-pin", "docker-image-copy", "windows-launcher", "launcher-version", "update-trust-key")
GROUPS = FAILING + ("image-inputs", "image-supervisor", "image-tooling", "image-dev-tooling", "windows-packaging",
                    "android-native", "macos-desktop")
# What each owner acceptance (label update-reach:NAME, or --allow NAME) accepts.
# A launcher closure change is accepted either as a deliberate launcher change
# with its version raised (windows-launcher: launcher-version must be empty),
# or as one that does not change the launcher at all (no-launcher-change).
# launcher-version has no label of its own: the label must say which it is.
NO_LAUNCHER_CHANGE = "no-launcher-change"
ACCEPTS = {**{name: (name,) for name in FAILING if name != "launcher-version"},
           NO_LAUNCHER_CHANGE: ("windows-launcher", "launcher-version")}

Reader = Callable[[str], Optional[bytes]]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git(*args: str, root: Path = ROOT) -> str:
    return subprocess.check_output(["git", *args], cwd=root, text=True).strip()


def reader(revision: str, root: Path = ROOT) -> Reader:
    """Bytes at a revision; None only for a proven absent path, never unreadable objects."""
    def read(path: str) -> Optional[bytes]:
        result = subprocess.run(["git", "show", f"{revision}:{path}"], cwd=root, capture_output=True)
        if result.returncode == 0:
            return result.stdout
        # A partial clone can have the tree but lack a promised blob. A failed
        # lazy fetch must not erase that file from the launcher's import closure.
        listing = subprocess.run(["git", "--literal-pathspecs", "ls-tree", "-z", revision, "--", path],
                                 cwd=root, capture_output=True)
        if listing.returncode == 0 and not listing.stdout:
            return None
        raise RuntimeError(f"Cannot read historical update-reach content: {revision}:{path}")
    return read


def matches(path: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def contract(read: Reader) -> Optional[types.ModuleType]:
    """docker/update_contract.py as committed at a revision (stdlib only, no side effects on import)."""
    text = read(CONTRACT)
    if text is None:
        return None
    module = types.ModuleType("update_contract_at_revision")
    exec(compile(text, CONTRACT, "exec"), module.__dict__)
    return module


def legacy_fingerprint(read: Reader, files: Iterable[str]) -> Optional[str]:
    """The runtime string before #1821: sha256 over whole files (FILES)."""
    digest = hashlib.sha256()
    for name in files:
        data = read(name)
        if data is None:
            return None
        digest.update(name.encode() + b"\0" + data + b"\0")
    return digest.hexdigest()


def published_runtime(read: Reader) -> Optional[str]:
    """The ``container.runtime`` a release built from this revision signs.

    Images compare it with the one they carry; images built before #1821
    compare nothing else. A change means older images refuse the release.
    """
    module = contract(read)
    if module is None:
        return None
    if hasattr(module, "FILES"):
        return legacy_fingerprint(read, module.FILES)
    return module.runtime(module.REQUIRES)


def image_inputs(module: Optional[types.ModuleType]) -> tuple:
    return tuple(getattr(module, "IMAGE_INPUTS", ()) or ())


def stale_pin(module: Optional[types.ModuleType], read: Reader) -> bool:
    """IMAGE_INPUTS changed beyond comments and IMAGE_INPUTS_PIN was not updated."""
    if module is None or not hasattr(module, "IMAGE_INPUTS_PIN"):
        return False
    return module.image_inputs_digest(read) != module.IMAGE_INPUTS_PIN


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


def image_only_copies(dockerfile: str, inputs: Iterable[str], runtime_paths: Iterable[str]) -> list:
    """Image files no update replaces: copied, not image inputs, not in the update archive.

    A directory the archive carries in part (fluid/: the package, not its
    tests) counts as carried: the update rebuilds everything that runs from it.
    """
    inputs = set(inputs)
    runtime_paths = tuple(runtime_paths)
    result = []
    for source in dockerfile_copies(dockerfile):
        if source in inputs or source == TRUST_KEY:
            continue
        if any(source == p or source.startswith(p + "/") or p.startswith(source + "/") for p in runtime_paths):
            continue
        result.append(source)
    return result


# Relative modules a launcher file loads: static imports (not type-only), side-effect
# imports, re-exports, dynamic import() (installed-entry.ts loads installer-updates
# that way) and require().
_IMPORT = re.compile(r"""(?:^|\n)\s*import\s+(?!type\b)[^'";]*?from\s+['"](\.{1,2}/[^'"]+)['"]"""
                     r"""|(?:^|\n)\s*import\s*['"](\.{1,2}/[^'"]+)['"]"""
                     r"""|(?:^|\n)\s*export\s+(?!type\b)[^'";]*?from\s+['"](\.{1,2}/[^'"]+)['"]"""
                     r"""|\bimport\(\s*['"](\.{1,2}/[^'"]+)['"]\s*\)"""
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
            target = posixpath.normpath(posixpath.join(posixpath.dirname(path), next(group for group in match.groups() if group)))
            candidates = [target] if posixpath.splitext(target)[1] else [target + ".ts", target + ".js"]
            for candidate in candidates:
                if candidate not in LAUNCHER_STOP and read(candidate) is not None:
                    queue.append(candidate)
                    break
    return seen


def launcher_version(read: Reader) -> Optional[int]:
    """desktop/launcher.json "version" at a revision; None when absent or not a valid contract."""
    data = read(LAUNCHER_JSON)
    if data is None:
        return None
    try:
        value = json.loads(data)
    except ValueError:
        return None
    version = value.get("version") if isinstance(value, dict) else None
    return version if isinstance(version, int) and not isinstance(version, bool) and version >= 1 else None


def launcher_raised(base: Reader, head: Reader) -> bool:
    """The head's launcher version is valid and above the base's (any valid one when the base has none)."""
    old, new = launcher_version(base), launcher_version(head)
    return new is not None and (old is None or new > old)


def classify(changed: Iterable[str], base: Reader, head: Reader) -> dict:
    """Group the changed paths of base..head by how far the in-app update reaches them."""
    changed = sorted(set(changed))
    archive = _load("source_archive", "scripts/source_archive.py")
    groups = {name: [] for name in GROUPS}

    base_contract, head_contract = contract(base), contract(head)
    if published_runtime(base) != published_runtime(head):
        groups["docker-image"] = [p for p in changed if p == CONTRACT] or [CONTRACT]
    inputs = set(image_inputs(base_contract)) | set(image_inputs(head_contract))
    changed_inputs = [p for p in changed if p in inputs]
    if stale_pin(head_contract, head):
        groups["image-pin"] = changed_inputs or [CONTRACT]
    else:
        groups["image-inputs"] = changed_inputs
    groups["image-supervisor"] = [p for p in changed if matches(p, IMAGE_SUPERVISOR) and p not in inputs
                                  and p != CONTRACT]

    dockerfile = (head("Dockerfile") or b"").decode()
    copies = image_only_copies(dockerfile, image_inputs(head_contract), archive.RUNTIME_PATHS)
    for path in changed:
        for source in copies:
            if path == source or path.startswith(source + "/"):
                if matches(path, IMAGE_DEV_TOOLING):
                    group = "image-dev-tooling"
                elif matches(path, IMAGE_TOOLING):
                    group = "image-tooling"
                else:
                    group = "docker-image-copy"
                groups[group].append(path)
                break

    launcher = launcher_files(base) | launcher_files(head)
    groups["windows-launcher"] = [p for p in changed if p in launcher]
    # Every launcher reports desktop/launcher.json's version; a closure change
    # that keeps it makes two different launchers indistinguishable (#97).
    if groups["windows-launcher"] and not launcher_raised(base, head):
        groups["launcher-version"] = list(groups["windows-launcher"])
    groups["update-trust-key"] = [p for p in changed if p == TRUST_KEY]
    groups["windows-packaging"] = [p for p in changed if matches(p, WINDOWS_PACKAGING) and p != TRUST_KEY]
    groups["android-native"] = [p for p in changed if matches(p, ANDROID_NATIVE) and not matches(p, CLOUD_LEGACY_PATHS)]
    # docker/ rides in the archive for containers only; desktops never run it.
    groups["macos-desktop"] = [p for p in changed if in_archive(p, archive.RUNTIME_PATHS, archive.EXCLUDED)
                               and not matches(p, IMAGE_SUPERVISOR)]
    return groups


def in_archive(path: str, runtime_paths: Iterable[str], excluded: Iterable[str]) -> bool:
    """Whether the update archive carries this path (source_archive.pathspec, read as paths and globs)."""
    if not any(path == r or path.startswith(r + "/") for r in runtime_paths):
        return False
    return not any(path == item or path.startswith(item + "/") or fnmatch.fnmatchcase(path, item) for item in excluded)


def accepted(allowed: Iterable[str]) -> set:
    """The failing groups the owner's acceptances (group names or no-launcher-change) cover."""
    return {group for name in allowed for group in ACCEPTS.get(name, ())}


def verdict(groups: dict, allowed: Iterable[str] = ()) -> list:
    """Failing groups with changes that the owner has not acknowledged."""
    covered = accepted(allowed)
    return [name for name in FAILING if groups.get(name) and name not in covered]


def labels_for(failing: Iterable[str]) -> list:
    """The acceptance labels that would cover these groups (launcher-version has only no-launcher-change)."""
    names = []
    for name in failing:
        option = NO_LAUNCHER_CHANGE if name == "launcher-version" else name
        if option not in names:
            names.append(option)
    return names


EXPLAIN = {
    "docker-image": "This release requires more than older Docker images provide (docker/update_contract.py: REQUIRES). "
                    "Those workers will show \"Needs a newer Docker image\" and keep their version until the image is "
                    "replaced. Warn a release ahead with RECOMMENDS; only the owner accepts this.",
    "image-pin": "These files are baked into the Docker image and no update replaces them. If the app does not "
                 "depend on the change, run: python docker/update_contract.py --pin and commit. If it does, also "
                 "add a capability to REQUIRES.",
    "docker-image-copy": "The image runs its own copy; existing containers never see this change.",
    "windows-launcher": "Installed Windows and Linux desktop apps run their installer's copy (app.asar) before every "
                        "hand-over; their launcher never sees this change. Raise desktop/launcher.json \"version\" "
                        "(desktop/LAUNCHER.md) and have the owner accept the launcher change.",
    "launcher-version": "The launcher closure changed but desktop/launcher.json \"version\" was not raised, so older "
                        "and newer launchers report the same version (desktop/LAUNCHER.md). Raise it in this range. "
                        "Only if the change leaves what the launcher does unchanged may the owner accept it with "
                        "update-reach:no-launcher-change instead; update-reach:windows-launcher does not accept it.",
    "update-trust-key": "Images and installed apps verify updates with the key they shipped; they will refuse every later update.",
    "image-inputs": "Only new Docker images get this (pinned: older images keep taking updates without it).",
    "image-supervisor": "Images that hand over to the updated supervisor get this with the update; older images keep "
                        "their own copy until the image is replaced. Keep it compatible with both.",
    "image-tooling": "Owner tools inside the image stay at the image's version (not used by the running app).",
    "image-dev-tooling": "Test tools inside the image (the agent's workbench) stay at the image's version; the "
                         "running app never imports them. Only new images get the change.",
    "windows-packaging": "Only new Windows installs get packaging changes (icons, installer options).",
    "android-native": "Needs a Play build.",
    "macos-desktop": "macOS desktop apps have no in-place update; these need a new installer (Windows and Linux "
                     "installs take them in-app).",
}


def latest_final_tag(root: Path = ROOT) -> Optional[str]:
    tags = [t for t in git("tag", "--list", root=root).splitlines() if FINAL_TAG.fullmatch(t)]
    return max(tags, key=lambda t: tuple(int(p) for p in t[1:].split("."))) if tags else None


def render(base: str, head: str, groups: dict, failing: list, allowed: Iterable[str], *, draft: bool = False) -> str:
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
    options = labels_for(failing)
    bump = ("raise desktop/launcher.json \"version\" for the launcher change, or " if "launcher-version" in failing
            else "")
    if failing and draft:
        lines.append("NOTE — OUT OF REACH: " + ", ".join(failing) + ". Push/open a DRAFT PR only; "
                     "before readiness/merge, " + bump + "move the change into updated source, add a migration "
                     "path, or ask the owner to accept it with PR labels: "
                     + ", ".join(LABEL + name for name in options) + ".")
    elif failing:
        lines.append("FAILED: " + bump + "move the change into the updated source, add a migration path, or have "
                     "the owner accept it (pull request label " + ", ".join(LABEL + name for name in options)
                     + ", then re-run this check; locally --allow " + " --allow ".join(options) + ").")
    else:
        lines.append("Passed: every change that existing Docker, Windows and Linux installs need reaches them in-app.")
    return "\n".join(lines)


def allowed_by_labels(labels: str) -> list:
    """Owner acceptances named by update-reach:NAME labels (a group, or no-launcher-change)."""
    names = [label.strip().removeprefix(LABEL) for label in labels.split(",") if label.strip().startswith(LABEL)]
    return [name for name in names if name in ACCEPTS]


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--base", help="revision installs are on (default: newest final release tag)")
    parser.add_argument("--head", default="HEAD", help="candidate revision (default: HEAD)")
    parser.add_argument("--allow", action="append", default=[], choices=sorted(ACCEPTS),
                        help="acknowledge an out-of-reach group the owner accepted, or no-launcher-change (repeatable)")
    parser.add_argument("--labels", default="",
                        help="comma-separated pull request labels; update-reach:GROUP acknowledges GROUP")
    parser.add_argument("--draft", action="store_true",
                        help="local pre-push only: report unaccepted groups as notes for a draft/no PR")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    args.allow += allowed_by_labels(args.labels)
    base = args.base or latest_final_tag()
    if not base:
        parser.error("no final release tag found; pass --base")
    changed = git("diff", "--name-only", f"{base}...{args.head}").splitlines()
    groups = classify(changed, reader(base), reader(args.head))
    failing = verdict(groups, args.allow)
    if args.json:
        print(json.dumps({"base": base, "head": args.head, "groups": groups, "failing": failing}, indent=2))
    else:
        print(render(base, args.head, groups, failing, args.allow, draft=args.draft))
    return 1 if failing and not args.draft else 0


if __name__ == "__main__":
    sys.exit(main())
