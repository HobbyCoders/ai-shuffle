#!/usr/bin/env python3
"""Verify a built APK or AAB contains the mobile bundle, not just its HTML shell."""
from __future__ import annotations

import argparse
from pathlib import Path
import zipfile


def verify_apk(apk: Path, bundle: Path, *, app_bundle: bool = False) -> None:
    prefix = "base/assets/mobile-ui/" if app_bundle else "assets/mobile-ui/"
    kind = "AAB" if app_bundle else "APK"
    expected = {file.relative_to(bundle).as_posix(): file for file in bundle.rglob("*") if file.is_file()}
    if "index.html" not in expected or not any(name.startswith("_app/") and name.endswith(".js") for name in expected):
        raise ValueError("The source mobile bundle is missing its index or compiled JavaScript")
    with zipfile.ZipFile(apk) as archive:
        names = set(archive.namelist())
        missing = sorted(name for name in expected if prefix + name not in names)
        if missing:
            raise ValueError(f"{kind} is missing {len(missing)} mobile assets: {missing[:5]}; check ignoreAssetsPattern")
        for name, file in expected.items():
            if archive.read(prefix + name) != file.read_bytes():
                raise ValueError(f"{kind} has stale or modified mobile asset: {name}")
    print(f"Verified {len(expected)} mobile assets in {apk.name}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("apk", type=Path)
    parser.add_argument("--app-bundle", action="store_true", help="Verify an AAB instead of an APK")
    parser.add_argument("--bundle", type=Path, default=Path(__file__).resolve().parents[1] / "app/src/main/assets/mobile-ui")
    args = parser.parse_args()
    verify_apk(args.apk, args.bundle, app_bundle=args.app_bundle)
