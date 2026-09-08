"""Checks that public downloads are universal, correctly identified and intact."""

import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("prepare_apk", Path(__file__).with_name("prepare-apk-download.py"))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source = Path(self.temp.name) / "release"
        self.source.mkdir()
        self.destination = Path(self.temp.name) / "download"
        self.env = {"GITHUB_SHA": "abc123", "GITHUB_RUN_NUMBER": "42", "GITHUB_RUN_ID": "99",
                    "GITHUB_SERVER_URL": "https://github.com", "GITHUB_REPOSITORY": "HobbyCoders/ai-shuffle"}
        self.metadata = {"applicationId": "com.hobbycoders.aishuffle", "elements": [
            {"outputFile": "app-release.apk", "versionCode": 29800000, "versionName": "0.8.5"}]}
        (self.source / "app-release.apk").write_bytes(b"signed-apk-fixture")

    def prepare(self):
        (self.source / "output-metadata.json").write_text(json.dumps(self.metadata))
        module.prepare(self.source, self.destination, self.env)

    def test_download_bytes_checksum_and_provenance_agree(self):
        self.prepare()
        apk = (self.destination / "AI-Shuffle-Android.apk").read_bytes()
        self.assertEqual(apk, b"signed-apk-fixture")
        digest = hashlib.sha256(apk).hexdigest()
        self.assertEqual((self.destination / "AI-Shuffle-Android.apk.sha256").read_text(),
                         f"{digest}  AI-Shuffle-Android.apk\n")
        info = json.loads((self.destination / "android-build.json").read_text())
        self.assertEqual(info["sha256"], digest)
        self.assertEqual(info["versionCode"], 29800000)
        self.assertEqual(info["versionName"], "0.8.5")
        self.assertEqual(info["commit"], "abc123")
        self.assertEqual(info["runNumber"], 42)
        self.assertEqual(info["runUrl"], "https://github.com/HobbyCoders/ai-shuffle/actions/runs/99")

    def test_rejects_a_different_application(self):
        self.metadata["applicationId"] = "com.example.other"
        with self.assertRaisesRegex(ValueError, "application ID"):
            self.prepare()
        self.assertFalse(self.destination.exists())

    def test_rejects_unsigned_release_output(self):
        self.metadata["elements"][0]["outputFile"] = "app-release-unsigned.apk"
        with self.assertRaisesRegex(ValueError, "signed universal"):
            self.prepare()
        self.assertFalse(self.destination.exists())

    def test_rejects_split_apks_instead_of_silently_picking_one(self):
        self.metadata["elements"].append(dict(self.metadata["elements"][0]))
        with self.assertRaises(ValueError):
            self.prepare()
        self.assertFalse(self.destination.exists())


if __name__ == "__main__":
    unittest.main()
