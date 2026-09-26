#!/usr/bin/env python3
"""Generate THIRD_PARTY_NOTICES.md at the repo root.

Python packages: the transitive closure of requirements.txt,
requirements-dev.txt, requirements-native.txt and the fluid harness, read from
the installed distributions of the interpreter that runs this script (the same
metadata pip-licenses reads, without the extra tool). Run it with the Python
environment the image or installer is built from, or packages that are not
installed there are listed as unresolved.

npm packages: read from the package-lock.json files of frontend/,
browser-mcp/, tools/ and desktop/. frontend/ lists every locked package
because the bundler can pull code from any of them; the others list
production dependencies only, which is what ships.

Usage: python scripts/generate_third_party_notices.py [--output PATH]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from importlib import metadata
from pathlib import Path

try:
    from packaging.requirements import InvalidRequirement, Requirement
    from packaging.utils import canonicalize_name
except ImportError:  # pip always vendors packaging
    from pip._vendor.packaging.requirements import InvalidRequirement, Requirement  # type: ignore
    from pip._vendor.packaging.utils import canonicalize_name  # type: ignore

ROOT = Path(__file__).resolve().parents[1]
HERE = Path(__file__).resolve().parent / "third_party_notices"
REQUIREMENT_FILES = ["requirements.txt", "requirements-dev.txt", "requirements-native.txt"]
NPM_PROJECTS = [("frontend", True), ("browser-mcp", False), ("tools", False), ("desktop", False)]
# Packages whose metadata names no license, checked by hand against their repositories.
LICENSE_OVERRIDES = {"google-crc32c": "Apache-2.0"}


# ── Python ────────────────────────────────────────────────────────────────


def _requirement_lines(path: Path) -> list[str]:
    lines = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split(" #", 1)[0].strip()
        if line and not line.startswith(("#", "-")):
            lines.append(line)
    return lines


def _root_requirements() -> list[Requirement]:
    roots: list[Requirement] = []
    for name in REQUIREMENT_FILES:
        path = ROOT / name
        if path.exists():
            roots.extend(Requirement(line) for line in _requirement_lines(path))
    # fluid is installed from the tree (plain, as the Dockerfile does); its deps ship.
    roots.append(Requirement("fluid"))
    return roots


def _marker_ok(req: Requirement, extras: set[str]) -> bool:
    if req.marker is None:
        return True
    return any(req.marker.evaluate({"extra": extra}) for extra in (extras or {""}))


def _license_of(dist: metadata.Distribution) -> str:
    meta = dist.metadata
    override = LICENSE_OVERRIDES.get(canonicalize_name(meta["Name"]))
    if override:
        return override
    expression = (meta.get("License-Expression") or "").strip()
    if expression:
        return expression
    classifiers = [
        c.split("::")[-1].strip()
        for c in meta.get_all("Classifier") or []
        if c.startswith("License ::") and c.strip() != "License :: OSI Approved"
    ]
    if classifiers:
        return "; ".join(sorted(set(classifiers)))
    text = (meta.get("License") or "").strip()
    if text and "\n" not in text and len(text) <= 80:
        return text
    return "See package (license text in metadata)" if text else "UNKNOWN"


def _url_of(dist: metadata.Distribution) -> str:
    meta = dist.metadata
    urls = {}
    for entry in meta.get_all("Project-URL") or []:
        label, _, url = entry.partition(",")
        urls[label.strip().lower()] = url.strip()
    for key in ("homepage", "home", "source", "source code", "repository", "github", "code"):
        if urls.get(key):
            return urls[key]
    home = (meta.get("Home-page") or "").strip()
    if home and home.upper() != "UNKNOWN":
        return home
    name = canonicalize_name(meta["Name"])
    return f"https://pypi.org/project/{name}/"


def python_packages() -> tuple[list[tuple[str, str, str, str]], list[str]]:
    seen: dict[str, set[str]] = {}
    rows: dict[str, tuple[str, str, str, str]] = {}
    missing: set[str] = set()
    queue: list[tuple[Requirement, set[str]]] = [(r, set(r.extras)) for r in _root_requirements()]
    while queue:
        req, extras = queue.pop()
        key = canonicalize_name(req.name)
        if key in seen and extras <= seen[key]:
            continue
        seen.setdefault(key, set()).update(extras)
        try:
            dist = metadata.distribution(req.name)
        except metadata.PackageNotFoundError:
            missing.add(req.name)
            continue
        name = dist.metadata["Name"]
        if key != "fluid":  # our own harness, covered by the AI Shuffle license
            rows[key] = (name, dist.version, _license_of(dist), _url_of(dist))
        for spec in dist.requires or []:
            try:
                child = Requirement(spec)
            except InvalidRequirement:
                continue
            if _marker_ok(child, seen[key]):
                queue.append((child, set(child.extras)))
    return sorted(rows.values(), key=lambda r: r[0].lower()), sorted(missing, key=str.lower)


# ── npm ───────────────────────────────────────────────────────────────────


def npm_packages(project: str, include_dev: bool) -> list[tuple[str, str, str, str]]:
    lock = json.loads((ROOT / project / "package-lock.json").read_text(encoding="utf-8"))
    rows = {}
    for path, info in lock.get("packages", {}).items():
        if not path or info.get("link"):
            continue
        if info.get("dev") and not include_dev:
            continue
        name = info.get("name") or path.rsplit("node_modules/", 1)[-1]
        version = info.get("version", "")
        license_ = info.get("license") or "UNKNOWN"
        if isinstance(license_, dict):
            license_ = license_.get("type", "UNKNOWN")
        url = f"https://www.npmjs.com/package/{name}/v/{version}"
        rows[(name, version)] = (name, version, str(license_), url)
    return sorted(rows.values(), key=lambda r: (r[0].lower(), r[1]))


# ── Output ────────────────────────────────────────────────────────────────


def _cell(text: str) -> str:
    return re.sub(r"\s+", " ", text).replace("|", "\\|")


def _table(rows: list[tuple[str, str, str, str]]) -> str:
    out = ["| Package | Version | License | URL |", "| --- | --- | --- | --- |"]
    out += [f"| {_cell(n)} | {_cell(v)} | {_cell(l)} | {_cell(u)} |" for n, v, l, u in rows]
    return "\n".join(out)


def render() -> str:
    header = (HERE / "header.md").read_text(encoding="utf-8")
    t3code = (ROOT / "frontend/src/lib/components/git/t3code.LICENSE").read_text(encoding="utf-8").strip()
    parts = [header.replace("{{T3CODE_LICENSE}}", t3code).rstrip()]

    py_rows, missing = python_packages()
    parts.append(
        "## Python packages\n\n"
        "Installed into the Docker image and the desktop installer's Python runtime "
        "(`requirements.txt`, `requirements-dev.txt`, `requirements-native.txt` and the "
        "fluid harness, with their dependencies). No Claude Code CLI binary is bundled; "
        "the user's separately installed stock CLI is licensed by Anthropic under its "
        "own terms. Platform-specific dependencies "
        f"were resolved for {sys.platform}.\n\n" + _table(py_rows)
    )
    if missing:
        parts.append(
            "Not installed in the environment this file was generated from, so not "
            "listed above: " + ", ".join(f"`{m}`" for m in missing) + "."
        )

    for project, include_dev in NPM_PROJECTS:
        scope = "all locked packages (the build bundles from them)" if include_dev else "production dependencies"
        parts.append(f"## npm packages: {project}/\n\nFrom `{project}/package-lock.json`, {scope}.\n\n"
                     + _table(npm_packages(project, include_dev)))

    apache = (HERE / "Apache-2.0.txt").read_text(encoding="utf-8").strip("\n")
    parts.append("## Apache License 2.0\n\nApplies to cloudflared and tweakcn, among others above.\n\n```\n" + apache + "\n```")
    return "\n\n".join(parts) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", default=str(ROOT / "THIRD_PARTY_NOTICES.md"))
    args = parser.parse_args()
    text = render()
    Path(args.output).write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {args.output} ({text.count(chr(10))} lines)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
