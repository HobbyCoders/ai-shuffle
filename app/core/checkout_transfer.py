"""Bounded Git snapshots for an explicit, online worker handoff.

Snapshot commits use a private index and temporary ref. The owner's branch,
index and working files are never changed. Ignored files are not transferred.
"""
from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import uuid

MAX_BUNDLE = 128 * 1024 * 1024


def git(cwd, *args, env=None):
    result = subprocess.run(
        ["git", "-c", "core.hooksPath=", "-c", "core.fsmonitor=false", *args],
        cwd=cwd, env=env, capture_output=True, timeout=120,
    )
    if result.returncode:
        raise ValueError("Git could not prepare the worker handoff: " + result.stderr.decode("utf-8", "replace")[:500])
    return result.stdout


def _trees(cwd, scratch):
    env = dict(os.environ, GIT_INDEX_FILE=str(Path(scratch) / "index"))
    index = git(cwd, "rev-parse", "--git-path", "index").decode().strip()
    index = Path(index) if Path(index).is_absolute() else Path(cwd) / index
    if index.exists():
        shutil.copyfile(index, env["GIT_INDEX_FILE"])
    else:
        git(cwd, "read-tree", "HEAD", env=env)
    entries = git(cwd, "ls-files", "--stage", env=env)
    if any(line.startswith(b"160000 ") for line in entries.splitlines()):
        raise ValueError("Resume cannot transfer submodule work; finish it on the owning worker first.")
    if any(line.startswith(b"S ") or line[:1].islower()
           for line in git(cwd, "ls-files", "-v", env=env).splitlines()):
        raise ValueError("Resume cannot transfer sparse or assume-unchanged files.")
    for name in git(cwd, "ls-files", "--others", "--exclude-standard", "-z", env=env).decode().split("\0"):
        if name and (Path(name).name == ".env" or Path(name).suffix.lower() in (".pem", ".key")):
            raise ValueError("Untracked credential files must be ignored or moved before resuming elsewhere.")
    index_tree = git(cwd, "write-tree", env=env).decode().strip()
    git(cwd, "add", "-A", "--", ".", env=env)
    work_tree = git(cwd, "write-tree", env=env).decode().strip()
    head = git(cwd, "rev-parse", "HEAD").decode().strip()
    return head, index_tree, work_tree


def fingerprint(cwd):
    with tempfile.TemporaryDirectory(prefix="shuffle-handoff-") as scratch:
        return ":".join(_trees(cwd, scratch))


def export_checkout(cwd, branch):
    git(cwd, "check-ref-format", "refs/heads/" + branch)
    actual = git(cwd, "symbolic-ref", "--short", "HEAD").decode().strip()
    if actual != branch:
        raise ValueError("The owning worker's checkout is on a different branch.")
    ref = "refs/shuffle-handoff/" + uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix="shuffle-handoff-") as scratch:
        head, index_tree, work_tree = _trees(cwd, scratch)
        args = ("-c", "user.name=AI Shuffle", "-c", "user.email=handoff@localhost", "commit-tree")
        staged = git(cwd, *args, index_tree, "-p", head, "-m", "Handoff index").decode().strip()
        snapshot = git(cwd, *args, work_tree, "-p", staged, "-m", "Handoff worktree").decode().strip()
        git(cwd, "update-ref", ref, snapshot)
        try:
            bundle = Path(scratch) / "state.bundle"
            git(cwd, "bundle", "create", str(bundle), ref)
            if bundle.stat().st_size > MAX_BUNDLE:
                raise ValueError("This checkout exceeds the 128 MiB resume transfer limit.")
            data = bundle.read_bytes()
        finally:
            git(cwd, "update-ref", "-d", ref)
        signature = ":".join((head, index_tree, work_tree))
        if fingerprint(cwd) != signature:
            raise ValueError("Files changed while preparing resume. Try again when the worker is idle.")
        return dict(branch=branch, head=head, index_tree=index_tree, snapshot=snapshot,
                    fingerprint=signature, bundle=base64.b64encode(data).decode(),
                    sha256=hashlib.sha256(data).hexdigest())


def import_checkout(base, destination, state):
    """Create a fresh checkout, refusing existing paths or divergent local refs."""
    branch = state["branch"]
    git(base, "check-ref-format", "refs/heads/" + branch)
    dest = Path(destination)
    if dest.exists() or dest.is_symlink():
        # A retry after a lost acknowledgment can reuse its exact prepared
        # checkout, but cannot reset or overwrite even one local modification.
        known_paths = [line.removeprefix("worktree ") for line in
                       git(base, "worktree", "list", "--porcelain").decode().splitlines()
                       if line.startswith("worktree ")]
        known = any(Path(path).resolve() == dest.resolve() for path in known_paths)
        if (known and not dest.is_symlink()
                and git(dest, "symbolic-ref", "--short", "HEAD").decode().strip() == branch
                and fingerprint(dest) == state["fingerprint"]):
            return str(dest)
        raise ValueError("A local checkout already exists for this branch. Preserve or move it before resuming here.")
    # A ref already checked out elsewhere must never be repointed under that tree.
    listing = git(base, "worktree", "list", "--porcelain").decode()
    if "branch refs/heads/" + branch in listing.splitlines():
        raise ValueError("This branch is already checked out locally; its files were not changed.")
    encoded = state.get("bundle", "")
    if len(encoded) > (MAX_BUNDLE * 4 // 3 + 4):
        raise ValueError("Resume bundle exceeds the transfer limit.")
    data = base64.b64decode(encoded, validate=True)
    if hashlib.sha256(data).hexdigest() != state["sha256"]:
        raise ValueError("Resume bundle checksum mismatch.")
    ref = "refs/shuffle-handoff/" + uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix="shuffle-handoff-") as scratch:
        bundle = Path(scratch) / "state.bundle"
        bundle.write_bytes(data)
        git(base, "bundle", "verify", str(bundle))
        heads = git(base, "bundle", "list-heads", str(bundle)).decode().splitlines()
        if len(heads) != 1 or heads[0].split()[0] != state["snapshot"]:
            raise ValueError("Resume bundle has an unexpected head.")
        git(base, "fetch", "--no-tags", str(bundle), heads[0].split()[1] + ":" + ref)
    try:
        if git(base, "rev-parse", ref + "^^").decode().strip() != state["head"]:
            raise ValueError("Resume bundle has an unexpected history.")
        if git(base, "rev-parse", ref + "^{tree}").decode().strip() != state["fingerprint"].split(":")[2]:
            raise ValueError("Resume bundle has an unexpected working tree.")
        if git(base, "rev-parse", ref + "^^{tree}").decode().strip() != state["index_tree"]:
            raise ValueError("Resume bundle has an unexpected index.")
        old = git(base, "for-each-ref", "--format=%(objectname)", "refs/heads/" + branch).decode().strip()
        if old:
            # Only a fast-forward can replace an un-checked-out local branch.
            git(base, "merge-base", "--is-ancestor", old, state["head"])
        dest.parent.mkdir(parents=True, exist_ok=True)
        git(base, "worktree", "add", "--detach", str(dest), ref)
        git(base, "update-ref", "refs/heads/" + branch, state["head"], old or "0" * 40)
        git(dest, "symbolic-ref", "HEAD", "refs/heads/" + branch)
        git(dest, "read-tree", state["index_tree"])
        return str(dest)
    finally:
        git(base, "update-ref", "-d", ref)
