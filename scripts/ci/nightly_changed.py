"""Cheap scheduled no-op check. Publication still requires the exact-main gate.

The HTTPS pointer is only a reason to skip work, never permission to publish.
No dependencies, credentials or signing keys are needed here.
"""
import base64
import json
import os
from pathlib import Path
import re
import urllib.error
import urllib.request

URL = "https://hobbycoders.com/v1/desktop/manifest/edge"  # same pointer as /nightly
# Cloudflare answers 403 to urllib's default "Python-urllib/x.y" agent, which
# silently stopped every scheduled edge publication (2026-10-12). Name the caller.
USER_AGENT = "ai-shuffle-ci/1 (+https://github.com/HobbyCoders/ai-shuffle)"


def changed(revision, fetch):
    if not re.fullmatch(r"[a-f0-9]{40}", revision):
        raise ValueError("Invalid main revision")
    try:
        raw = fetch()
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return True
        raise
    envelope = json.loads(raw)
    manifest = json.loads(base64.b64decode(envelope["payload"], validate=True))
    if manifest.get("channel") not in ("edge", "nightly"):
        raise ValueError("Expected nightly pointer")
    return manifest["revision"] != revision


def fetch():
    request = urllib.request.Request(URL, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=20) as response:
        raw = response.read(32769)
    if len(raw) > 32768:
        raise ValueError("Manifest exceeds limits")
    return raw


if __name__ == "__main__":
    needed = changed(os.environ["GITHUB_SHA"], fetch)
    print("Main changed; checking the local attestation" if needed else "Nightly already published; nothing to do")
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
        output.write(f"changed={str(needed).lower()}\n")
