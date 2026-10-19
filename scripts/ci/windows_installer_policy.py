"""Read-only, fail-closed Windows installer decision for newest final releases.

Needed means INPUTS changed from the hub's current build revision to the tag,
never that app versions or tags differ. No publication credentials are used.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from urllib.request import Request, urlopen

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.ci import update_reach as reach

URL = "https://hobbycoders.com/v1/desktop/installers"
INSTALLER_URL = "https://hobbycoders.com/v1/desktop/installer"
RELEASE_API = "https://api.github.com/repos/HobbyCoders/ai-shuffle/releases/tags/"
RELEASE_DOWNLOAD = "https://github.com/HobbyCoders/ai-shuffle/releases/download/"


def verify_release(row):
    """Anchor the unsigned hub row to the independently published GitHub assets."""
    tag = "v" + row["version"]
    request = Request(RELEASE_API + tag, headers={"User-Agent": "ai-shuffle-release-check"})
    if token := os.environ.get("GH_TOKEN"):
        # Unredirected: urllib copies ordinary headers to any redirect target.
        request.add_unredirected_header("Authorization", "Bearer " + token)
    with urlopen(request, timeout=30) as response:
        release = json.load(response)
    if release.get("tag_name") != tag or release.get("draft") is not False:
        raise ValueError("win: missing published installer release")

    def asset(name):
        matches = [a for a in release["assets"] if a.get("name") == name]
        if len(matches) != 1:
            raise ValueError("win: missing or ambiguous installer release asset")
        return matches[0]

    def contents(name):
        asset(name)
        request = Request(RELEASE_DOWNLOAD + tag + "/" + name,
                          headers={"User-Agent": "ai-shuffle-release-check"})
        with urlopen(request, timeout=30) as response:
            return response.read()

    name = "AI-Shuffle-Desktop-" + row["version"] + "-win-x64.exe"
    installer = asset(name)
    if (type(installer.get("size")) is not int or installer["size"] != row["size"]
            or installer.get("digest") not in (None, "sha256:" + row["sha256"])):
        raise ValueError("win: GitHub installer digest or size mismatch")
    # Older releases may have no API digest. The checksum published beside the
    # installer still provides an independent digest, never the hub's own claim.
    sums = contents("SHA256SUMS").decode("utf-8").splitlines()
    entries = [line.split() for line in sums if line.strip()]
    checksums = [parts[0] for parts in entries if len(parts) == 2 and parts[1] == name]
    if checksums != [row["sha256"]]:
        raise ValueError("win: GitHub installer checksum mismatch")
    if json.loads(contents("desktop-build.json")).get("revision") != row["revision"]:
        raise ValueError("win: GitHub installer build revision mismatch")


def downloads():
    request = Request(URL, headers={"Cache-Control": "no-cache", "User-Agent": "ai-shuffle-release-check"})
    with urlopen(request, timeout=30) as response:
        return json.load(response)


def windows_row(payload):
    return payload.get("platforms", {}).get("win")


def verify_installer(row):
    """Verify the actual public bytes, not just the presence of a checksum field."""
    if (not isinstance(row, dict) or row.get("url") != INSTALLER_URL
            or not re.fullmatch(r"[a-f0-9]{40}", row.get("revision", ""))
            or not re.fullmatch(r"[a-f0-9]{64}", row.get("sha256", ""))
            or type(row.get("size")) is not int or row["size"] <= 0
            or not reach.FINAL_TAG.fullmatch("v" + row.get("version", ""))):
        raise ValueError("win: missing or unverifiable installer metadata")
    revision = reach.git("rev-parse", f"refs/tags/v{row['version']}^{{commit}}")
    if revision != row["revision"]:
        raise ValueError("win: installer revision does not match its release tag")
    verify_release(row)
    digest, size = hashlib.sha256(), 0
    request = Request(INSTALLER_URL, headers={"Cache-Control": "no-cache", "User-Agent": "ai-shuffle-release-check"})
    with urlopen(request, timeout=30) as response:
        while chunk := response.read(1024 * 1024):
            size += len(chunk)
            if size > row["size"]:
                raise ValueError("win: installer size mismatch")
            digest.update(chunk)
    if size != row["size"] or digest.hexdigest() != row["sha256"]:
        raise ValueError("win: installer checksum mismatch")


def changed_inputs(base, head):
    # Verify both commit objects first; a missing history/blob must mean build.
    for revision in (base, head):
        reach.git("cat-file", "-e", revision + "^{commit}")
    changed = reach.git("diff", "--name-only", "--no-renames", base, head).splitlines()
    return reach.windows_installer_changes(changed, reach.reader(base), reach.reader(head))


def newest_final(tag):
    if not reach.FINAL_TAG.fullmatch(tag):
        return False
    tags = [t for t in reach.git("tag", "--list").splitlines() if reach.FINAL_TAG.fullmatch(t)]
    rank = lambda t: tuple(map(int, t[1:].split(".")))
    return tag == max([tag, *tags], key=rank)


def decide(tag, revision, *, force=False):
    if not newest_final(tag):
        return {"build": False, "reason": "not newest final stable", "previous": None}
    if force:
        return {"build": True, "reason": "owner forced build_desktop", "previous": None}
    try:
        row = windows_row(downloads())
        verify_installer(row)
        inputs = changed_inputs(row["revision"], revision)
        return {"build": bool(inputs), "reason": "installer inputs changed" if inputs else "installer inputs unchanged",
                "previous": row}
    except Exception as error:
        # Do not log HTTP bodies, URLs supplied by the hub, or subprocess output.
        return {"build": True, "reason": "win: hub installer cannot be verified (" + type(error).__name__ + ")",
                "previous": None}


def verify_unchanged(row, previous, revision):
    if not isinstance(previous, dict) or row != previous:
        raise ValueError("win: skipped build must retain the previous installer exactly")
    verify_installer(row)
    if changed_inputs(row["revision"], revision):
        raise ValueError("win: installer inputs changed; a Windows build is required")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    decision = decide(args.tag, args.revision, force=args.force)
    encoded = json.dumps(decision, separators=(",", ":"))
    if args.github_output:
        with args.github_output.open("a") as output:
            output.write("build=" + str(decision["build"]).lower() + "\npolicy=" + encoded + "\n")
    print(encoded)
    return 0


if __name__ == "__main__":
    sys.exit(main())
