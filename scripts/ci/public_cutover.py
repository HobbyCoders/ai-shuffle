"""The frozen public-root record and the container sequence it anchors.

The public history starts at one root commit carrying release/public-cutover.json
and release/container-sequence-offset. Publishers read them to continue the
container sequence and desktop signing floor from the archived history; both
files are immutable for this history epoch. Nothing here reads the archive.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[2]
MAPPING = "release/public-cutover.json"
OFFSET = "release/container-sequence-offset"
# Names the frozen record carries; only validated, never contacted.
OLD_REPO = "HobbyCoders/ai-shuffle-private"
NEW_REPO = "HobbyCoders/ai-shuffle"


def git(root, *args):
    return subprocess.check_output(["git", *args], cwd=root, text=True).strip()


def mapping(root, revision="HEAD"):
    if git(root, "rev-parse", "--is-shallow-repository") != "false":
        raise ValueError("Complete history required")
    roots = git(root, "rev-list", "--max-parents=0", revision).splitlines()
    if len(roots) != 1:
        # Legacy history includes unrelated-history merges (e.g. vendored
        # projects). Only the NEW mapped epoch must have exactly one root.
        if any(git(root, "ls-tree", initial, "--", MAPPING) for initial in roots):
            raise ValueError("Public cutover history must have exactly one root; private ancestors are forbidden")
        initial = git(root, "rev-list", "--first-parent", "--max-parents=0", revision)
        return initial, None
    initial = roots[0]
    if not git(root, "ls-tree", initial, "--", MAPPING):
        # An old export may carry the record, but may never publish with it.
        return initial, None
    record = json.loads(git(root, "show", f"{initial}:{MAPPING}"))
    if (record.get("format") != 1 or record.get("old_repository") != OLD_REPO
            or record.get("new_repository") != NEW_REPO):
        raise ValueError("Unsupported cutover mapping")
    for key in ("old_repository_id", "export_pr", "maximum_shipped_sequence", "maximum_desktop_sequence"):
        if type(record.get(key)) is not int or not 0 < record[key] < 2**53:
            raise ValueError(f"Invalid {key}")
    for key, length in (("published_revision", 40), ("envelope_sha256", 64)):
        if not re.fullmatch(r"[0-9a-f]{%d}" % length, record.get(key, "")):
            raise ValueError(f"Invalid {key}")
    return initial, record


def container_sequence(root, revision="HEAD"):
    revision = git(root, "rev-parse", f"{revision}^{{commit}}")
    initial, record = mapping(root, revision)
    raw = git(root, "show", f"{revision}:{OFFSET}")
    if not re.fullmatch(r"0|[1-9][0-9]*", raw):
        raise ValueError("Missing or malformed committed container offset")
    offset = int(raw)
    count = int(git(root, "rev-list", "--first-parent", "--count", revision))
    if offset:
        if not record or offset != record["maximum_shipped_sequence"]:
            raise ValueError("Positive offset requires the public root mapping; never publish the private export")
        # Reject even a changed-and-reverted offset/record in this epoch.
        if git(root, "log", "--format=%H", f"{initial}..{revision}", "--", OFFSET, MAPPING):
            raise ValueError("Cutover mapping and offset are immutable for this history epoch")
        if git(root, "show", f"{initial}:{OFFSET}") != raw:
            raise ValueError("Offset differs from the public root")
    elif record or count <= 1:
        raise ValueError("Single-commit publication requires a positive mapped offset")
    sequence = offset + count
    if not 0 < sequence < 2**53:
        raise ValueError("Container sequence outside the safe integer range")
    return sequence
