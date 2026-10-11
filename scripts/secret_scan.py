"""Offline, stdlib-only fallback for the reviewed .gitleaks.toml provider rules.

No binary download, network, entropy guessing, or secret values in diagnostics.
The supported config subset fails closed; it is also valid gitleaks config.
"""
from __future__ import annotations

import argparse
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


def tree_files(root: Path):
    paths = git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    for name in sorted(set(paths.split(b"\0")) - {b""}):
        path = name.decode("utf-8", errors="surrogateescape")
        file = root / path
        if file.is_symlink() or not file.exists():
            continue
        yield path, file.read_bytes().decode("utf-8", errors="replace"), "tree"


def commit_files(root: Path, local: str, remote: str):
    # A new ref has no remote tip: scan everything not already on origin/main.
    # If that ref isn't available, scan all reachable history (fail safe).
    git(root, "cat-file", "-e", local + "^{commit}")
    revisions = [local]
    if remote != "0" * 40:
        git(root, "cat-file", "-e", remote + "^{commit}")
        revisions += ["^" + remote]
    else:
        exists = subprocess.run(["git", "rev-parse", "--verify", "origin/main"],
                                cwd=root, capture_output=True, check=False, timeout=15)
        if exists.returncode == 0:
            revisions += ["^origin/main"]
    commits = git(root, "rev-list", *revisions, "--").decode().splitlines()
    for commit in commits:
        # Inspect changed blobs for every commit, including each merge parent.
        # Unlike a final-tree diff this catches add-then-delete leaks, and unlike
        # text patches it handles binary-marked files and quoted paths safely.
        names = git(root, "diff-tree", "--root", "-m", "-r", "--no-commit-id",
                    "--name-only", "--diff-filter=AMT", "--no-renames", "-z", commit, "--")
        for name in sorted(set(names.split(b"\0")) - {b""}):
            path = name.decode("utf-8", "surrogateescape")
            text = git(root, "cat-file", "blob", f"{commit}:{path}").decode("utf-8", "replace")
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
         revision: str | None = None) -> int:
    rules = load_rules(root)
    sources = (revision_files(root, revision) if revision else
               commit_files(root, local, remote or "0" * 40) if local else tree_files(root))
    count = 0
    for path, text, source_revision in sources:
        for rule_id, line in findings(rules, path, text):
            # Never print matching lines, tokens, or hashes of their values.
            metadata = [safe_metadata(rules, value, text) for value in (rule_id, path, source_revision)]
            print(f"secret scan: {metadata[0]}: {metadata[1]}:{line} ({metadata[2]}; value redacted)")
            count += 1
    print(f"Secret scan (offline provider rules): {count} finding(s)")
    return 1 if count else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--local")
    mode.add_argument("--revision", help="scan one committed tree, e.g. origin/main")
    parser.add_argument("--remote")
    args = parser.parse_args()
    try:
        return scan(local=args.local, remote=args.remote, revision=args.revision)
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, re.error, subprocess.TimeoutExpired):
        print("Secret scan failed: cannot read inputs/config; push blocked (details withheld).")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
