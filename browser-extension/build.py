"""Package the AI Shuffle browser extension.

    python browser-extension/build.py

writes, under ``browser-extension/dist/``:

* ``unpacked/``: load this folder with "Load unpacked" (chrome://extensions or
  edge://extensions, Developer mode on). Its manifest carries the public key
  in ``dev-key.txt``, so the extension ID is always
  ``fnjpjlkegjhjepkdnnignfokaffgdmak``: the ID the account service and workers
  accept for sign-in and the side panel.
* ``ai-shuffle-extension-<version>.zip``: the same files without the key, for
  the Chrome Web Store and Edge Add-ons (each store assigns its own ID).
"""

from __future__ import annotations

import json
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FILES = [
    "manifest.json",
    "background.js",
    "account.js",
    "tabs.js",
    "sidepanel.html",
    "sidepanel.css",
    "sidepanel.js",
    "icons/icon16.png",
    "icons/icon32.png",
    "icons/icon48.png",
    "icons/icon128.png",
]


def main() -> int:
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    dist = ROOT / "dist"
    unpacked = dist / "unpacked"
    if unpacked.exists():
        shutil.rmtree(unpacked)
    unpacked.mkdir(parents=True)
    for name in FILES:
        target = unpacked / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    dev = dict(manifest, key=(ROOT / "dev-key.txt").read_text(encoding="utf-8").strip())
    (unpacked / "manifest.json").write_text(json.dumps(dev, indent=2) + "\n", encoding="utf-8")

    store = dist / f"ai-shuffle-extension-{manifest['version']}.zip"
    with zipfile.ZipFile(store, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in FILES:
            archive.write(ROOT / name, name)
    print(f"Unpacked (developer ID): {unpacked}")
    print(f"Store package: {store}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
