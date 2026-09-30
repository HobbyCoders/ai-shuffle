"""Conservatively omit APK work for PRs confined to Android web/docs files.

Read every page from `gh api --paginate --slurp pulls/.../files`. Renames must
classify both paths. Unknown paths and malformed/incomplete evidence fail safe.
Manual and release workflows bypass this classifier and always build the APK.
"""
import json
from pathlib import Path
import sys


NON_APK_PREFIXES = ("android/hub/", "android/site/", "android/docs/",
                    "docs/")


def apk_required(pages, pr=None, expected_head=None):
    if not isinstance(pages, list) or not pages:
        raise ValueError("Missing PR file pages")
    files = []
    for page in pages:
        if not isinstance(page, list):
            raise ValueError("Invalid PR file page")
        files.extend(page)
    if expected_head is not None or pr is not None:
        if (not isinstance(pr, dict) or not isinstance(pr.get("head"), dict)
                or pr["head"].get("sha") != expected_head
                or pr.get("changed_files") != len(files)):
            raise ValueError("PR moved or changed-file evidence is incomplete")
    # GitHub's PR files API is capped at 3000 files. Never trust a capped list
    # to omit platform coverage, even if every returned path looks irrelevant.
    if not files or len(files) >= 3000:
        return True
    names = []
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("filename"), str):
            raise ValueError("Invalid PR file record")
        names.append(item["filename"])
        if item.get("status") == "renamed" and not item.get("previous_filename"):
            raise ValueError("Missing previous path for renamed file")
        if "previous_filename" in item:
            if not isinstance(item["previous_filename"], str):
                raise ValueError("Invalid previous path")
            names.append(item["previous_filename"])
    # Exclude only known web/docs inputs, not future Android build directories.
    return any(not name.startswith(NON_APK_PREFIXES) for name in names)


if __name__ == "__main__":
    pages = json.loads(Path(sys.argv[1]).read_text())
    pr = json.loads(Path(sys.argv[2]).read_text())
    print("apk=" + str(apk_required(pages, pr, sys.argv[3])).lower())
