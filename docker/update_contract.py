"""Immutable image/application compatibility contract (also used by publishing)."""
import hashlib
import json
from pathlib import Path
import sys

FILES = (
    "Dockerfile", "entrypoint.sh", "docker/container_runtime.py",
    "docker/application_updates.py", "docker/update_contract.py",
    "docker/start-chrome.sh", "docker/chrome-supervisor.sh", "docker/chrome-doctor.sh",
)


def fingerprint(root):
    digest = hashlib.sha256()
    for name in FILES:
        digest.update(name.encode() + b"\0" + (Path(root) / name).read_bytes() + b"\0")
    return digest.hexdigest()


if __name__ == "__main__":
    if len(sys.argv) == 1:
        print(fingerprint(Path(__file__).resolve().parents[1]))
    else:
        root, revision, sequence = sys.argv[1:]
        print(json.dumps({"format": 1, "revision": revision, "sequence": int(sequence),
                          "runtime": fingerprint(root)}))
