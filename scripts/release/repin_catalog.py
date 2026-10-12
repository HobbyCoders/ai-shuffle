"""Repin the connector catalog: --public-root SHA [--check].

Run in the fresh public checkout in its first reviewed release PR. Reads only
local public objects; never fetches private ancestors. --check does not write.
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.ci.public_cutover import public_root
from scripts.ci.release_preflight import verify_catalog


def repin(root, revision, check=False):
    if revision != public_root(root):
        raise ValueError("Catalog must be pinned to this public history's root")
    path = root / "app/core/connectors/catalog.json"
    catalog = json.loads(path.read_text())
    catalog["revision"] = revision
    identity = {key: value for key, value in catalog.items() if key != "release"}
    catalog["release"] = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]
    # Verify the producing pinned tree before touching the catalog.
    import subprocess
    for spec in catalog["packages"].values():
        for filename, digest in spec["files"].items():
            if Path(filename).name != filename:
                raise ValueError("Invalid connector filename")
            data = subprocess.check_output(["git", "show", f"{revision}:app/core/connectors/{filename}"], cwd=root)
            if hashlib.sha256(data).hexdigest() != digest:
                raise ValueError(f"Root connector digest mismatch: {filename}")
    if check:
        if json.loads(path.read_text()) != catalog:
            raise ValueError("Catalog needs repinning in a reviewed public PR")
    else:
        path.write_text(json.dumps(catalog, indent=2) + "\n")
    verify_catalog(root)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--public-root", required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    repin(Path.cwd(), args.public_root, args.check)


if __name__ == "__main__":
    main()
