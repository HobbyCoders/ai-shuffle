"""Stage a main-suite UI artifact as data, or pin a version for the normal rebuild.

This is a cache, never publication authorization. The caller must first select an
eligible revision with edge_publication_gate, which the signer rechecks unchanged.
Run with Python -I from the trusted control checkout, without signing credentials.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import zipfile

CONTROL = Path(__file__).resolve().parents[2]


def load(relative):
    spec = importlib.util.spec_from_file_location("ui_cache_" + Path(relative).stem, CONTROL / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gate = load("scripts/ci/edge_publication_gate.py")
source = load("scripts/ci/publication_source.py")
archive = load("scripts/source_archive.py")
MAX_ARTIFACT_SIZE = archive.UI_MAX_SIZE + 1024 * 1024


def artifact_name(revision, attempt):
    return f"source-ui-{revision}-{attempt}"


def find_artifact(api, revision, evidence):
    run_id, attempt = evidence["run_id"], evidence["run_attempt"]
    matches = []
    for page in range(1, 11):
        listing = api(f"actions/runs/{run_id}/artifacts?per_page=100&page={page}")
        items = listing.get("artifacts")
        if not isinstance(items, list):
            raise ValueError("Unexpected artifact response")
        matches.extend(item for item in items if item.get("name") == artifact_name(revision, attempt))
        if len(items) < 100:
            break
    else:
        raise ValueError("Refusing partial artifact evidence")
    if len(matches) != 1:
        return None
    item = matches[0]
    run = item.get("workflow_run") or {}
    repository_id = api("").get("id")
    if (type(item.get("id")) is not int or item["id"] <= 0 or item.get("expired") is not False
            or type(item.get("size_in_bytes")) is not int or not 0 < item["size_in_bytes"] <= MAX_ARTIFACT_SIZE
            or run.get("id") != run_id or run.get("head_sha") != revision or run.get("head_branch") != "main"
            or not repository_id or run.get("repository_id") != repository_id
            or run.get("head_repository_id") != repository_id):
        return None
    return item


def download_artifact(artifact_id, output):
    # Do not execute candidate code or let an extraction tool choose paths.
    with output.open("xb") as stream:
        subprocess.run(["gh", "api", f"repos/{gate.REPOSITORY}/actions/artifacts/{artifact_id}/zip"],
                       stdout=stream, check=True, timeout=60)
    if not 0 < output.stat().st_size <= MAX_ARTIFACT_SIZE:
        raise ValueError("Artifact exceeds download limit")


def unpack_artifact(download, output):
    with zipfile.ZipFile(download) as zipped:
        entries = zipped.infolist()
        if (len(entries) != 1 or entries[0].filename != "ui.zip"
                or not 0 < entries[0].file_size <= archive.UI_MAX_SIZE
                or (entries[0].external_attr >> 16) & 0xf000 not in (0, 0x8000)
                or entries[0].flag_bits & 1):
            raise ValueError("Expected a single bounded UI ZIP")
        with zipped.open(entries[0]) as incoming, output.open("xb") as outgoing:
            shutil.copyfileobj(incoming, outgoing)


def bundle_version(bundle, revision):
    with zipfile.ZipFile(bundle) as zipped:
        stamp = zipped.getinfo(f"ai-shuffle-{revision}/ui-build.json")
        if stamp.file_size > 4096:
            raise ValueError("UI version stamp exceeds limit")
        version = json.loads(zipped.read(stamp))["version"]
    # Reuse the signer's full ZIP contract, including both shells and the stamp.
    archive.describe_ui_bundle(bundle, revision, version)
    return version


def valid_version(root, revision, resolved, pinned):
    # Mirror nightly-version.mjs: a release tag can land after the main build.
    # Only a genuine pre-tag first-parent dev version can differ from today's
    # resolver. An arbitrary artifact's version never becomes signing authority.
    if pinned == resolved:
        return True
    match = re.fullmatch(r"([0-9]+\.[0-9]+\.[0-9]+(?:[.-][A-Za-z0-9.-]+)?)-dev\.([1-9][0-9]*)\+g([0-9a-f]{7,40})", pinned)
    if not match or not revision.startswith(match[3]):
        return False
    tag = f"refs/tags/v{match[1]}"
    try:
        tagged = source.git(root, "rev-parse", "--verify", "--quiet", f"{tag}^{{commit}}")
    except subprocess.CalledProcessError:
        return False
    return (tagged in source.git(root, "rev-list", "--first-parent", revision).splitlines()
            and source.git(root, "rev-list", "--first-parent", "--count", f"{tagged}..{revision}") == match[2])


def prepare(api, root, revision, output, *, download=download_artifact):
    # Also checks full SHA + checkout, using trusted Git-only version resolution.
    resolved = source.read_source("version", root, revision)
    if output.exists():
        raise ValueError("UI staging output must be new")
    result = {"reused": "false", "version": resolved}
    try:
        evidence = gate.hosted_status(api, revision)
        if not evidence:
            return result
        item = find_artifact(api, revision, evidence)
        if not item:
            return result
        with tempfile.TemporaryDirectory(prefix="source-ui-cache-") as temporary:
            temporary = Path(temporary)
            download(item["id"], temporary / "artifact.zip")
            unpack_artifact(temporary / "artifact.zip", temporary / "ui.zip")
            version = bundle_version(temporary / "ui.zip", revision)
            if not valid_version(root, revision, resolved, version):
                raise ValueError("UI artifact version does not match source history")
            # A retry that started during the download invalidates this attempt.
            current = gate.hosted_status(api, revision)
            if not current or any(current[key] != evidence[key] for key in ("run_id", "run_attempt")):
                raise ValueError("UI artifact run changed during verification")
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(temporary / "ui.zip", output)
        return {"reused": "true", "version": version}
    except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile, RuntimeError, subprocess.SubprocessError) as error:
        # Missing/expired artifacts, old workflow revisions, API outages and
        # invalid data only cost the optimization; the gated rebuild still works.
        print(f"UI cache unavailable ({type(error).__name__}); using the source UI build fallback.")
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = prepare(gate.GitHub(), args.source_root.resolve(), args.revision, args.output.resolve())
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as stream:
        for key, value in result.items():
            stream.write(f"{key}={value}\n")


if __name__ == "__main__":
    main()
