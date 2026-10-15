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
                Candidate REQUIRES/RECOMMENDS and optional OVERLAYS are bounded
                literal AST data. Validated overlays add runtime-overlays-v1 to
                requires before trusted control code derives the runtime hash.
  image-record  object: {format: 2, revision: string, sequence: integer,
                runtime: string, provides: string[], source_tree: string}; reads
                candidate PROVIDES literals, committed sequence and Git tree,
                deriving runtime with the trusted image-record algorithm. Includes
                validated candidate overlays when present (not baked capabilities).
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
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit
import re
import subprocess

CONTROL = Path(__file__).resolve().parents[2]
MAX_CAPABILITY_BYTES = 256 * 1024
MAX_CAPABILITY_AST_NODES = 10000


def load(relative):
    spec = importlib.util.spec_from_file_location("publication_" + Path(relative).stem, CONTROL / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True, timeout=30).strip()


def capability_source(root, revision):
    # Resolve once: the size check and read must refer to the same immutable
    # blob, not a symbolic ref which could move between the two Git calls.
    blob = git(root, "rev-parse", f"{revision}:docker/update_contract.py")
    size = int(git(root, "cat-file", "-s", blob))
    if not 0 <= size <= MAX_CAPABILITY_BYTES:
        raise ValueError("Candidate capability module exceeds the 256 KiB size limit")
    raw = subprocess.check_output(["git", "-C", str(root), "cat-file", "blob", blob], timeout=30)
    if len(raw) != size or len(raw) > MAX_CAPABILITY_BYTES:
        raise ValueError("Candidate capability blob size changed or exceeds the 256 KiB limit")
    return raw


def capability_literals(root, revision, fields, *, include_overlays=False):
    # Capability declarations are data. In particular do NOT exec the
    # candidate's update_contract.py (even under an empty globals dictionary).
    raw = capability_source(root, revision)
    try:
        tree = ast.parse(raw)
    except (SyntaxError, RecursionError) as error:
        raise ValueError("Candidate capability module has invalid or excessively nested syntax") from error
    for count, _ in enumerate(ast.walk(tree), start=1):
        if count > MAX_CAPABILITY_AST_NODES:
            raise ValueError("Candidate capability module exceeds the AST node limit")
    values = {}
    wanted = set(fields) | ({"OVERLAYS"} if include_overlays else set())
    for node in tree.body:
        if isinstance(node, (ast.AnnAssign, ast.AugAssign)) and isinstance(node.target, ast.Name) and node.target.id in wanted:
            raise ValueError("Candidate needs a plain literal capability assignment")
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in wanted:
                    if target.id in values:
                        raise ValueError("Duplicate container capability declaration")
                    names = ast.literal_eval(node.value)
                    if target.id == "OVERLAYS":
                        values[target.id] = names
                        continue
                    if (not isinstance(names, (tuple, list)) or len(names) > 32
                            or any(not isinstance(name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,39}", name) for name in names)):
                        raise ValueError("Invalid container capability declaration")
                    values[target.id] = sorted(set(names))
    if not set(fields) <= set(values):
        raise ValueError("Candidate needs literal declarations: " + ", ".join(fields))
    if include_overlays:
        values["OVERLAYS"] = validate_overlays(values.get("OVERLAYS", ()), values.get("REQUIRES", []))
    return values


def validate_overlays(overlays, requires):
    """Stdlib-only trust boundary; do not import the candidate or its updater."""
    if not isinstance(overlays, (tuple, list)) or len(overlays) > 16:
        raise ValueError("Invalid overlay declaration count/type")
    required = {"kind", "capability", "platform", "url", "sha256", "size", "unpacked_size", "executable"}
    seen = set()
    for item in overlays:
        if not isinstance(item, dict) or not required <= item.keys() or item.keys() - required - {"library_path"}:
            raise ValueError("Invalid overlay fields")
        if item["kind"] not in ("python", "node", "chromium", "tools"):
            raise ValueError("Invalid overlay runtime kind")
        capability = item["capability"]
        if (not isinstance(capability, str) or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,39}", capability)
                or not capability.startswith(item["kind"] + "-") or capability not in requires):
            raise ValueError("Overlay must provide a required namespaced capability")
        if item["platform"] not in ("linux-x86_64", "linux-aarch64"):
            raise ValueError("Invalid overlay architecture")
        identity = (item["platform"], item["kind"])
        if identity in seen:
            raise ValueError("Duplicate overlay runtime/architecture")
        seen.add(identity)
        if not isinstance(item["sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", item["sha256"]):
            raise ValueError("Invalid overlay SHA-256")
        for name, maximum in (("size", 512 * 1024**2), ("unpacked_size", 2 * 1024**3)):
            if type(item[name]) is not int or not 0 < item[name] <= maximum:
                raise ValueError("Invalid overlay size bound")
        url = item["url"]
        if not isinstance(url, str) or not 0 < len(url) <= 2048 or any(ord(c) <= 32 for c in url):
            raise ValueError("Invalid overlay URL")
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment
                or (parsed.port is not None and parsed.port == 0)):
            raise ValueError("Overlay URL must be credential-free HTTPS with a valid port")
        for field in ("executable", "library_path"):
            if field not in item:
                continue
            value = item[field]
            if (not isinstance(value, str) or not 0 < len(value) <= 1024 or "\\" in value or "\0" in value
                    or PurePosixPath(value).is_absolute() or ".." in PurePosixPath(value).parts
                    or str(PurePosixPath(value)) == "."):
                raise ValueError("Invalid overlay relative path")
    return list(overlays)


def contract_fields(values):
    requires = sorted(set(values["REQUIRES"]) | ({"runtime-overlays-v1"} if values["OVERLAYS"] else set()))
    if len(requires) > 32:
        raise ValueError("Overlay bootstrap requirement exceeds capability count bound")
    trusted = load("docker/update_contract.py")
    result = {"runtime": trusted.runtime(requires), "requires": requires, "recommends": values["RECOMMENDS"]}
    if values["OVERLAYS"]:
        result["overlays"] = values["OVERLAYS"]
    return result


def contract(root, revision):
    values = capability_literals(root, revision, ("REQUIRES", "RECOMMENDS"), include_overlays=True)
    return contract_fields(values)


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
        values = capability_literals(root, revision, ("PROVIDES", "REQUIRES", "RECOMMENDS"), include_overlays=True)
        contract_fields(values)  # apply the same validation and bootstrap count bound
        provides = values["PROVIDES"]
        trusted = load("docker/update_contract.py")
        # Use only trusted executable logic with candidate literal data.
        trusted.PROVIDES = provides
        trusted.OVERLAYS = values["OVERLAYS"]
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
