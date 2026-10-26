"""The frozen public-root record and the container sequence it anchors.

The public history starts at one root commit carrying release/public-cutover.json
and release/container-sequence-offset. Publishers read them to continue the
container sequence and desktop signing floor from the archived history; both
files are immutable for this history epoch. Nothing here reads the archive.

The public root is the root on the revision's first-parent chain. The only other
roots allowed are those of the reviewed, scrubbed history imports pinned in
release/imported-history.json (immutable once committed). Every ancestor must
either descend from the public root or be in a pinned import head's history, so
an accidental merge of private history still refuses, even one that shares a
root commit with the import.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[2]
MAPPING = "release/public-cutover.json"
OFFSET = "release/container-sequence-offset"
IMPORTS = "release/imported-history.json"
# Names the frozen record carries; only validated, never contacted.
OLD_REPO = "HobbyCoders/ai-shuffle-private"
NEW_REPO = "HobbyCoders/ai-shuffle"


def git(root, *args):
    return subprocess.check_output(["git", *args], cwd=root, text=True).strip()


def is_ancestor(root, commit, revision):
    result = subprocess.run(["git", "merge-base", "--is-ancestor", commit, revision],
                            cwd=root, capture_output=True)
    return result.returncode == 0


def imported_history(root, revision):
    """The committed, reviewed history imports: [{"head": SHA, "roots": [SHA, ...]}]."""
    changes = git(root, "log", "--first-parent", "--format=%H", revision, "--", IMPORTS).splitlines()
    if len(changes) > 1:
        raise ValueError("The imported history record is immutable once committed")
    if not changes:
        return []
    record = json.loads(git(root, "show", f"{revision}:{IMPORTS}"))
    imports = record.get("imports") if isinstance(record, dict) and record.get("format") == 1 else None
    if not isinstance(imports, list) or not imports:
        raise ValueError("Unsupported imported history record")
    sha = re.compile(r"[0-9a-f]{40}")
    for item in imports:
        if (not isinstance(item, dict) or set(item) != {"head", "roots"}
                or not isinstance(item["head"], str) or not sha.fullmatch(item["head"])
                or not isinstance(item["roots"], list) or not item["roots"]
                or any(not isinstance(r, str) or not sha.fullmatch(r) for r in item["roots"])):
            raise ValueError("Invalid imported history entry")
    return imports


def check_imports(root, revision, initial, roots):
    """Allow extra roots only from the pinned, reviewed history imports."""
    extra = set(roots) - {initial}
    if any(git(root, "ls-tree", other, "--", MAPPING) for other in extra):
        raise ValueError("Only the public root may carry the cutover mapping; private ancestors are forbidden")
    imports = imported_history(root, revision)
    if not extra:
        return
    if not extra <= {r for item in imports for r in item["roots"]}:
        raise ValueError("Public cutover history must have exactly one root; private ancestors are forbidden")
    heads = []
    for item in imports:
        if is_ancestor(root, item["head"], revision):
            if set(git(root, "rev-list", "--max-parents=0", item["head"]).splitlines()) != set(item["roots"]):
                raise ValueError("Imported history roots do not match the pinned head")
            heads.append(item["head"])
    # A root alone is not proof: raw private history can share a root commit
    # with the scrubbed import. Every commit must descend from the public
    # root or belong to a pinned import head's history.
    own = set(git(root, "rev-list", "--ancestry-path", f"{initial}..{revision}").splitlines()) | {initial}
    outside = set(git(root, "rev-list", revision, "--not", *heads, "--").splitlines()) - own
    if outside:
        raise ValueError("History outside the public root and the pinned imports; private ancestors are forbidden")


def mapping(root, revision="HEAD"):
    if git(root, "rev-parse", "--is-shallow-repository") != "false":
        raise ValueError("Complete history required")
    roots = git(root, "rev-list", "--max-parents=0", revision).splitlines()
    # The epoch root is the one reached through first parents; other roots
    # come from merges (legacy vendored projects or a pinned history import).
    initial = git(root, "rev-list", "--first-parent", "--max-parents=0", revision)
    if not git(root, "ls-tree", initial, "--", MAPPING):
        if any(git(root, "ls-tree", other, "--", MAPPING) for other in roots):
            raise ValueError("Public cutover history must have exactly one root; private ancestors are forbidden")
        # Legacy history, or an old export that may carry the record but may
        # never publish with it.
        return initial, None
    check_imports(root, revision, initial, roots)
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
        # Reject even a changed-and-reverted offset/record in this epoch. Walk
        # first parents only: a merge that changes either file differs from
        # its first parent, while pinned imported history is never walked.
        if git(root, "log", "--first-parent", "--format=%H", f"{initial}..{revision}", "--", OFFSET, MAPPING):
            raise ValueError("Cutover mapping and offset are immutable for this history epoch")
        if git(root, "show", f"{initial}:{OFFSET}") != raw:
            raise ValueError("Offset differs from the public root")
    elif record or count <= 1:
        raise ValueError("Single-commit publication requires a positive mapped offset")
    sequence = offset + count
    if not 0 < sequence < 2**53:
        raise ValueError("Container sequence outside the safe integer range")
    return sequence
