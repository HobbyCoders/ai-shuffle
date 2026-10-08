"""Resume a Git branch through its configured origin, never a file bundle.

The owner commits non-ignored work and pushes without force. The receiver uses
its own origin credentials and only fast-forwards clean local checkouts.
"""
from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

from fluid.tools.child_env import scrub_child_env


def _run(cwd, *args, timeout=120):
    try:
        return subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, timeout=timeout,
            env={**scrub_child_env(), "GIT_TERMINAL_PROMPT": "0"},
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError("Git timed out during resume. Check the remote and retry; ownership has not moved.") from exc


def git(cwd, *args):
    result = _run(cwd, *args)
    if result.returncode:
        # Remote URLs and credential-helper output can contain secrets.
        raise ValueError(f"Git {args[0]} failed during resume. Check Git credentials, hooks and branch conflicts on that worker; ownership has not moved.")
    return result.stdout


def _is_ancestor(cwd, older, newer):
    """True when commit ``older`` is ``newer`` or reachable from it.

    A commit this repository does not have is not an ancestor of anything here.
    """
    if older == newer:
        return True
    if _run(cwd, "cat-file", "-e", older + "^{commit}").returncode:
        return False
    result = _run(cwd, "merge-base", "--is-ancestor", older, newer)
    if result.returncode not in (0, 1):
        git(cwd, "merge-base", "--is-ancestor", older, newer)  # raises the generic error
    return result.returncode == 0


def _contains_changes(cwd, published, tip):
    """True when merging ``tip`` into ``published`` would change nothing.

    This recognises a branch that was squash-merged or rebased upstream: its
    commits are not ancestors of the published tip, but their content is
    already there. Git older than 2.38 has no ``merge-tree --write-tree``;
    then this answers False and the commits count as unpublished.
    """
    result = _run(cwd, "merge-tree", "--write-tree", published, tip)
    if result.returncode:
        return False
    merged = result.stdout.decode().split("\n", 1)[0].strip()
    return merged == git(cwd, "rev-parse", published + "^{tree}").decode().strip()


def _unpublished(cwd, tip, published):
    """Commits on ``tip`` that exist nowhere on origin, newest first.

    ``published`` lists commits just confirmed on origin (the handed-off head
    and the freshly fetched branch tip), so a stale or missing remote-tracking
    ref cannot make published work look local. A commit counts as published
    when it is reachable from one of them, has a patch-equivalent commit there
    (rebased or cherry-picked), or the whole branch is already contained in one
    of them (squash-merged).
    """
    extra = git(cwd, "rev-list", tip, "--not", *published).decode().split()
    if not extra:
        return []
    equivalent = {line[2:].strip() for line in git(cwd, "cherry", published[0], tip).decode().splitlines()
                  if line.startswith("- ")}
    pending = [sha for sha in extra if sha not in equivalent]
    if pending and any(_contains_changes(cwd, ref, tip) for ref in published):
        return []
    return pending


def _plural(count, noun):
    return f"{count} {noun}" + ("" if count == 1 else "s")


def _describe_commits(cwd, shas):
    subject = git(cwd, "log", "-1", "--format=%h %s", shas[0]).decode().strip()
    return f"{_plural(len(shas), 'commit')} (newest: {subject})"


def _require_clean(cwd, branch, where):
    """Refuse a checkout with uncommitted work, naming what is uncommitted."""
    entries = _status(cwd).decode(errors="replace").split("\0")
    changed, untracked, skip = [], 0, False
    for entry in entries:
        if skip or not entry:
            skip = False
            continue
        code, name = entry[:2], entry[3:]
        # A rename or copy is followed by its source path.
        skip = code[0] in "RC"
        changed.append(name)
        untracked += code == "??"
    if not changed:
        return
    tracked = len(changed) - untracked
    parts = [_plural(tracked, "modified file")] if tracked else []
    parts += [_plural(untracked, "untracked file")] if untracked else []
    sample = ", ".join(changed[:3]) + (", ..." if len(changed) > 3 else "")
    raise ValueError(
        f"The receiving checkout has local changes: {where} on branch {branch} has "
        f"{' and '.join(parts)} ({sample}). Commit and push, stash or discard them on "
        "that worker, then try again. Nothing was changed."
    )


