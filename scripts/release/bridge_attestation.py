"""Verify the bridge: --private-export E --public-root S --out FILE.

Requires --private-checkout PATH (or AI_SHUFFLE_PRIVATE_CHECKOUT). --verify FILE
repeats live identity/merged-PR/reach and signed-envelope checks; the JSON is an
audit receipt, NEVER a signature or authorization accepted by the exact-tree gate.
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.ci import edge_publication_gate as gate
from scripts.ci import public_cutover as cutover


def attest(root, export, initial, private_checkout=None):
    try:
        published = gate.published_edge_revision()
    except Exception:
        published = gate.cached_edge_revision()
    envelope = gate.edge_cache_path().read_bytes()
    if gate.verify_edge_manifest(envelope) != published:
        raise ValueError("Verified envelope changed")
    actual_root, actual_export = cutover.verify_bridge(
        root, "HEAD", published, envelope, private_root=private_checkout, walk=gate.update_reach_readiness)
    if (initial, export) != (actual_root, actual_export):
        raise ValueError("Requested E/S do not match verified export/root")
    return {"format": 1, "private_export": export, "public_root": initial,
            "tree": cutover.git(root, "rev-parse", f"{initial}^{{tree}}"),
            "envelope_sha256": cutover.hashlib.sha256(envelope).hexdigest()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private-export")
    parser.add_argument("--public-root")
    parser.add_argument("--private-checkout", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--verify", type=Path)
    args = parser.parse_args()
    if args.verify:
        receipt = json.loads(args.verify.read_text())
        if attest(Path.cwd(), receipt["private_export"], receipt["public_root"], args.private_checkout) != receipt:
            raise ValueError("Bridge receipt no longer matches live evidence")
        print("PASS bridge reverified (not a publication authorization)")
    else:
        if not all((args.private_export, args.public_root, args.out)):
            parser.error("--private-export, --public-root and --out are required")
        receipt = attest(Path.cwd(), args.private_export, args.public_root, args.private_checkout)
        # Exclusive create: never overwrite someone else's evidence.
        with args.out.open("x") as stream:
            stream.write(json.dumps(receipt, indent=2) + "\n")
        print("PASS bridge receipt written (not a publication authorization)")


if __name__ == "__main__":
    main()
