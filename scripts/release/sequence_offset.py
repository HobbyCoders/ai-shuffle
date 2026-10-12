"""Committed epoch sequence: show [--revision SHA], verify, compute --last-shipped N.

compute only prints the offset to commit in release/container-sequence-offset
in the frozen private export PR; it never changes configuration or history.
show/verify read the target commit, never environment/repository variables.
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.ci.public_cutover import container_sequence, mapping
import json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("show", "compute", "verify", "signing-floor"))
    parser.add_argument("--last-shipped", type=int)
    parser.add_argument("--revision", default="HEAD")
    args = parser.parse_args()
    if args.command == "compute":
        if args.last_shipped is None or not 0 < args.last_shipped < 2**53 - 1:
            parser.error("--last-shipped must be a positive safe integer below 2**53-1")
        print(args.last_shipped)
    elif args.command == "signing-floor":
        _, record = mapping(Path.cwd(), args.revision)
        print(json.dumps({"floor": record["maximum_desktop_sequence"] if record else 0,
                          "old_repository_id": record["old_repository_id"] if record else None}))
    else:
        print(container_sequence(Path.cwd(), args.revision))


if __name__ == "__main__":
    main()
