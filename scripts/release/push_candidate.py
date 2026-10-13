"""Push a verified local candidate to unused immutable tags in IMAGE_TAGS.

Refuse existing tags and ambiguous registry errors before writing any tag.
Promotion is a separate digest-only operation, never an implicit semver alias.
"""
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.release.promote_image import IMAGE


def push(tags):
    for tag in tags:
        prefix, separator, name = tag.rpartition(":")
        if not separator or prefix != IMAGE or not re.fullmatch(r"(?:candidate-[A-Za-z0-9_.-]+|sha-[0-9a-f]{40}|[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?)", name):
            raise ValueError("Only immutable candidate/version/full-SHA tags may be pushed")
        result = subprocess.run(["docker", "manifest", "inspect", tag], capture_output=True, text=True)
        if result.returncode == 0:
            raise ValueError(f"Immutable image tag already exists: {tag}; reuse its digest")
        # Authentication, connectivity and registry errors are NOT absence.
        error = result.stderr.lower()
        if not any(message in error for message in ("manifest unknown", "no such manifest", "manifest_unknown")):
            raise ValueError("Cannot establish absence of immutable image tag")
    for tag in tags:
        subprocess.run(["docker", "tag", "ai-shuffle:postgres-candidate", tag], check=True)
        subprocess.run(["docker", "push", tag], check=True)
        subprocess.run(["docker", "manifest", "inspect", tag], check=True, stdout=subprocess.DEVNULL)


if __name__ == "__main__":
    tags = os.environ.get("IMAGE_TAGS", "").splitlines()
    if not tags or any(not tag for tag in tags):
        raise ValueError("IMAGE_TAGS is required")
    push(tags)
