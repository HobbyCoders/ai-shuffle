"""Pinned official AOSP provider for the API 36 emulator, with all crash checks on."""
import base64
import hashlib
from http.client import IncompleteRead
from pathlib import Path
import sys
import time
import urllib.error
import urllib.request

VERSION = "136.0.7103.129"
COMMIT = "9037cdbb9b19590479b79e27c6693110381ce7ff"
SHA256 = "8da41a8692c64870dc875a0fa656c59a678aa09902935c5095e7de6fd7c55a87"
SIZE = 274674253
URL = ("https://android.googlesource.com/platform/external/chromium-webview/+/"
       f"{COMMIT}/{VERSION}/x86_64/webview.apk?format=TEXT")


class IncompleteDownload(RuntimeError):
    pass


def verify(apk):
    size = Path(apk).stat().st_size
    if size != SIZE:
        raise IncompleteDownload(f"Incomplete AOSP WebView: {size} bytes, expected {SIZE}")
    with Path(apk).open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if digest != SHA256:
        raise RuntimeError(f"AOSP WebView checksum mismatch: {digest}")


def transfer(apk):
    # Gitiles serves a base64 blob. Decode aligned chunks without buffering a
    # several-hundred-MB response in memory. No mutable latest URL or APK mirror.
    with urllib.request.urlopen(URL, timeout=120) as response, Path(apk).open("wb") as output:
        if response.status != 200:
            raise IncompleteDownload(f"Incomplete AOSP WebView HTTP response: {response.status}")
        pending = b""
        while chunk := response.read(1024 * 1024):
            pending += chunk
            length = len(pending) // 4 * 4
            output.write(base64.b64decode(pending[:length], validate=True))
            pending = pending[length:]
        if pending:
            raise IncompleteDownload("Incomplete AOSP WebView base64 download")
    verify(apk)


def download(apk):
    # Only retry a failed/truncated NETWORK TRANSFER, never an emulator/test.
    # A full-size checksum mismatch is not transient and must fail immediately.
    for attempt in range(1, 4):
        try:
            transfer(apk)
            break
        except urllib.error.HTTPError as error:
            if error.code not in (408, 429, 500, 502, 503, 504) or attempt == 3:
                raise
            print(f"AOSP download HTTP {error.code}; transfer attempt {attempt}/3 failed", flush=True)
        except (IncompleteDownload, IncompleteRead, urllib.error.URLError, TimeoutError, ConnectionError) as error:
            if attempt == 3:
                raise
            print(f"AOSP download transfer attempt {attempt}/3 failed: {error}", flush=True)
        time.sleep(2 * attempt)
    print(f"Verified official AOSP WebView {VERSION}, source commit {COMMIT}, SHA256 {SHA256}")


if __name__ == "__main__":
    download(sys.argv[1])
