"""Offline, stdlib-only fallback for the reviewed .gitleaks.toml provider rules.

No binary download, network, entropy guessing, or secret values in diagnostics.
The supported config subset fails closed; it is also valid gitleaks config.

A push scan (--local) also refuses the maintainer's own setup details (machine
names, addresses, local paths): the repository is public, so a pushed commit
publishes them even if it never reaches main.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def git(root: Path, *args: str) -> bytes:
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, check=False, timeout=15,
                            env={**os.environ, "GIT_NO_LAZY_FETCH": "1", "GIT_TERMINAL_PROMPT": "0"})
    if result.returncode:
        raise RuntimeError("secret scan: git operation failed (output withheld)")
    return result.stdout


def load_rules(root: Path):
    config = tomllib.loads((root / ".gitleaks.toml").read_text(encoding="utf-8"))
    if set(config) - {"title", "rules"} or not config.get("rules"):
        raise ValueError("unsupported secret scan config")
    rules = []
    for rule in config["rules"]:
        if set(rule) - {"id", "description", "regex", "allowlists"}:
            raise ValueError("unsupported secret scan rule")
        allows = []
        for allow in rule.get("allowlists", []):
            if (set(allow) - {"description", "condition", "regexTarget", "paths", "regexes"}
                    or allow.get("condition") != "AND" or allow.get("regexTarget") != "match"
                    or not allow.get("paths") or not allow.get("regexes")):
                raise ValueError("allowlists must scope both paths and exact matched fixtures")
            allows.append(([re.compile(p) for p in allow["paths"]],
                           [re.compile(r) for r in allow["regexes"]]))
        rules.append((rule["id"], re.compile(rule["regex"]), allows))
    return rules


def findings(rules, path: str, text: str):
    for rule_id, pattern, allows in rules:
        for match in pattern.finditer(text):
            if any(any(p.search(path) for p in paths) and
                   any(r.fullmatch(match.group()) for r in regexes) for paths, regexes in allows):
                continue
            yield rule_id, text.count("\n", 0, match.start()) + 1


# The installed pre-push hook refuses a checkout without this line: a branch
# that predates the owner-detail scan would otherwise push unchecked.
OWNER_DETAIL_GATE = 1
# Hash the original values, not substitutes: the guard must catch a real leak
# without publishing the sensitive strings itself.
OWNER_DETAIL_HASHES = frozenset({
    "8be0b5445fca6eb16d14c6f4eaff1cf1d1a3362ef660bfb2b66e74bf309b7e61",  # machine-one
    "6baff1894abb437cfa6086c13670ed2dd5ecfb8178a36f1b8b42fc7c6f588912",  # machine-two
    "8e08b724734fcbc0af03e2469bbe556b37c8330296e4a4a7736a0572bf39522e",  # private-address
    "167ed9eb3a83f4f32378c4b195f745ee5c16116f74d7cc8afe5a09a1d77f219f",  # tailnet-address
    "660bd4b3315cfd8a19cf6050f83741778bd1ee5b9fb25e9c7907c92cad322986",  # home-folder
    "4be591bebbdaca82fab8546a25738a9c9d4e3a6b02fe55bfe2d3636ef455da3b",  # tailnet-name-1
    "b3abbaeb6353ff23d26dbe1dd39f47933d15f686d8d82ca5fe84c9ef0b99220a",  # tailnet-name-2
    "c23fbc9537dd5bb88eef798b887c4948c17c349f729583146d9883fdfd21f634",  # research-artifact-1
    "87274b4f9f72acc805c05b3ced451499c0aa1d795c399112f2c0cbac55b43bb9",  # research-branch-1
    "a68c1f4657a0416721a8148a20443e0a70c383344b91bd4772a96b49d3d51f21",  # research-worktree-1
    "61e1affb401d8062e7732e625e1ca1c8c5937a383681b4ce3baa576bd0eac126",  # research-memory-path-1
    "ed79ee2278242f7851224d74a17df3b97d6dcab6e1db5bd87289a41ea8d800c6",  # research-artifact-2
    "95119a49a78b052307fa0511a5386deb6409bbcb4850571268243a1e298c00b9",  # research-checkout-1
    "9038006871154a43afa40dafaaac8edb4d0f919a2e4a6c57b88d0e2c7f8d4a10",  # research-branch-2
    "003dcea77b63d29547399dac003a401a27fe26235d87a6c405205b47016cc42e",  # research-worktree-2
    "83da9489f41a8387e4504781d8948da4356fc07b6eb9b931100192e882d877d3",  # credential-location-command-1
    "ecb1bdcd5b485a522660951ad0fee754e45566f74e996046bbff0ff8e3a0f199",  # research-checkout-2
    "6a5a14cb2b2845c24b94719b2b91b8940be56590c40a719bcdee59629f14603c",  # credential-location-command-2
})
OWNER_DETAIL_PATTERNS = tuple(re.compile(pattern, re.I) for pattern in (
    r"\b\w+\b",  # worker words, also inside URLs and hyphenated names
    r"\b(?:\d{1,3}\\?\.){3}\d{1,3}\b",  # IPv4, including regex literals
    r"\b(?=([\w-]+(?:\.[\w-]+)*\.ts\.net)\b)",  # overlapping hostname suffixes
    r"/home/[\w.-]+",  # the home directory prefix, including in a longer path
    r"(?:https?://|[A-Z]:[\\/]|~[/\\]|\.claude/)[^\s`<>\"()]+",  # URLs and local paths
    r"\b[\w.-]+(?:/[\w.-]+)+",  # branch and worktree path tokens
    r"`([^`\n]+)`",  # code-quoted setup commands, including embedded spaces
))


def owner_details(text: str):
    """Yield the (start, end) of each owner-specific setup detail in ``text``."""
    for pattern in OWNER_DETAIL_PATTERNS:
        for match in pattern.finditer(text):
            group = match.lastindex or 0  # the captured value where a pattern has one
            token = match.group(group).replace(r"\.", ".").rstrip(".,;:").casefold()
            if hashlib.sha256(token.encode()).hexdigest() in OWNER_DETAIL_HASHES:
                yield match.span(group)


def without_owner_details(value: str) -> str:
    parts, position = [], 0
    for start, end in sorted(set(owner_details(value))):  # matches can overlap
        if start >= position:
            parts += [value[position:start], "[REDACTED]"]
        position = max(position, end)
    return "".join(parts) + value[position:]


def tree_files(root: Path):
    paths = git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    for name in sorted(set(paths.split(b"\0")) - {b""}):
        path = name.decode("utf-8", errors="surrogateescape")
        file = root / path
        if file.is_symlink() or not file.exists():
            continue
        yield path, file.read_bytes().decode("utf-8", errors="replace"), "tree"


def new_ref_boundary(root: Path, push_remote: str | None) -> str | None:
    """The ref whose history a new branch's scan may skip, or None for all of it.

    A new ref has no remote tip, so the scan skips what the push target's main
    already holds. The hook names that target (git's first pre-push argument):
    in a clone whose origin is another repository, origin/main says nothing
    about what the target has, and excluding it left the target's whole
    history to scan. Only main counts: a stale tracking ref of a rewritten or
    deleted branch could hide commits the remote no longer has. A named target
    without that ref (not fetched, or a URL) scans all reachable history (fail
    safe). An older hook names no target and keeps the origin/main boundary.
    This assumes a fresh fetch: an unfetched force-push that rewinds main can
    hide commits the remote no longer has, as with the old origin/main boundary.
    """
    if push_remote and push_remote not in git(root, "remote").decode("utf-8").splitlines():
        return None  # URLs, paths and unknown names must not select a tracking ref.
    ref = "origin/main" if not push_remote else f"refs/remotes/{push_remote}/main"
    exists = subprocess.run(["git", "rev-parse", "--verify", "--quiet", ref + "^{commit}"],
                            cwd=root, capture_output=True, check=False, timeout=15)
    return ref if exists.returncode == 0 else None


COMMIT_OBJECT = "(commit message and author)"  # not a path: no allowlist can match it
TAG_OBJECT = "(tag message and tagger)"


def commit_files(root: Path, local: str, remote: str, push_remote: str | None = None):
    git(root, "cat-file", "-e", local + "^{commit}")
    # An annotated tag is an object of its own, published with the push even
    # when every commit under it is already on the remote.
    tag = local
    while git(root, "cat-file", "-t", tag).strip() == b"tag":
        text = git(root, "cat-file", "tag", tag).decode("utf-8", "replace")
        yield TAG_OBJECT, text, tag[:12]
        tag = text.split(None, 2)[1]  # a tag object starts "object <id>"
    revisions = [local]
    if remote != "0" * 40:
        git(root, "cat-file", "-e", remote + "^{commit}")
        revisions += ["^" + remote]
    else:
        boundary = new_ref_boundary(root, push_remote)
        if boundary is not None:
            revisions += ["^" + boundary]
        else:
            print("Secret scan: scanning all reachable history; fetch the push remote's main first "
                  "(git fetch <remote> main) to establish a boundary. For a URL/path push, "
                  "configure and fetch a named remote, then push to that name.", flush=True)
    commits = git(root, "rev-list", *revisions, "--").decode().splitlines()
    scope = f"not on {revisions[1][1:]}" if len(revisions) > 1 else "all reachable history"
    print(f"Secret scan: {len(commits)} outgoing commit(s), {scope}", flush=True)
    for commit in commits:
        # The commit object too: its message is published with the push, and
        # an unset Git identity falls back to the machine's own host name.
        yield COMMIT_OBJECT, git(root, "cat-file", "commit", commit).decode("utf-8", "replace"), commit[:12]
        # Inspect changed blobs for every commit, including each merge parent.
        # Unlike a final-tree diff this catches add-then-delete leaks, and unlike
        # text patches it handles binary-marked files and quoted paths safely.
        changes = git(root, "diff-tree", "--root", "-m", "-r", "--no-commit-id",
                      "--raw", "--diff-filter=AMT", "--no-renames", "-z", commit, "--").split(b"\0")
        # Gitlinks point at submodule commits, not blobs in this repository:
        # only their names are published from here.
        names = {changes[i + 1]: changes[i].split()[1] == b"160000" for i in range(0, len(changes) - 1, 2)}
        for name, gitlink in sorted(names.items()):
            path = name.decode("utf-8", "surrogateescape")
            text = "" if gitlink else git(root, "cat-file", "blob", f"{commit}:{path}").decode("utf-8", "replace")
            yield path, text, commit[:12]


def revision_files(root: Path, revision: str):
    for entry in git(root, "ls-tree", "-r", "-z", revision).split(b"\0"):
        if not entry:
            continue
        metadata, name = entry.split(b"\t", 1)
        _, kind, oid = metadata.split()
        if kind == b"blob":
            yield name.decode("utf-8", "surrogateescape"), git(root, "cat-file", "blob", oid.decode()).decode("utf-8", "replace"), revision


def safe_metadata(rules, value: str, text: str) -> str:
    # Ignore fixture allowlists here: even an allowed fixture must not leak in
    # diagnostics. Capture groups cover values in context-sensitive rules.
    for _, pattern, _ in rules:
        for match in pattern.finditer(text):
            for token in (match.group(), *match.groups()):
                if token:
                    value = value.replace(token, "[REDACTED]")
        value = pattern.sub("[REDACTED]", value)
    return json.dumps(value, ensure_ascii=True)


def scan(root: Path = ROOT, local: str | None = None, remote: str | None = None,
         revision: str | None = None, push_remote: str | None = None) -> int:
    rules = load_rules(root)
    sources = (revision_files(root, revision) if revision else
               commit_files(root, local, remote or "0" * 40, push_remote) if local else tree_files(root))
    count = owner_count = 0
    for path, text, source_revision in sources:
        for rule_id, line in findings(rules, path, text):
            # Never print matching lines, tokens, or hashes of their values,
            # nor an owner detail in the file's own name.
            metadata = [safe_metadata(rules, without_owner_details(value), text)
                        for value in (rule_id, path, source_revision)]
            print(f"secret scan: {metadata[0]}: {metadata[1]}:{line} ({metadata[2]}; value redacted)")
            count += 1
        if not local:
            # Outgoing commits only: they are what a push publishes. The whole
            # tree takes about 15 seconds and tests/test_public_release_files.py
            # checks it in CI.
            continue
        lines = sorted({text.count("\n", 0, start) + 1 for start, _ in owner_details(text)})
        places = [("lines " if len(lines) > 1 else "line ") + ", ".join(map(str, lines))] if lines else []
        if any(owner_details(path)):
            places.append("file name")
        if places:
            # As above: where it is, never the value or a hash of it.
            shown = safe_metadata(rules, without_owner_details(path), text)
            print(f"owner detail: {shown}: {' and '.join(places)} ({source_revision}; value withheld)")
            owner_count += len(lines) + (len(places) - bool(lines))
    print(f"Secret scan (offline provider rules): {count} finding(s)")
    if local:
        print(f"Owner detail scan (machine names, addresses, local paths): {owner_count} finding(s)")
        if owner_count:
            print("This repository is public. Use a generic example instead, and remove the detail from "
                  "every outgoing commit: a later commit that deletes it still publishes the earlier one.")
    return 1 if count or owner_count else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--local")
    mode.add_argument("--revision", help="scan one committed tree, e.g. origin/main")
    parser.add_argument("--remote")
    parser.add_argument("--push-remote", help="the remote being pushed to (pre-push hook $1)")
    args = parser.parse_args()
    try:
        return scan(local=args.local, remote=args.remote, revision=args.revision,
                    push_remote=args.push_remote)
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, re.error, subprocess.TimeoutExpired):
        print("Secret scan failed: cannot read inputs/config; push blocked (details withheld).")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