def _require_published(cwd, branch, current, head, published, where):
    """Refuse to move ``branch`` off ``current`` if that strands local commits.

    Only commits that exist nowhere on origin count. Commits that were pushed,
    merged, squash-merged or rebased upstream are safe to move past.
    """
    if _is_ancestor(cwd, current, head):
        return
    pending = _unpublished(cwd, current, published)
    if pending:
        raise ValueError(
            f"Branch {branch} in {where} has {_describe_commits(cwd, pending)} that "
            f"{'is' if len(pending) == 1 else 'are'} not on origin. Push them from that worker, "
            "or move them to another branch there, then try again. Nothing was changed."
        )


def _remote_key(url):
    """Compare SSH/HTTPS repository identities without sending credentials."""
    url = url.strip().rstrip("/")
    if "://" in url and not url.startswith("file://"):
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        port = parsed.port
        if port and port != {"ssh": 22, "https": 443, "http": 80, "git": 9418}.get(parsed.scheme):
            host += ":" + str(port)
        path = parsed.path.strip("/")
        identity = host + "/" + (path.lower() if host == "github.com" else path)
    elif re.match(r"^(?:[^/@:]+@)?[^/:]+:", url) and not re.match(r"^[A-Za-z]:[\\/]", url):
        host, path = url.split(":", 1)
        host = host.rsplit("@", 1)[-1].lower()
        path = path.strip("/")
        identity = host + "/" + (path.lower() if host == "github.com" else path)
    else:
        identity = url
    identity = identity.removesuffix(".git")
    return hashlib.sha256(identity.encode()).hexdigest()


def remote_identity(cwd):
    if git(cwd, "rev-parse", "--is-inside-work-tree").strip() != b"true":
        raise ValueError("Resume here requires a Git repository with an origin remote.")
    fetch = git(cwd, "remote", "get-url", "--all", "origin").decode().splitlines()
    push = git(cwd, "remote", "get-url", "--push", "--all", "origin").decode().splitlines()
    if len(fetch) != 1 or len(push) != 1 or _remote_key(fetch[0]) != _remote_key(push[0]):
        raise ValueError("Resume here requires one matching fetch/push origin repository.")
    return _remote_key(fetch[0])


def _branch(cwd, branch):
    git(cwd, "check-ref-format", "refs/heads/" + branch)
    if git(cwd, "symbolic-ref", "--short", "HEAD").decode().strip() != branch:
        raise ValueError("The worker checkout is on a different branch.")


def _status(cwd):
    return git(cwd, "status", "--porcelain=v1", "-z", "--untracked-files=all")


def fingerprint(cwd):
    """Only a clean checkout can finish a handoff."""
    if _status(cwd):
        raise ValueError("The owning worker's files changed during resume; ownership was not moved.")
    return git(cwd, "rev-parse", "HEAD").decode().strip()


def _preflight(cwd):
    # Do not commit half-completed Git operations or silently omit nested work.
    for marker in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply", "sequencer"):
        path = Path(git(cwd, "rev-parse", "--git-path", marker).decode().strip())
        if (path if path.is_absolute() else Path(cwd) / path).exists():
            raise ValueError("Finish the in-progress Git operation before resuming elsewhere.")
    if git(cwd, "ls-files", "--unmerged"):
        raise ValueError("Resolve Git conflicts before resuming elsewhere.")
    entries = git(cwd, "ls-files", "--stage")
    if any(line.startswith(b"160000 ") for line in entries.splitlines()):
        raise ValueError("Resume does not support repositories with submodules.")
    if any(line.startswith(b"S ") or line[:1].islower() for line in git(cwd, "ls-files", "-v").splitlines()):
        raise ValueError("Resume cannot commit sparse or assume-unchanged files.")
    for name in git(cwd, "ls-files", "--cached", "--others", "--exclude-standard", "-z").decode().split("\0"):
        if name and _is_credential_file(Path(cwd) / name):
            raise ValueError(f"Credential file {name} must be removed from tracking and ignored before resuming elsewhere.")


