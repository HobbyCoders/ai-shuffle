"""Read a candidate as Git data using only the trusted publisher's Python code.

Never import or execute a file from --source-root. Run this entrypoint with
Python -I; its imports and archive allowlist belong to the control checkout.

CLI: python -I <control>/scripts/ci/publication_source.py ACTION
     --source-root <candidate-repository> --revision <full-SHA>
     [--output <absolute-new-archive.zip>]
Every action prints one JSON value:
  version       string: display version from candidate Git tags/history
  archive       object: {sha256, size, tree, files}; requires --output, builds
                that ZIP from candidate Git objects using the control allowlist
  sequence      integer: committed candidate container sequence
  signing-floor object: {floor: integer, old_repository_id: integer|null}
  contract      object: {runtime: string, requires: string[], recommends: string[]}
                Candidate REQUIRES/RECOMMENDS are read as literal AST data;
                the runtime hash algorithm is trusted control code.
  image-record  object: {format: 2, revision: string, sequence: integer,
                runtime: string, provides: string[], source_tree: string}; reads
                candidate PROVIDES literals, committed sequence and Git tree,
                deriving runtime with the trusted image-record algorithm.
No candidate module is imported, including the candidate's archive/sequence code.
The Node publisher selects the same candidate with:
  node <control>/desktop/scripts/publish-source.mjs --source-root <candidate>
SOURCE_REVISION (or GITHUB_SHA fallback) supplies its full revision. Without
--source-root the publisher uses its own control checkout (stable compatibility).
"""
from __future__ import annotations

import argparse
import ast
import importlib.util
import json
from pathlib import Path
import re
import subprocess

CONTROL = Path(__file__).resolve().parents[2]


def load(relative):
    spec = importlib.util.spec_from_file_location("publication_" + Path(relative).stem, CONTROL / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True, timeout=30).strip()


def capability_literals(root, revision, fields):
    # Capability declarations are data. In particular do NOT exec the
    # candidate's update_contract.py (even under an empty globals dictionary).
    tree = ast.parse(git(root, "show", f"{revision}:docker/update_contract.py"))
    values = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in fields:
                    if target.id in values:
                        raise ValueError("Duplicate container capability declaration")
                    names = ast.literal_eval(node.value)
                    if (not isinstance(names, (tuple, list)) or len(names) > 32
                            or any(not isinstance(name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,39}", name) for name in names)):
                        raise ValueError("Invalid container capability declaration")
                    values[target.id] = sorted(set(names))
    if set(values) != set(fields):
        raise ValueError("Candidate needs literal declarations: " + ", ".join(fields))
    return values


def contract(root, revision):
    values = capability_literals(root, revision, ("REQUIRES", "RECOMMENDS"))
    trusted = load("docker/update_contract.py")
    return {"runtime": trusted.runtime(values["REQUIRES"]),
            "requires": values["REQUIRES"], "recommends": values["RECOMMENDS"]}


def read_source(command, root, revision, output=None):
    if not re.fullmatch(r"[a-f0-9]{40}", revision):
        raise ValueError("Expected full source revision")
    if git(root, "rev-parse", "HEAD") != revision:
        raise ValueError("Source checkout does not match revision")
    if command == "archive":
        if output is None:
            raise ValueError("Archive output is required")
        return load("scripts/source_archive.py").build(revision, output, repo=root)
    if command == "contract":
        return contract(root, revision)
    if command == "version":
        # The trusted version resolver's only I/O is this Git reader. Bind it
        # explicitly; no cwd or candidate resolver is involved.
        resolver = load("android/ci/resolve-version-name.py")
        resolver.git = lambda *args: git(root, *args)
        return resolver.version_name()
    cutover = load("scripts/ci/public_cutover.py")
    if command == "sequence":
        return cutover.container_sequence(root, revision)
    if command == "image-record":
        provides = capability_literals(root, revision, ("PROVIDES",))["PROVIDES"]
        trusted = load("docker/update_contract.py")
        # Use only trusted executable logic with candidate literal data.
        trusted.PROVIDES = provides
        record = trusted.image_record(revision, cutover.container_sequence(root, revision))
        record["source_tree"] = git(root, "rev-parse", f"{revision}^{{tree}}")
        return record
    if command == "signing-floor":
        _, record = cutover.mapping(root, revision)
        return {"floor": record["maximum_desktop_sequence"] if record else 0,
                "old_repository_id": record["old_repository_id"] if record else None}
    raise ValueError("Unknown source operation")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("version", "archive", "sequence", "signing-floor", "contract", "image-record"))
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    print(json.dumps(read_source(args.command, args.source_root.resolve(), args.revision, args.output)))


if __name__ == "__main__":
    main()
