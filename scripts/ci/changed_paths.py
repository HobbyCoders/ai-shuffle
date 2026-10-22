"""Fail-closed PR path classification. No shell interpolation of revisions or paths."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import PurePosixPath
import re
import subprocess

AREAS = ("backend", "frontend", "workflows", "browser", "tools", "desktop")
DOC_SUFFIXES = {".md", ".rst", ".txt", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg"}
ROOT_DOCS = {"README.md", "CONTRIBUTING.md", "CHANGELOG.md", "SECURITY.md"}


def full(reason, paths=()):
    return {**{f"full_{area}": True for area in AREAS}, "paths": sorted(paths), "reason": reason}


def classify(paths):
    paths = sorted(set(paths))
    if not paths:
        return full("empty diff")
    result = {f"full_{area}": False for area in AREAS}
    for path in paths:
        p = PurePosixPath(path)
        if (not path or path.startswith("/") or ".." in p.parts or "." in path.split("/")
                or "\\" in path or str(p) != path):
            return full("malformed path", paths)
        if (p.parts[0] in {"app", "fluid", "docker", "tests", "scripts"}
                or (len(p.parts) == 1 and (
                    path.startswith(("requirements", "Dockerfile", "docker-compose"))
                    or (path.startswith("compose") and p.suffix in {".yml", ".yaml"})
                    or path in {"pyproject.toml", "pytest.ini", "setup.cfg", ".python-version", ".gitleaks.toml"}))):
            return full("backend/shared path", paths)
        if (path in ROOT_DOCS or (
                path.startswith(("docs/", "frontend/src/lib/docs/content/"))
                and not path.startswith("docs/install/") and p.suffix in DOC_SUFFIXES)):
            continue
        if p.parts[0] in {"frontend", "mobile"}:
            result["full_frontend"] = True
        elif len(p.parts) == 3 and path.startswith(".github/workflows/") and p.suffix in {".yml", ".yaml"}:
            result["full_workflows"] = True
        elif p.parts[0] in {"browser-mcp", "tools", "desktop"}:
            result[{"browser-mcp": "full_browser", "tools": "full_tools", "desktop": "full_desktop"}[p.parts[0]]] = True
        else:
            return full("unknown path", paths)
    return {**result, "paths": paths, "reason": "mapped paths"}


def parse_diff(data):
    if not data or not data.endswith(b"\0"):
        raise ValueError("missing or unterminated diff")
    fields = data[:-1].split(b"\0")
    if len(fields) % 2:
        raise ValueError("malformed name-status records")
    records = []
    for status, path in zip(fields[::2], fields[1::2]):
        if status not in {b"A", b"M", b"D"} or not path:
            raise ValueError("unknown status (renames must be represented as delete/add)")
        records.append((status.decode("ascii"), path.decode("utf-8", errors="strict")))
    return records


def git(*args):
    return subprocess.check_output(["git", *args], stderr=subprocess.PIPE)


def tree(revision):
    entries = {}
    for record in git("ls-tree", "-rz", revision).split(b"\0"):
        if record:
            metadata, path = record.split(b"\t", 1)
            mode, kind, _ = metadata.split(b" ")
            entries[path.decode("utf-8", errors="strict")] = (mode, kind)
    return entries


def classify_diff(base, head):
    try:
        if not all(re.fullmatch(r"[0-9a-f]{40}", rev or "") for rev in (base, head)):
            return full("missing/invalid explicit revisions")
        records = parse_diff(git("diff", "--name-status", "-z", "--no-renames", base, head, "--"))
        before, after = tree(base), tree(head)
        paths = [path for _, path in records]
        for status, path in records:
            old, new = before.get(path), after.get(path)
            if ((status != "A" and old is None) or (status != "D" and new is None)
                    or any(entry is not None and entry != (b"100644", b"blob") for entry in (old, new))
                    or (old is not None and new is not None and old != new)):
                return full("mode change, executable, symlink, submodule or missing tree entry", paths)
        return classify(paths)
    except (ValueError, UnicodeError, subprocess.CalledProcessError, OSError):
        return full("unreadable/malformed diff or history")


def check_gate(needs, receipts, head):
    """A skipped matrix is legal ONLY after an explicit successful classifier."""
    required = {"changes", "backend-shard", "backend-contracts", "backend-security"}
    if set(needs) != required or needs["changes"].get("result") != "success":
        raise ValueError("missing/failed classifier or dependency")
    decision = needs["changes"].get("outputs", {}).get("full_backend")
    if decision not in {"true", "false"}:
        raise ValueError("unknown classifier output")
    if needs["backend-security"].get("result") != "success":
        raise ValueError("security did not succeed")
    expected = "success" if decision == "true" else "skipped"
    if any(needs[job].get("result") != expected for job in ("backend-shard", "backend-contracts")):
        raise ValueError("required heavy job failed, cancelled, missing or unexpectedly skipped")
    if decision == "false":
        if receipts:
            raise ValueError("unexpected shard receipts")
        return
    if len(receipts) != 4 or {r["shard"] for r in receipts} != {1, 2, 3, 4}:
        raise ValueError("missing/duplicate shard receipt")
    if (any(r["head"] != head or r["count"] <= 0 or r["workers"] != 2 for r in receipts)
            or len({r["collection"] for r in receipts}) != 1
            or len({r["partition"] for r in receipts}) != 1
            or len({r["total"] for r in receipts}) != 1
            or sum(r["count"] for r in receipts) != receipts[0]["total"]):
        raise ValueError("inconsistent partition receipts")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base")
    parser.add_argument("--head", required=True)
    parser.add_argument("--gate", action="store_true")
    parser.add_argument("--receipts", default="shard-results")
    args = parser.parse_args()
    if args.gate:
        from pathlib import Path
        receipts = [json.loads(p.read_text()) for p in Path(args.receipts).glob("**/receipt.json")]
        check_gate(json.loads(os.environ["BACKEND_NEEDS"]), receipts, args.head)
        print("Backend tests: all required coverage succeeded")
        return
    result = classify_diff(args.base, args.head)
    print(json.dumps(result, indent=2))
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as output:
            for area in AREAS:
                key = f"full_{area}"
                output.write(f"{key}={str(result[key]).lower()}\n")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as output:
            output.write("### Backend path classification\n```json\n" + json.dumps(result, indent=2) + "\n```\n")


if __name__ == "__main__":
    main()