def _is_credential_file(path):
    """A .env file, or a .pem/.key file that is not clearly public.

    Public keys and certificates (for example a release-signing public key) are
    safe to push and must not block a move. Anything else with those suffixes,
    including unreadable or unrecognised content, counts as a credential, so
    the check fails closed.
    """
    if path.name == ".env":
        return True
    if path.suffix.lower() not in (".pem", ".key"):
        return False
    try:
        with open(path, "rb") as handle:
            head = handle.read(65536)
    except OSError:
        return True
    if any(marker in head for marker in (b"PRIVATE KEY", b"BEGIN OPENSSH", b"PuTTY-User-Key-File")):
        return True
    public = (b"-----BEGIN PUBLIC KEY-----", b"-----BEGIN RSA PUBLIC KEY-----",
              b"-----BEGIN CERTIFICATE-----", b"-----BEGIN PGP PUBLIC KEY BLOCK-----")
    return not any(marker in head for marker in public)


def export_checkout(cwd, branch, expected_remote=None):
    """Commit and push the owner branch; return identifiers, never file bytes."""
    _branch(cwd, branch)
    remote = remote_identity(cwd)
    if expected_remote is not None and remote != expected_remote:
        raise ValueError("The workers' origin remotes refer to different repositories.")
    _preflight(cwd)
    if _status(cwd):
        git(cwd, "add", "-A", "--", ".")
        _preflight(cwd)
        git(cwd, "commit", "-m", "Save work before resuming on another worker")
    head = fingerprint(cwd)
    # Compare with origin as it is now, not a remote-tracking ref from the
    # last fetch: a checkout that is clean and already on origin (in sync, or
    # behind because others pushed since) has nothing to publish.
    upstream = _fetch_branch(cwd, branch)
    if upstream is None or not _is_ancestor(cwd, head, upstream):
        if upstream is not None and not _is_ancestor(cwd, upstream, head):
            ahead = git(cwd, "rev-list", head, "--not", upstream).decode().split()
            behind = git(cwd, "rev-list", upstream, "--not", head).decode().split()
            raise ValueError(
                f"Branch {branch} in the owning worker's checkout {cwd} has "
                f"{_describe_commits(cwd, ahead)} that {'is' if len(ahead) == 1 else 'are'} "
                f"not on origin/{branch}, and "
                f"origin/{branch} has {_plural(len(behind), 'commit')} that it does not have. "
                f"Pull or rebase {branch} on that worker so it can be pushed without force, "
                "then try again. Ownership has not moved."
            )
        # Push this exact commit, not a moving HEAD, and never force-update a branch.
        git(cwd, "push", "origin", head + ":refs/heads/" + branch)
        upstream = head
    published = git(cwd, "ls-remote", "--exit-code", "origin", "refs/heads/" + branch).decode().split()
    if not published or published[0] != upstream or fingerprint(cwd) != head:
        raise ValueError("The branch changed during resume. Retry when both workers are idle.")
    return dict(protocol="git-origin-v1", branch=branch, head=head, fingerprint=head, remote=remote)


def _fetch_branch(cwd, branch):
    """Fetch ``branch`` from origin; return its tip, or None when origin lacks it."""
    listed = git(cwd, "ls-remote", "origin", "refs/heads/" + branch).decode().split()
    if not listed:
        return None
    # Forced: a remote-tracking ref is only a cache of origin.
    git(cwd, "fetch", "--no-tags", "origin", "+refs/heads/" + branch + ":refs/remotes/origin/" + branch)
    return git(cwd, "rev-parse", "FETCH_HEAD").decode().strip()


def receiving_checkout(base, destination, branch):
    """The checkout an import of ``branch`` would change (read-only).

    A registered checkout of the branch, including the project's base
    checkout, is reused; otherwise ``destination`` would be created.
    """
    for entry in git(base, "worktree", "list", "--porcelain").decode().split("\n\n"):
        lines = entry.splitlines()
        if "branch refs/heads/" + branch in lines:
            return Path(next(line.removeprefix("worktree ") for line in lines if line.startswith("worktree ")))
    return Path(destination)


