"""Real Git fixtures for tagged and untagged Play display versions."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).with_name("resolve-version-name.py")


class VersionNameTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = self.temp.name
        self.git("init", "-q")
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                 "commit", "--allow-empty", "-m", "initial")

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.repo, text=True).strip()

    def resolve(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=self.repo,
                              text=True, capture_output=True)

    def test_tagged_and_untagged_versions_follow_source(self):
        self.git("tag", "v0.8.17")
        self.assertEqual(self.resolve("--release-tag", "v0.8.17").stdout.strip(), "0.8.17")
        self.assertEqual(self.resolve().stdout.strip(), "0.8.17")
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                 "commit", "--allow-empty", "-m", "next")
        result = self.resolve()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stdout.strip(), r"^0\.8\.17-dev\.1\+g[0-9a-f]+$")
        self.assertNotEqual(self.resolve("--release-tag", "v0.8.17").returncode, 0)

    def test_missing_history_or_invalid_tag_fails_instead_of_using_stale_default(self):
        self.assertNotEqual(self.resolve().returncode, 0)
        self.assertNotEqual(self.resolve("--release-tag", "v0.8.17\nINJECT=value").returncode, 0)


if __name__ == "__main__":
    unittest.main()
