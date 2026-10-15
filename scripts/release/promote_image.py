"""Explicit digest-only Docker promotion: --digest sha256:HEX --tags latest,1,1.0.

Run on the reviewed release tag after the candidate's lifecycle/buyer checks and
owner canary acceptance. Never builds an image. Rechecks release PR and newest
final tag, binds the version tag to the supplied digest, then reads aliases back.
For an accepted nightly candidate, use --digest sha256:HEX --tags nightly on
clean public main. This separate lane verifies image provenance and the owner's
exact-main full-suite attestation; it never moves stable aliases. Supplying the
digest is the operator's explicit canary-acceptance decision, not a build request.
Credentials come from Docker/gh's existing login, never command arguments.
"""
import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.ci import release_preflight as checks
from scripts.ci.public_cutover import git

IMAGE = "ghcr.io/hobbycoders/ai-shuffle-server"


def inspect_digest(ref):
    value = subprocess.check_output(["docker", "buildx", "imagetools", "inspect", ref,
                                     "--format", "{{.Manifest.Digest}}"], text=True).strip()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
        raise ValueError("Registry did not return an image digest")
    return value


def promote(root, digest, tags):
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError("A complete sha256 image digest is required")
    if "nightly" in tags:
        if tags != ["nightly"]:
            raise ValueError("Nightly promotion cannot move stable aliases")
        promote_nightly(root, digest)
        return
    tag = git(root, "describe", "--tags", "--exact-match", "HEAD")
    if not re.fullmatch(checks.FINAL, tag):
        raise ValueError("Promotion requires a final release checkout")
    revision = checks.verify_tag(root, tag)
    checks.verify_merged_pr(revision)
    checks.verify_catalog(root, remote=True)
    if not checks.newest_final(root, tag):
        raise ValueError("Only the newest final release can move aliases; fetch all public tags first")
    parts = tag[1:].split(".")
    allowed = {"latest", parts[0], ".".join(parts[:2])}
    if not tags or len(set(tags)) != len(tags) or not set(tags) <= allowed:
        raise ValueError("Only latest and this release's major/minor aliases can be promoted")
    if inspect_digest(f"{IMAGE}:{tag[1:]}") != digest:
        raise ValueError("Version tag does not match the candidate digest")
    move_aliases(digest, tags)


def promote_nightly(root, digest):
    from scripts.ci import edge_publication_gate as gate
    from scripts.release.verify_image import verify_image

    if git(root, "status", "--porcelain"):
        raise ValueError("Nightly promotion requires a clean checkout")
    revision = git(root, "rev-parse", "HEAD")
    if revision != git(root, "rev-parse", "origin/main"):
        raise ValueError("Nightly promotion requires the exact public main checkout")
    # A release label or SHA tag alone cannot establish the candidate's source.
    verify_image(root, digest)
    # Reuse the exact owner/status/current-protected-main gate, not a new notion
    # of green. Recheck after the image pull, immediately before moving nightly.
    ready, _ = gate.exact_main_readiness(gate.GitHub(), revision)
    if not ready:
        raise ValueError("Nightly promotion requires a successful owner exact-main attestation")
    move_aliases(digest, ["nightly"])


def move_aliases(digest, tags):
    subprocess.run(["docker", "buildx", "imagetools", "create", "--prefer-index=false",
                    *[part for alias in tags for part in ("--tag", f"{IMAGE}:{alias}")], f"{IMAGE}@{digest}"], check=True)
    for alias in tags:
        if inspect_digest(f"{IMAGE}:{alias}") != digest:
            raise ValueError(f"Promotion read-back failed: {alias}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--digest", required=True)
    parser.add_argument("--tags", required=True)
    args = parser.parse_args()
    # Refresh only public main/tags before checking newest-final chronology.
    subprocess.run(["git", "fetch", "origin", "main", "--tags"], check=True)
    promote(Path.cwd(), args.digest, args.tags.split(","))
    print("PASS promoted aliases point to the verified candidate digest")


if __name__ == "__main__":
    main()
