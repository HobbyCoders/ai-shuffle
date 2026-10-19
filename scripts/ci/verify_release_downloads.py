"""Verify current Linux/Android and either built or proven-unchanged Windows downloads."""
from __future__ import annotations

import argparse
import json
import sys
from urllib.request import Request, urlopen
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.ci import windows_installer_policy as windows

URL = "https://hobbycoders.com/v1/desktop/installers"
WORKFLOWS = {"win": "desktop-installer.yml", "linux": "platform-installer.yml", "android": "platform-installer.yml"}


def verify(payload, tag, revision, *, publication_ready=True, windows_policy=None, windows_result="success"):
    platforms = payload.get("platforms", {}) if isinstance(payload, dict) else {}
    if not isinstance(platforms, dict):
        platforms = {}
    errors = []
    if not publication_ready:
        errors.append("Release gates failed or were skipped; do not manually register installers. "
                      "Fix the failed release jobs and retry the same release before registration.")
    for platform, workflow in WORKFLOWS.items():
        row = platforms.get(platform)
        current = isinstance(row, dict) and row.get("version") == tag.removeprefix("v") and row.get("revision") == revision
        if platform == "win" and windows_policy is not None:
            # A sealed promotion may include a retained Windows asset even if a
            # new decision says its inputs need not be rebuilt. Never rebuild it.
            built = windows_result == "success" or (windows_result == "promoted" and current)
            if not built:
                try:
                    if windows_result not in {"skipped", "promoted"} or windows_policy.get("build") is not False:
                        raise ValueError("win: required Windows build did not succeed; fix the release jobs and retry")
                    windows.verify_unchanged(row, windows_policy.get("previous"), revision)
                except Exception as error:
                    errors.append(str(error) if isinstance(error, ValueError) and str(error).startswith("win:") else
                                  f"win: cannot verify previous installer ({type(error).__name__}); fix the release jobs and retry")
                continue
        if not current:
            message = f"{platform}: missing or stale download for {tag} ({revision})"
            if publication_ready:
                message += f"; retry release registration or run {workflow} from {tag}"
                if platform != "win":
                    message += f" with platform={platform} and release_assets=true (no build run ID)"
            errors.append(message)
    if errors:
        raise ValueError("\n".join(errors))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--activation-result", required=True, choices=("success", "failure", "skipped", "cancelled"))
    parser.add_argument("--windows-policy", type=json.loads)
    parser.add_argument("--windows-result", default="success", choices=("success", "failure", "skipped", "cancelled", "promoted"))
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
        verify(payload, args.tag, args.revision, publication_ready=args.activation_result == "success",
               windows_policy=args.windows_policy, windows_result=args.windows_result)
    except ValueError as error:
        for line in str(error).splitlines():
            print("::error::" + line, file=sys.stderr)
        return 1
    print(f"PASS: linux/android match {args.tag} ({args.revision}); win matches the release or the verified unchanged installer")
    return 0


if __name__ == "__main__":
    sys.exit(main())
