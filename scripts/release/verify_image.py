"""Verify/reuse an immutable image: --digest sha256:HEX [--checkout PATH].

Pulls from the fixed owner package, never executes the image; copies its baked
provenance from a stopped container. Requires exact revision, complete Git tree,
shared sequence and capability contract. Legacy images without tree fail closed.
"""
import argparse
import json
import tempfile
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.ci.public_cutover import git, container_sequence
from scripts.release.promote_image import IMAGE, inspect_digest


def verify_record(root, record):
    from docker.update_contract import image_record
    revision = git(root, "rev-parse", "HEAD")
    sequence = container_sequence(root)
    expected = image_record(revision, sequence)
    expected["source_tree"] = git(root, "rev-parse", "HEAD^{tree}")
    if record != expected:
        raise ValueError("Image provenance does not match the exact release revision/tree/sequence/contract")


def verify_image(root, digest):
    import re
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError("Invalid image digest")
    ref = f"{IMAGE}@{digest}"
    if inspect_digest(ref) != digest:
        raise ValueError("Registry digest mismatch")
    subprocess.run(["docker", "pull", ref], check=True)
    container = subprocess.check_output(["docker", "create", ref], text=True).strip()
    try:
        with tempfile.TemporaryDirectory(prefix="shuffle-image-proof-") as directory:
            record = Path(directory) / "image.json"
            subprocess.run(["docker", "cp", f"{container}:/app/docker/image.json", str(record)], check=True)
            verify_record(root, json.loads(record.read_text()))
        subprocess.run(["docker", "tag", ref, "ai-shuffle:postgres-candidate"], check=True)
    finally:
        subprocess.run(["docker", "rm", container], check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--digest", required=True)
    parser.add_argument("--checkout", type=Path, default=Path.cwd())
    args = parser.parse_args()
    verify_image(args.checkout, args.digest)
    print("PASS immutable image matches exact release source")


if __name__ == "__main__":
    main()
