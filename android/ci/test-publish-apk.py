"""Exercise the real publishing script against a local fake gh command."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).with_name("publish-apk.sh").resolve()
BASH = str(Path(os.environ["ProgramFiles"]) / "Git/bin/bash.exe") if os.name == "nt" else "bash"


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "calls"
        (self.root / "AI-Shuffle-Android.apk").write_bytes(b"verified-apk")
        checksum = hashlib.sha256(b"verified-apk").hexdigest()
        (self.root / "AI-Shuffle-Android.apk.sha256").write_text(f"{checksum}  AI-Shuffle-Android.apk\n", newline="\n")
        info = {"runNumber": 42, "versionName": "0.8.5", "versionCode": 29800000,
                "commit": "abc123", "runUrl": "https://github.com/example/app/actions/runs/99"}
        (self.root / "android-build.json").write_text(json.dumps(info))
        mock = self.bin / "gh"
        mock.write_text('''#!/usr/bin/env bash
set -eu
printf '%s\\n' "$*" >> "$TEST_CALLS"
case "$1 $2" in
  'release view') test "$TEST_EXISTS" = yes ;;
  'release download')
    while [ "$1" != --dir ]; do shift; done
    printf '{"runNumber":%s}\\n' "$TEST_PREVIOUS" > "$2/android-build.json" ;;
  'release create'|'release upload'|'release edit') ;;
  *) exit 97 ;;
esac
''', newline="\n")
        mock.chmod(0o755)
        python = self.bin / "python3"
        python.write_text(f'#!/usr/bin/env bash\nexec "{Path(sys.executable).as_posix()}" "$@"\n', newline="\n")
        python.chmod(0o755)
        self.env = {**os.environ, "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
                    "GITHUB_REPOSITORY": "example/app", "GITHUB_SHA": "abc123", "RELEASE_TAG": "",
                    "TEST_CALLS": self.log.as_posix(), "TEST_EXISTS": "no", "TEST_PREVIOUS": "41"}

    def publish(self, expected=0):
        result = subprocess.run([BASH, str(SCRIPT), self.root.as_posix()], env=self.env,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return self.log.read_text() if self.log.exists() else ""

    def test_first_publication_has_assets_and_does_not_replace_latest_stable(self):
        calls = self.publish()
        self.assertIn("release create android-latest AI-Shuffle-Android.apk", calls)
        self.assertIn("--prerelease --latest=false", calls)
        self.assertIn("abc123", (self.root / "release-notes.md").read_text())

    def test_older_rerun_cannot_replace_newer_apk(self):
        self.env.update(TEST_EXISTS="yes", TEST_PREVIOUS="43")
        calls = self.publish()
        self.assertNotIn("release upload", calls)
        self.assertNotIn("release edit", calls)

    def test_newer_build_updates_assets_and_notes(self):
        self.env["TEST_EXISTS"] = "yes"
        calls = self.publish()
        self.assertIn("release upload android-latest", calls)
        self.assertIn("release edit android-latest", calls)
        self.assertNotIn("release create", calls)

    def test_versioned_release_normalizes_tag_and_only_attaches_assets(self):
        self.env.update(TEST_EXISTS="yes", RELEASE_TAG=" V0.8.5 ")
        calls = self.publish()
        self.assertIn("release upload v0.8.5", calls)
        self.assertNotIn("android-latest", calls)
        self.assertNotIn("release edit", calls)

    def test_corrupt_download_fails_before_any_github_call(self):
        (self.root / "AI-Shuffle-Android.apk").write_bytes(b"corrupted")
        self.assertEqual(self.publish(expected=1), "")


if __name__ == "__main__":
    unittest.main()
