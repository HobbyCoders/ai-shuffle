"""Prepare frozen export metadata: --export-pr N --last-shipped M.

Run in the OLD checkout, after disabling/draining publishers and privately
inventorying every distributed/withdrawn image/source sequence. --last-shipped
is that verified maximum, NOT a value inferred from the current feed. Writes
only the mapping/offset; review and merge them in the numbered export PR. Never
publish the export. No E/S hash is embedded (their trees must remain identical).
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.ci import public_cutover as c
from scripts.ci import edge_publication_gate as gate


def prepare(root, export_pr, maximum, desktop_maximum):
    if type(maximum) is not int or not 0 < maximum < 2**53-1 or export_pr <= 0:
        raise ValueError("A positive inventory maximum and export PR are required")
    if type(desktop_maximum) is not int or not 0 < desktop_maximum < 2**53:
        raise ValueError("The signed desktop/installer inventory maximum is required")
    if c.git(root,"status","--porcelain"):
        raise ValueError("Prepare on a clean export branch")
    if c.mapping(root)[1] is not None:
        raise ValueError("Cannot prepare a second public epoch")
    metadata=c.api(c.NEW_REPO)
    if metadata.get("private") is not True or metadata.get("full_name") != c.NEW_REPO:
        raise ValueError("Prepare before the private repository is renamed")
    pr=c.api(c.NEW_REPO,f"pulls/{export_pr}")
    if pr.get("merged_at") or pr.get("base",{}).get("ref")!="main":
        raise ValueError("Open the final export PR against private main first")
    published=gate.published_edge_revision()
    envelope=gate.edge_cache_path().read_bytes()
    if gate.verify_edge_manifest(envelope)!=published:
        raise ValueError("Signed baseline changed")
    payload=json.loads(__import__('base64').b64decode(json.loads(envelope)["payload"]))
    if desktop_maximum < payload["sequence"]:
        raise ValueError("Desktop inventory maximum is below the verified live feed")
    if maximum < payload["container"]["sequence"]:
        raise ValueError("Inventory maximum is below the verified live feed")
    record={"format":1,"old_repository":c.OLD_REPO,"new_repository":c.NEW_REPO,
            "old_repository_id":metadata["id"],"export_pr":export_pr,
            "maximum_shipped_sequence":maximum,"maximum_desktop_sequence":desktop_maximum,"published_revision":published,
            "envelope_sha256":hashlib.sha256(envelope).hexdigest()}
    path=root/c.MAPPING
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open("x") as stream:stream.write(json.dumps(record,indent=2)+"\n")
    (root/c.OFFSET).write_text(str(maximum)+"\n")
    return record


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--export-pr",type=int,required=True)
    parser.add_argument("--last-shipped",type=int,required=True)
    parser.add_argument("--last-desktop-sequence",type=int,required=True)
    args=parser.parse_args()
    prepare(Path.cwd(),args.export_pr,args.last_shipped,args.last_desktop_sequence)
    print("Prepared export mapping/offset; review and merge the export PR, NEVER publish it")


if __name__=="__main__":main()
