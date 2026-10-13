#!/usr/bin/env python3
"""Regression coverage for Android dropping Svelte's underscore-prefixed assets."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
import zipfile

spec = importlib.util.spec_from_file_location("verify_mobile_apk", Path(__file__).with_name("verify-mobile-apk.py"))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class MobileApkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bundle = self.root / "bundle"
        self.contents = {"index.html": b"<script src='_app/entry.js'></script>",
                         "_app/entry.js": b"window.booted = true", "_app/styles.css": b"body{}"}
        for name, data in self.contents.items():
            path = self.bundle / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        self.apk = self.root / "app.apk"

    def pack(self, contents):
        with zipfile.ZipFile(self.apk, "w") as archive:
            for name, data in contents.items():
                archive.writestr("assets/offline-demo/" + name, data)

    def test_complete_bundle_matches_apk(self):
        self.pack(self.contents)
        module.verify_apk(self.apk, self.bundle)

    def test_html_only_apk_is_not_a_working_ui(self):
        self.pack({"index.html": self.contents["index.html"]})
        with self.assertRaisesRegex(ValueError, "missing 2 mobile assets"):
            module.verify_apk(self.apk, self.bundle)

    def test_stale_script_is_rejected(self):
        self.pack({**self.contents, "_app/entry.js": b"old version"})
        with self.assertRaisesRegex(ValueError, "stale or modified"):
            module.verify_apk(self.apk, self.bundle)

    def test_complete_app_bundle_matches_source(self):
        with zipfile.ZipFile(self.apk, "w") as archive:
            for name, data in self.contents.items():
                archive.writestr("base/assets/offline-demo/" + name, data)
        module.verify_apk(self.apk, self.bundle, app_bundle=True)

    def test_app_bundle_rejects_apk_layout(self):
        self.pack(self.contents)
        with self.assertRaisesRegex(ValueError, "missing 3 mobile assets"):
            module.verify_apk(self.apk, self.bundle, app_bundle=True)

    def test_app_bundle_rejects_stale_asset(self):
        with zipfile.ZipFile(self.apk, "w") as archive:
            for name, data in self.contents.items():
                archive.writestr("base/assets/offline-demo/" + name, b"stale" if name.endswith(".js") else data)
        with self.assertRaisesRegex(ValueError, "stale or modified"):
            module.verify_apk(self.apk, self.bundle, app_bundle=True)

    def test_missing_source_build_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "source mobile bundle"):
            module.verify_apk(self.apk, self.root / "absent")


if __name__ == "__main__":
    unittest.main()