def import_checkout(base, destination, state):
    """Fetch using local configuration; preserve dirty or divergent branches."""
    if state.get("protocol") != "git-origin-v1":
        raise ValueError("Update both workers to use Git-based resume.")
    branch, head = state["branch"], state["head"]
    git(base, "check-ref-format", "refs/heads/" + branch)
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", head):
        raise ValueError("Invalid resume commit.")
    if remote_identity(base) != state["remote"]:
        raise ValueError("The workers' origin remotes refer to different repositories.")
    # Forced: a remote-tracking ref is only a cache of origin, and a rebased
    # branch must not fail the fetch.
    git(base, "fetch", "--no-tags", "origin", "+refs/heads/" + branch + ":refs/remotes/origin/" + branch)
    fetched = git(base, "rev-parse", "FETCH_HEAD").decode().strip()
    # Origin may be ahead when the owner was clean but behind (nothing to push).
    if not _is_ancestor(base, head, fetched):
        raise ValueError("The remote branch changed during resume. Retry from the owning worker.")
    published = [head] + ([fetched] if fetched != head else [])
    dest = receiving_checkout(base, destination, branch)
    if dest.exists() or dest.is_symlink():
        known = [line.removeprefix("worktree ") for line in git(base, "worktree", "list", "--porcelain").decode().splitlines() if line.startswith("worktree ")]
        if not any(Path(p).resolve() == dest.resolve() for p in known) or dest.is_symlink():
            raise ValueError("A local checkout already exists at the destination; its files were not changed.")
        _branch(dest, branch)
        _require_clean(dest, branch, f"the receiving worker's checkout {dest}")
        _preflight(dest)
        current = git(dest, "rev-parse", "HEAD").decode().strip()
        _require_published(dest, branch, current, head, published, f"the receiving worker's checkout {dest}")
        if _is_ancestor(dest, current, head):
            git(dest, "merge", "--ff-only", "--no-overwrite-ignore", head)
        else:
            # Every local commit is already on origin, so moving the branch
            # strands nothing. Ignored files are still never overwritten.
            git(dest, "checkout", "--no-overwrite-ignore", "-B", branch, head)
    else:
        old = git(base, "for-each-ref", "--format=%(objectname)", "refs/heads/" + branch).decode().strip()
        if old:
            _require_published(base, branch, old, head, published, f"the receiving worker's repository {base}")
            git(base, "update-ref", "refs/heads/" + branch, head, old)
            git(base, "worktree", "add", str(dest), branch)
        else:
            git(base, "worktree", "add", "-b", branch, str(dest), head)
    if fingerprint(dest) != head:
        raise ValueError("The receiving checkout changed during resume; ownership was not moved.")
    git(dest, "branch", "--set-upstream-to=origin/" + branch, branch)
    return str(dest)


# --- Task worker branches between workers -----------------------------------
#
# A Task worker that runs on another worker gets its checkout through origin,
# the same way a handoff does: the parent pushes ``task/<worker id>`` at its
# committed HEAD (never forced), the target fetches it with its own
# credentials, and the parent later removes the ref only while it still points
# at that base. Reasons and errors are fixed text: Git's stderr and remote URLs
# can carry tokens, so they are read for classification and never returned.

TASK_BRANCH_PREFIX = "task/"


def _task_branch(cwd, branch):
    if not branch.startswith(TASK_BRANCH_PREFIX) or _run(cwd, "check-ref-format", "refs/heads/" + branch).returncode:
        raise ValueError("Only task/<worker id> branches move between workers this way.")


def _require_sha(sha):
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha):
        raise ValueError("Invalid task base commit.")


def origin_identity(cwd):
    """``remote_identity`` for task transfer, or None when origin is unusable.

    The identity is a hash of the normalized URL, so it can travel to another
    worker without its credentials.
    """
    try:
        return remote_identity(cwd)
    except ValueError:
        return None


def _remote_tip(cwd, branch):
    """``(tip or None, reachable)`` for ``branch`` on origin, as it is now."""
    listed = _run(cwd, "ls-remote", "origin", "refs/heads/" + branch)
    if listed.returncode:
        return None, False
    fields = listed.stdout.decode(errors="replace").split()
    return (fields[0] if fields else None), True


