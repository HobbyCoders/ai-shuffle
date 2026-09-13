"""Security regressions for the account hub's development dependencies."""

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_hub_lockfile_excludes_vulnerable_sharp():
    lock = json.loads((ROOT / "android/hub/package-lock.json").read_text(encoding="utf-8"))
    sharp_packages = {
        path: package
        for path, package in lock["packages"].items()
        if path.endswith("node_modules/sharp")
    }
    assert sharp_packages, "Check all direct and nested Miniflare sharp dependencies"
    for path, package in sharp_packages.items():
        version = package["version"]
        release, separator, _ = version.partition("-")
        assert not separator, f"{path}: unreviewed prerelease {version}"
        assert tuple(map(int, release.split("."))) >= (0, 35, 4), (
            f"{path}: sharp {version} is affected by GHSA-rgj7-g3m4-5g8c"
        )
