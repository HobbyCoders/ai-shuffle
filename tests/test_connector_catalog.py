"""The connector catalog's digests match the sources in the tree.

Installs verify every connector file against catalog.json, so an edit to a
connector without a matching digest update makes the package integrity check
refuse the file. This catches the drift before it ships.
"""
import hashlib
import json
from pathlib import Path

CONNECTORS = Path(__file__).resolve().parents[1] / "app" / "core" / "connectors"
CATALOG = json.loads((CONNECTORS / "catalog.json").read_text())


def test_every_catalog_digest_matches_the_connector_source():
    stale = []
    for kind, spec in CATALOG["packages"].items():
        for filename, digest in spec["files"].items():
            source = CONNECTORS / filename
            assert source.is_file(), f"{kind}: {filename} is listed in catalog.json but missing"
            actual = hashlib.sha256(source.read_bytes()).hexdigest()
            if actual != digest:
                stale.append(f"{kind}: {filename} is {actual[:12]}, catalog says {digest[:12]}")
    assert not stale, "Update catalog.json after editing these connectors:\n" + "\n".join(stale)


def test_catalog_pins_a_full_commit_revision():
    revision = CATALOG["revision"]
    assert len(revision) == 40 and all(c in "0123456789abcdef" for c in revision)
