#!/usr/bin/env python3
"""Fail if a Play bundle's manifest still declares app control's AccessibilityService.

The Google Play build is made with ANDROID_PLAY_STORE_BUILD=true, which removes
the service (android/app/src/playStore/AndroidManifest.xml). Play blocks
releases that include an undeclared AccessibilityService, so check the built
artifact rather than trusting the build switch.
"""
import sys
import zipfile
from pathlib import Path

MANIFEST = "base/manifest/AndroidManifest.xml"
PACKAGE = b"com.hobbycoders.aishuffle"
SERVICE = b"ShuffleAccessibilityService"
ACCESSIBILITY_ACTION = b"android.accessibilityservice.AccessibilityService"


def check(bundle: Path) -> list[str]:
    with zipfile.ZipFile(bundle) as archive:
        # The bundle stores the manifest as protobuf; names are plain UTF-8 inside it.
        manifest = archive.read(MANIFEST)
    if PACKAGE not in manifest:
        return [f"{MANIFEST} does not name {PACKAGE.decode()}; is this the AI Shuffle bundle?"]
    return [f"{bundle} still declares {needle.decode()}"
            for needle in (SERVICE, ACCESSIBILITY_ACTION) if needle in manifest]


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {argv[0]} app-release.aab", file=sys.stderr)
        return 2
    problems = check(Path(argv[1]))
    for problem in problems:
        print(f"::error::{problem}", file=sys.stderr)
    if not problems:
        print("Play bundle manifest has no AccessibilityService.")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
