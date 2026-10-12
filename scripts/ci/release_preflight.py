"""Read-only release checks. No tags, artifacts, secrets or deployments are created.

Without --tag, check the candidate source tree. --tag also requires a matching
checkout on main; --github checks the protected branch and merged PR.
--remote-catalog reads the private pinned sources using the existing gh login.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REPOSITORY = "HobbyCoders/ai-shuffle"
FINAL = r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
TAG = FINAL + r"(?:-[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?"


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=root, text=True).strip()


def github(route: str):
    return json.loads(subprocess.check_output(["gh", "api", f"repos/{REPOSITORY}/{route}"], text=True))


def newest_final(root: Path, tag: str) -> bool:
    finals = [t for t in git(root, "tag", "--list").splitlines() if re.fullmatch(FINAL, t)]
    return bool(re.fullmatch(FINAL, tag)) and all(
        tuple(map(int, tag[1:].split("."))) >= tuple(map(int, t[1:].split("."))) for t in finals
    )


def verify_catalog(root: Path, remote: bool = False) -> None:
    base = "app/core/connectors"
    catalog = json.loads((root / base / "catalog.json").read_text())
    revision = catalog["revision"]
    if not re.fullmatch(r"[a-f0-9]{40}", revision):
        raise ValueError("Connector catalog must pin a full commit SHA")
    identity = {k: v for k, v in catalog.items() if k != "release"}
    release = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]
    if catalog["release"] != release:
        raise ValueError(f"Connector release identity is stale; expected {release}")
    # No private ancestor fetches: the first release PR repins to the public
    # root (the identical export cannot contain its own commit hash).
    initial = git(root, "rev-list", "--max-parents=0", "HEAD").splitlines()
    if len(initial) == 1 and git(root, "ls-tree", initial[0], "--", "release/public-cutover.json"):
        if revision not in git(root, "rev-list", "HEAD").splitlines():
            raise ValueError("Public releases require catalog repinning to public root S (or a later public commit) in a merged public PR")
    git(root, "cat-file", "-e", f"{revision}^{{commit}}")
    for spec in catalog["packages"].values():
        for filename, digest in spec["files"].items():
            if Path(filename).name != filename:
                raise ValueError("Invalid connector filename")
            sources = {
                "checkout": (root / base / filename).read_bytes(),
                "pinned commit": subprocess.check_output(["git", "show", f"{revision}:{base}/{filename}"], cwd=root),
            }
            if remote:
                sources["GitHub"] = subprocess.check_output([
                    "gh", "api", f"repos/{REPOSITORY}/contents/{base}/{filename}?ref={revision}",
                    "-H", "Accept: application/vnd.github.raw+json",
                ])
            for source, data in sources.items():
                if hashlib.sha256(data).hexdigest() != digest:
                    raise ValueError(f"{filename}: {source} bytes do not match catalog digest")


def verify_versions(root: Path, tag: str | None = None) -> str:
    version = re.search(r'^__version__ = "([^"]+)"', (root / "app/__init__.py").read_text(), re.MULTILINE).group(1)
    if tag and tag.removeprefix("v").split("-", 1)[0] != version:
        raise ValueError(f"Release tag {tag} does not match fallback version {version}")
    for directory in ("frontend", "desktop"):
        package = json.loads((root / directory / "package.json").read_text())
        lock = json.loads((root / directory / "package-lock.json").read_text())
        if (package["version"], lock["version"], lock["packages"][""]["version"]) != (version,) * 3:
            raise ValueError(f"{directory}: package/lock version drift")
    expected = {
        "android/app/build.gradle.kts": f'ANDROID_VERSION_NAME") ?: "{version}"',
        "docs/install/compose/compose.yaml": "${AI_SHUFFLE_VERSION:-" + version + "}",
        "docs/install/compose/env.example": f"AI_SHUFFLE_VERSION={version}\n",
    }
    for filename, needle in expected.items():
        if needle not in (root / filename).read_text():
            raise ValueError(f"{filename}: release version drift")
    page = Path("getting-started/install-docker-compose.md")
    docs = (root / "docs/content" / page).read_text()
    for filename in ("compose.yaml", "env.example"):
        if (root / "docs/install/compose" / filename).read_text().strip() not in docs:
            raise ValueError(f"Buyer docs do not embed {filename}")
    if docs != (root / "frontend/src/lib/docs/content" / page).read_text():
        raise ValueError("Frontend buyer docs drifted from docs/content")
    return version


def verify_tag(root: Path, tag: str) -> str:
    if not re.fullmatch(TAG, tag):
        raise ValueError("Expected a release tag vX.Y.Z or vX.Y.Z-rc.N")
    revision = git(root, "rev-parse", f"refs/tags/{tag}^{{commit}}")
    if git(root, "rev-parse", "HEAD") != revision:
        raise ValueError("Release tag does not match checkout HEAD")
    result = subprocess.run(["git", "merge-base", "--is-ancestor", revision, "refs/remotes/origin/main"], cwd=root, check=False)
    if result.returncode:
        raise ValueError("Release commit is not on origin/main; fetch main and merge the reviewed release PR first")
    return revision


def verify_merged_pr(revision: str) -> None:
    if not github("branches/main")["protected"]:
        raise ValueError("main is not protected; configure the release ruleset first")
    prs = github(f"commits/{revision}/pulls?per_page=100")
    if not isinstance(prs, list) or len(prs) >= 100:
        raise ValueError("Incomplete merged PR evidence")
    if not any(pr["merged_at"] and pr["base"]["ref"] == "main"
               and pr["base"].get("repo", {}).get("full_name") == REPOSITORY
               and pr["merge_commit_sha"] == revision for pr in prs):
        raise ValueError("Release commit must be a merged pull request on main; the initial public root is not a stable release candidate")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag")
    parser.add_argument("--github", action="store_true")
    parser.add_argument("--remote-catalog", action="store_true")
    args = parser.parse_args()
    if args.github and not args.tag:
        parser.error("--github requires --tag")
    revision = verify_tag(ROOT, args.tag) if args.tag else None
    version = verify_versions(ROOT, args.tag)
    verify_catalog(ROOT, args.remote_catalog)
    if args.github:
        verify_merged_pr(revision)
    print(f"Release source checks passed for {version}")
    if args.tag:
        print(f"promote_latest={str(newest_final(ROOT, args.tag)).lower()}")
    else:
        print("No tag or GitHub release gates checked; this is not approval to publish.")


if __name__ == "__main__":
    main()
