"""Build the two UI shells in an unprivileged CI job, before signing.

The signing job only inspects this job's ZIP as data. Never run this builder
in a job that has signing/cloud credentials or a write-enabled checkout.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

CONTROL = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("trusted_source_archive", CONTROL / "source_archive.py")
archive = importlib.util.module_from_spec(spec)
spec.loader.exec_module(archive)


def build(source: Path, revision: str, version: str, output: Path):
    actual = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    if actual != revision or not re.fullmatch(r"[a-f0-9]{40}", revision):
        raise ValueError("UI checkout is not the selected source revision")
    if not version:
        # Read version through trusted control code; never import candidate Python.
        reader = CONTROL / "ci/publication_source.py"
        version = json.loads(subprocess.check_output([
            "python", "-I", str(reader), "version", "--source-root", str(source), "--revision", revision], text=True))
    version = version.removeprefix("v")
    subprocess.run(["git", "diff", "--exit-code", "HEAD", "--", "frontend", "mobile"], cwd=source, check=True)
    with tempfile.TemporaryDirectory(prefix="shuffle-ui-build-") as home:
        env = {"PATH": os.environ["PATH"], "HOME": home, "CI": "true", "LANG": "C.UTF-8",
               "APP_VERSION": version, "GITHUB_SHA": revision, "NPM_CONFIG_USERCONFIG": os.devnull,
               "NPM_CONFIG_CACHE": str(Path(home) / "npm-cache")}
        frontend = source / "frontend"
        subprocess.run(["npm", "ci", "--no-audit", "--no-fund"], cwd=frontend, env=env, check=True)
        # This intentionally uses the existing serial desktop+phone producer;
        # parallel compilation would reintroduce their shared tsconfig race.
        subprocess.run(["npm", "run", "build"], cwd=frontend, env=env, check=True)
    subprocess.run(["git", "diff", "--exit-code", "HEAD", "--", "frontend", "mobile"], cwd=source, check=True)
    return archive.build_ui_bundle(source / "frontend/build", revision, version, output)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--version", default="")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build(args.source_root.resolve(), args.revision, args.version, args.output.resolve())))
