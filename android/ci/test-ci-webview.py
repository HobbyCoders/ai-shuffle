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