def _push_refused(stderr, branch):
    """A plain reason for a failed push, classified without echoing stderr."""
    text = stderr.decode(errors="replace").lower()
    if any(mark in text for mark in ("[rejected]", "non-fast-forward", "fetch first", "stale info")):
        return f"Origin's {branch} changed meanwhile; nothing was forced."
    if any(mark in text for mark in ("authentication", "permission", "could not read username", "403",
                                     "access denied", "not allowed", "protected", "hook declined")):
        return f"Origin refused {branch}. Check this worker's Git credentials and push rights for origin."
    return f"Could not update {branch} on origin. Check this worker's Git credentials and its connection to origin."


def publish_task_branch(cwd, branch, sha) -> dict:
    """Push commit ``sha`` to origin as ``branch``, without force.

    Returns ``branch``, ``base`` (the SHA), ``remote`` (origin identity hash),
    ``pushed`` and a plain ``reason`` when it was not pushed. A branch already
    on origin at ``sha`` counts as pushed, so a retry is safe; one at any other
    commit is left alone and reported. Never raises for Git failures.
    """
    _task_branch(cwd, branch)
    _require_sha(sha)
    result: dict = dict(protocol="git-origin-v1", branch=branch, base=sha, remote=None, pushed=False, reason=None)
    remote = origin_identity(cwd)
    if remote is None:
        result["reason"] = ("This project has no usable origin remote on this worker (one fetch and push "
                            "URL for the same repository), so its branch cannot reach another worker.")
        return result
    result["remote"] = remote
    try:
        tip, reachable = _remote_tip(cwd, branch)
        if not reachable:
            result["reason"] = ("Could not reach origin from this worker. Check its Git credentials "
                                "and connection to origin.")
            return result
        if tip == sha:
            result["pushed"] = True
            return result
        if tip is not None:
            result["reason"] = f"Origin already has {branch} at another commit; it was left alone and nothing was forced."
            return result
        # Push this exact commit, not a moving HEAD, and never force.
        pushed = _run(cwd, "push", "origin", sha + ":refs/heads/" + branch)
    except ValueError:  # timed out
        result["reason"] = "Git timed out reaching origin from this worker. Check its connection to origin and retry."
        return result
    if pushed.returncode:
        result["reason"] = _push_refused(pushed.stderr, branch)
        return result
    result["pushed"] = True
    return result


def fetch_task_branch(cwd, branch, sha, remote):
    """Fetch ``branch`` from origin and require its tip to be ``sha``.

    ``remote`` is the parent's origin identity; a different repository here is
    refused, never worked around. Leaves only a remote-tracking ref behind.
    """
    _task_branch(cwd, branch)
    _require_sha(sha)
    local = origin_identity(cwd)
    if local is None:
        raise ValueError("This project has no usable origin remote on this worker, so the task branch cannot be fetched.")
    if local != remote:
        raise ValueError("This worker's origin is a different repository from the calling worker's origin; "
                         "clone the project from the same origin to run tasks here.")
    try:
        fetched = _run(cwd, "fetch", "--no-tags", "origin",
                       "+refs/heads/" + branch + ":refs/remotes/origin/" + branch)
    except ValueError:
        raise ValueError(f"Git timed out fetching {branch} from origin on this worker.") from None
    if fetched.returncode:
        raise ValueError(f"Could not fetch {branch} from origin on this worker. Check its Git credentials "
                         "and connection to origin.")
    tip = _run(cwd, "rev-parse", "--verify", "--quiet", "refs/remotes/origin/" + branch + "^{commit}")
    if tip.returncode or tip.stdout.decode().strip() != sha:
        raise ValueError(f"Origin's {branch} is not at the commit the calling worker sent; refusing to start from it.")
    return sha


