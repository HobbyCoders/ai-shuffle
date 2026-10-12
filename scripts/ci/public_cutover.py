"""Fail-closed, one-epoch public-root bridge. Never imports private Git objects.

The record lives at release/public-cutover.json in the public root. The owner
sets AI_SHUFFLE_PRIVATE_CHECKOUT to a separate complete private checkout for
local attestation. Public publishers need only the resulting exact-main status.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[2]
MAPPING = "release/public-cutover.json"
OFFSET = "release/container-sequence-offset"
OLD_REPO = "HobbyCoders/ai-shuffle-private"
NEW_REPO = "HobbyCoders/ai-shuffle"


def git(root, *args):
    return subprocess.check_output(["git", *args], cwd=root, text=True).strip()


def api(repository, route=""):
    return json.loads(subprocess.check_output(
        ["gh", "api", f"repos/{repository}" + (f"/{route}" if route else "")], text=True, timeout=30))


def public_root(root, revision="HEAD"):
    if git(root, "rev-parse", "--is-shallow-repository") != "false":
        raise ValueError("Complete history required")
    roots = git(root, "rev-list", "--max-parents=0", revision).splitlines()
    if len(roots) != 1:
        raise ValueError("Expected exactly one root")
    return roots[0]


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


def verify_bridge(root, revision, published, envelope, *, private_root=None, github=api, walk=None):
    """Verify identity, signed baseline, complete exact tree and each old PR's reach."""
    initial, record = mapping(root, revision)
    if record is None:
        raise ValueError("No configured public-root transition")
    if published != record["published_revision"] or hashlib.sha256(envelope).hexdigest() != record["envelope_sha256"]:
        raise ValueError("Published envelope does not match the frozen cutover baseline")
    # Caller verifies the signature with the current trust key before calling.
    private_root = Path(private_root or os.environ.get("AI_SHUFFLE_PRIVATE_CHECKOUT", ""))
    if not private_root.is_absolute() or private_root.resolve() == Path(root).resolve():
        raise ValueError("A separate AI_SHUFFLE_PRIVATE_CHECKOUT is required")
    if git(private_root, "rev-parse", "--is-shallow-repository") != "false":
        raise ValueError("Complete private history required")
    identity = github(OLD_REPO, "")
    if identity.get("id") != record["old_repository_id"] or identity.get("full_name") != OLD_REPO or identity.get("private") is not True:
        raise ValueError("Private repository identity mismatch")
    branch = github(OLD_REPO, "branches/main")
    pr = github(OLD_REPO, f"pulls/{record['export_pr']}")
    if (not branch.get("protected") or not pr.get("merged_at")
            or pr.get("base", {}).get("ref") != "main"
            or pr.get("base", {}).get("repo", {}).get("id") != record["old_repository_id"]):
        raise ValueError("Export must be a merged PR on protected private main")
    export = pr.get("merge_commit_sha", "")
    if not re.fullmatch(r"[0-9a-f]{40}", export):
        raise ValueError("Invalid export SHA")
    if export not in git(private_root, "rev-list", "--first-parent", branch["commit"]["sha"]).splitlines():
        raise ValueError("Export is not on private main")
    if published not in git(private_root, "rev-list", "--first-parent", export).splitlines():
        raise ValueError("Frozen publication is not on the export first-parent history")
    if git(private_root, "rev-parse", f"{export}^{{tree}}") != git(root, "rev-parse", f"{initial}^{{tree}}"):
        raise ValueError("Private export and public root must have the identical complete Git tree")
    container_sequence(root, revision)
    if walk is None:
        raise ValueError("Strict private update-reach walker is required")
    accepted, reason = walk(export, base=published, root=private_root,
                            repository=OLD_REPO, api=lambda route: github(OLD_REPO, route), bridge=False)
    if not accepted:
        raise ValueError(reason)
    return initial, export
