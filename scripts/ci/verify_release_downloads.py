"""Fail a release unless every public download names its exact tag and revision."""
from __future__ import annotations

import argparse
import json
import sys
from urllib.request import Request, urlopen

URL = "https://hobbycoders.com/v1/desktop/installers"
WORKFLOWS = {"win": "desktop-installer.yml", "linux": "platform-installer.yml", "android": "platform-installer.yml"}


def verify(payload, tag, revision):
    platforms = payload.get("platforms", {}) if isinstance(payload, dict) else {}
    if not isinstance(platforms, dict):
        platforms = {}
    errors = []
    for platform, workflow in WORKFLOWS.items():
        row = platforms.get(platform)
        if not isinstance(row, dict) or row.get("version") != tag.removeprefix("v") or row.get("revision") != revision:
            errors.append(f"{platform}: missing or stale download for {tag} ({revision}); "
                          f"retry release registration or run {workflow} from {tag}" +
                          (f" with platform={platform}" if platform != "win" else ""))
    if errors:
        raise ValueError("\n".join(errors))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--revision", required=True)
    args = parser.parse_args()
    try:
        request = Request(URL, headers={"Cache-Control": "no-cache", "User-Agent": "ai-shuffle-release-check"})
        with urlopen(request, timeout=30) as response:
            payload = json.load(response)
    except Exception as error:
        # Never include HTTP response bodies in release logs.
        print(f"::error::Cannot verify win, linux, android downloads: {type(error).__name__}", file=sys.stderr)
        return 1
    try:
        verify(payload, args.tag, args.revision)
    except ValueError as error:
        for line in str(error).splitlines():
            print("::error::" + line, file=sys.stderr)
        return 1
    print(f"PASS: win, linux, android downloads all match {args.tag} ({args.revision})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