def delete_untouched_task_branch(cwd, branch, base) -> dict:
    """Delete ``branch`` on origin only while its tip is still ``base``.

    The delete carries ``base`` as its expected old value, so origin itself
    refuses it if anyone pushed meanwhile: no one's commits are deleted.
    Returns ``deleted`` and a plain ``reason`` when it was left in place.
    """
    _task_branch(cwd, branch)
    _require_sha(base)
    try:
        tip, reachable = _remote_tip(cwd, branch)
        if not reachable:
            return dict(deleted=False, reason="Could not reach origin from this worker; the branch was left in place.")
        if tip is None:
            return dict(deleted=False, reason=f"Origin has no {branch}; there was nothing to delete.")
        if tip != base:
            return dict(deleted=False, reason=f"Origin's {branch} has moved past its base commit; it was left alone.")
        # Compare-and-delete: the lease is the expected old value, checked by
        # origin under its ref lock, not a forced update.
        removed = _run(cwd, "push", "--force-with-lease=refs/heads/" + branch + ":" + base,
                       "origin", ":refs/heads/" + branch)
    except ValueError:
        return dict(deleted=False, reason="Git timed out reaching origin; the branch was left in place.")
    if removed.returncode:
        return dict(deleted=False, reason=_push_refused(removed.stderr, branch) + " The branch was left in place.")
    return dict(deleted=True, reason=None)


# --- Commits back from a Task worker on another worker ----------------------
#
# When the worker finishes, the worker that ran it pushes ``task/<id>`` to
# origin (only with commits past the base, never forced) and the calling
# worker fetches it into a local branch of the same name. Neither side touches
# a working tree, index or current branch, and reasons stay fixed text.

#: Seconds for the push on the worker that ran the task.
RESULT_PUSH_TIMEOUT = 60
#: Seconds for the fetch on the calling worker; it runs while the report is
#: being delivered, so it stays well under the delivery timeout.
RESULT_FETCH_TIMEOUT = 20


def _tip(cwd, ref):
    """The commit ``ref`` names here, or None."""
    found = _run(cwd, "rev-parse", "--verify", "--quiet", ref + "^{commit}")
    sha = found.stdout.decode(errors="replace").strip()
    return sha if not found.returncode and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha) else None


def _contains(cwd, older, newer):
    """``_is_ancestor`` that answers False instead of raising."""
    try:
        return _is_ancestor(cwd, older, newer)
    except ValueError:
        return False


def uncommitted_files(cwd):
    """How many files in the checkout at ``cwd`` hold uncommitted changes, or None."""
    try:
        status = _run(cwd, "status", "--porcelain=v1", "-z", "--untracked-files=all", timeout=30)
    except ValueError:
        return None
    if status.returncode:
        return None
    count, skip = 0, False
    for entry in status.stdout.decode(errors="replace").split("\0"):
        if skip or not entry:
            skip = False
            continue
        # A rename or copy is followed by its source path.
        skip = entry[:1] in "RC"
        count += 1
    return count


def push_task_result(cwd, branch, base, *, timeout=RESULT_PUSH_TIMEOUT) -> dict:
    """Push the commits on local ``branch`` past ``base`` to origin, without force.

    Returns ``branch``, ``base``, ``head`` (the local tip, None when the branch
    is gone), ``commits`` (past ``base``), ``remote`` (origin identity hash),
    ``pushed`` and a plain ``reason`` when commits were not pushed. Nothing is
    pushed without commits. Origin's branch is only ever fast-forwarded: one
    holding a commit this branch lacks (someone else's branch of that name) is
    left alone. Never raises for Git failures.
    """
    _task_branch(cwd, branch)
    _require_sha(base)
    result: dict = dict(branch=branch, base=base, head=None, commits=0, remote=None, pushed=False, reason=None)
    try:
        head = _tip(cwd, "refs/heads/" + branch)
        if head is None:
            result["reason"] = f"The worker's branch {branch} no longer exists on this worker, so nothing was pushed."
            return result
        result["head"] = head
        if head == base:
            return result
        counted = _run(cwd, "rev-list", "--count", head, "--not", base)
        result["commits"] = int(counted.stdout.decode().strip() or 0) if not counted.returncode else 0
        if not _contains(cwd, base, head):
            result["reason"] = (f"{branch} no longer starts at its base commit (it was rebased or reset), "
                                "so it was not pushed and nothing was forced.")
            return result
        remote = origin_identity(cwd)
        if remote is None:
            result["reason"] = ("This project has no usable origin remote on this worker, so the commits "
                                "stay in its checkout here.")
            return result
        result["remote"] = remote
        tip, reachable = _remote_tip(cwd, branch)
        if not reachable:
            result["reason"] = ("Could not reach origin from this worker (offline, or its Git credentials "
                                "were refused), so the commits stay in its checkout here.")
            return result
        if tip == head:
            result["pushed"] = True
            return result
        if tip is not None and not _contains(cwd, tip, head):
            result["reason"] = (f"Origin already has a different {branch} (a commit this branch does not "
                                "contain); it was left alone and nothing was forced.")
            return result
        # This exact commit, never a moving HEAD, and a plain push: origin
        # refuses anything that is not a fast-forward.
        pushed = _run(cwd, "push", "origin", head + ":refs/heads/" + branch, timeout=timeout)
    except ValueError:  # timed out
        result["reason"] = "Git timed out reaching origin from this worker, so the commits stay in its checkout here."
        return result
    if pushed.returncode:
        result["reason"] = _push_refused(pushed.stderr, branch)
        return result
    result["pushed"] = True
    return result


