"""Select the newest attempt of each downloaded shard, retaining untouched shards.

Download without merge-multiple: attempt-qualified artifact directories must stay
separate until selection. Validators still inspect every selected receipt/TAP.
"""
import argparse
from pathlib import Path
import re
import shutil


def select_evidence(source: Path, destination: Path, prefix: str, count: int):
    pattern = re.compile(re.escape(prefix) + r"-([1-9][0-9]*)-a([1-9][0-9]*)")
    selected = {}
    for path in source.iterdir():
        match = pattern.fullmatch(path.name)
        if not match or not path.is_dir():
            continue
        shard, attempt = map(int, match.groups())
        if shard not in range(1, count + 1):
            raise ValueError(f"unexpected shard: {path.name}")
        if shard not in selected or attempt > selected[shard][0]:
            selected[shard] = (attempt, path)
    missing = set(range(1, count + 1)) - selected.keys()
    if missing:
        raise ValueError(f"missing shard evidence: {sorted(missing)}")
    # Never merge into a pre-existing directory containing stale receipts.
    destination.mkdir(parents=True, exist_ok=False)
    for shard, (attempt, path) in sorted(selected.items()):
        target = destination / f"{prefix}-{shard}"
        shutil.copytree(path, target)
        print(f"Selected {path.name} (attempt {attempt}) -> {target}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--count", type=int, required=True)
    args = parser.parse_args()
    if args.count < 1:
        parser.error("--count must be positive")
    select_evidence(args.source, args.destination, args.prefix, args.count)


if __name__ == "__main__":
    main()
