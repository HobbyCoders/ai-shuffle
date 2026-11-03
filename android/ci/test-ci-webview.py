"""Check transfer recovery without ever retrying an emulator or app test."""
import base64
import hashlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

import ci_webview as provider

PINNED_SHA256 = provider.SHA256  # Preserve the real pin while transfer fixtures patch it.


class Response(io.BytesIO):
    status = 200


class ProviderDownloadTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.apk = Path(self.folder.name) / "webview.apk"
        self.patches = [patch.object(provider, "SIZE", 3),
                        patch.object(provider, "SHA256", hashlib.sha256(b"abc").hexdigest()),
                        patch.object(provider.time, "sleep")]
        for item in self.patches:
            item.start(); self.addCleanup(item.stop)

    def test_full_transfer_checks_size_and_sha(self):
        with patch.object(provider.urllib.request, "urlopen", return_value=Response(base64.b64encode(b"abc"))) as request:
            provider.download(self.apk)
        self.assertEqual(b"abc", self.apk.read_bytes())
        request.assert_called_once_with(provider.URL, timeout=120)

    def test_verified_cached_apk_skips_network_transfer(self):
        self.apk.write_bytes(b"abc")
        with patch.object(provider.urllib.request, "urlopen") as request:
            provider.download(self.apk)
        request.assert_not_called()
        self.assertEqual(b"abc", self.apk.read_bytes())

    def test_corrupt_cache_is_rejected_without_network_or_install(self):
        for data, error in ((b"ab", provider.IncompleteDownload), (b"bad", RuntimeError)):
            with self.subTest(data=data), patch.object(provider.urllib.request, "urlopen") as request:
                self.apk.write_bytes(data)
                with self.assertRaises(error):
                    provider.download(self.apk)
                request.assert_not_called()

    def test_workflow_cache_keys_match_the_verified_digest(self):
        import yaml
        root = Path(__file__).resolve().parents[2]
        for name, job in (("android.yml", "android-ui"), ("ci-android-minified.yml", "smoke")):
            with self.subTest(workflow=name):
                workflow = yaml.load((root / ".github/workflows" / name).read_text(), Loader=yaml.BaseLoader)
                steps = workflow["jobs"][job]["steps"]
                caches = [(i, step) for i, step in enumerate(steps)
                          if step.get("uses", "").startswith("actions/cache@")]
                self.assertEqual(1, len(caches))
                index, cache = caches[0]
                self.assertEqual("matrix.api == 36", cache["if"])
                self.assertEqual("aosp-webview-x86_64-" + PINNED_SHA256, cache["with"]["key"])
                self.assertEqual("${{ runner.temp }}/ci-webview.apk", cache["with"]["path"])
                verify = next(i for i, step in enumerate(steps) if 'run' in step and
                              'python3 ci/ci_webview.py "$RUNNER_TEMP/ci-webview.apk"' in step['run'])
                self.assertLess(index, verify)
                self.assertEqual("matrix.api == 36", steps[verify]["if"])
                self.assertNotIn("cache-hit", steps[verify]["if"])

    def test_only_a_truncated_transfer_is_recovered(self):
        responses = [Response(base64.b64encode(b"ab")), Response(base64.b64encode(b"abc"))]
        with patch.object(provider.urllib.request, "urlopen", side_effect=responses) as request:
            provider.download(self.apk)
        self.assertEqual(2, request.call_count)
        self.assertEqual(b"abc", self.apk.read_bytes())

    def test_full_size_checksum_mismatch_is_not_retried(self):
        with patch.object(provider.urllib.request, "urlopen", return_value=Response(base64.b64encode(b"bad"))) as request:
            with self.assertRaisesRegex(RuntimeError, "checksum mismatch"):
                provider.download(self.apk)
        self.assertEqual(1, request.call_count)

    def test_network_failures_have_a_strict_attempt_limit(self):
        with patch.object(provider.urllib.request, "urlopen", side_effect=TimeoutError("transfer timeout")) as request:
            with self.assertRaises(TimeoutError):
                provider.download(self.apk)
        self.assertEqual(3, request.call_count)

    def test_permanent_http_failure_is_not_retried(self):
        error = urllib.error.HTTPError(provider.URL, 404, "Not found", {}, None)
        with patch.object(provider.urllib.request, "urlopen", side_effect=error) as request:
            with self.assertRaises(urllib.error.HTTPError):
                provider.download(self.apk)
        self.assertEqual(1, request.call_count)

    def test_transient_http_failure_can_recover_only_the_transfer(self):
        error = urllib.error.HTTPError(provider.URL, 503, "Unavailable", {}, None)
        with patch.object(provider.urllib.request, "urlopen", side_effect=[error, Response(base64.b64encode(b"abc"))]) as request:
            provider.download(self.apk)
        self.assertEqual(2, request.call_count)

    def test_incomplete_file_cannot_be_installed(self):
        self.apk.write_bytes(b"ab")
        with self.assertRaisesRegex(provider.IncompleteDownload, "2 bytes, expected 3"):
            provider.verify(self.apk)


if __name__ == "__main__":
    unittest.main()
