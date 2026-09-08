"""Package the signed Gradle output with its checksum and build provenance."""

import hashlib
import json
import os
from pathlib import Path
import shutil


def prepare(source: Path, destination: Path, environ=os.environ):
    metadata = json.loads((source / "output-metadata.json").read_text())
    if metadata["applicationId"] != "com.hobbycoders.aishuffle":
        raise ValueError("Unexpected Android application ID")
    # This app ships one universal APK, not ABI-specific split APKs.
    element, = metadata["elements"]
    apk = source / "app-release.apk"
    if element["outputFile"] != apk.name or not apk.is_file():
        raise ValueError("Expected the signed universal release APK")
    destination.mkdir(parents=True, exist_ok=True)
    download = destination / "AI-Shuffle-Android.apk"
    shutil.copyfile(apk, download)
    digest = hashlib.sha256(download.read_bytes()).hexdigest()
    (destination / "AI-Shuffle-Android.apk.sha256").write_text(f"{digest}  {download.name}\n", newline="\n")
    info = {
        "versionName": element["versionName"],
        "versionCode": element["versionCode"],
        "sha256": digest,
        "commit": environ["GITHUB_SHA"],
        "runNumber": int(environ["GITHUB_RUN_NUMBER"]),
        "runUrl": f"{environ['GITHUB_SERVER_URL']}/{environ['GITHUB_REPOSITORY']}/actions/runs/{environ['GITHUB_RUN_ID']}",
    }
    (destination / "android-build.json").write_text(json.dumps(info, indent=2) + "\n")


if __name__ == "__main__":
    prepare(Path("app/build/outputs/apk/release"), Path("app/build/outputs/download"))