def _checked_out(cwd, branch):
    """True when some worktree of this repository has ``branch`` checked out."""
    listed = _run(cwd, "worktree", "list", "--porcelain")
    if listed.returncode:
        return True  # unknown: treat it as in use and never move it
    return ("branch refs/heads/" + branch) in listed.stdout.decode(errors="replace").splitlines()


def fetch_task_result(cwd, branch, head, remote, *, timeout=RESULT_FETCH_TIMEOUT):
    """Fetch ``branch`` from origin and point the local ``branch`` at ``head``.

    The local branch is created, or fast-forwarded when it is behind ``head``;
    one that holds other commits, or is checked out in any worktree, is left
    alone. Only refs change: no working tree, index or current branch. Raises
    ``ValueError`` with a plain reason; returns ``head``.
    """
    _task_branch(cwd, branch)
    _require_sha(head)
    local = origin_identity(cwd)
    if local is None:
        raise ValueError("This project has no usable origin remote on this worker, so the branch was not fetched.")
    if remote is not None and local != remote:
        raise ValueError("This worker's origin is a different repository from the other worker's origin, "
                         "so the branch was not fetched.")
    try:
        fetched = _run(cwd, "fetch", "--no-tags", "origin",
                       "+refs/heads/" + branch + ":refs/remotes/origin/" + branch, timeout=timeout)
    except ValueError:
        raise ValueError(f"Git timed out fetching {branch} from origin on this worker.") from None
    if fetched.returncode:
        if b"couldn't find remote ref" in fetched.stderr.lower():
            raise ValueError(f"Origin no longer has {branch}, so it could not be fetched here.")
        raise ValueError(f"Could not fetch {branch} from origin on this worker. Check its Git credentials "
                         "and connection to origin.")
    tip = _tip(cwd, "refs/remotes/origin/" + branch)
    if tip is None or not _contains(cwd, head, tip):
        raise ValueError(f"Origin's {branch} does not contain the commit the other worker reported, "
                         "so no local branch was made from it.")
    ref = "refs/heads/" + branch
    old = _tip(cwd, ref)
    if old == head:
        return head
    if _checked_out(cwd, branch):
        raise ValueError(f"{branch} is checked out in a worktree here, so it was left alone; "
                         f"the commits are at origin/{branch}.")
    if old is not None and not _contains(cwd, old, head):
        raise ValueError(f"A different local {branch} already exists here, so it was left alone; "
                         f"the commits are at origin/{branch}.")
    # Compare-and-swap: created only if absent, moved only from ``old``.
    updated = _run(cwd, "update-ref", "-m", "fetch task worker result", ref, head, old or "0" * len(head))
    if updated.returncode:
        raise ValueError(f"{branch} changed here while it was being fetched, so it was left alone; "
                         f"the commits are at origin/{branch}.")
    return head
