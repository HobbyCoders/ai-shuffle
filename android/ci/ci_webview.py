"""Pinned official AOSP provider for the API 36 emulator, with all crash checks on."""
import base64
import hashlib
from pathlib import Path
import sys
import urllib.request

VERSION = "136.0.7103.129"
COMMIT = "9037cdbb9b19590479b79e27c6693110381ce7ff"
SHA256 = "8da41a8692c64870dc875a0fa656c59a678aa09902935c5095e7de6fd7c55a87"
URL = ("https://android.googlesource.com/platform/external/chromium-webview/+/"
       f"{COMMIT}/{VERSION}/x86_64/webview.apk?format=TEXT")


def verify(apk):
    with Path(apk).open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if digest != SHA256:
        raise RuntimeError(f"AOSP WebView checksum mismatch: {digest}")


def download(apk):
    # Gitiles serves a base64 blob. Decode aligned chunks without buffering a
    # several-hundred-MB response in memory. No mutable latest URL or APK mirror.
    with urllib.request.urlopen(URL, timeout=120) as response, Path(apk).open("wb") as output:
        pending = b""
        while chunk := response.read(1024 * 1024):
            pending += chunk
            length = len(pending) // 4 * 4
            output.write(base64.b64decode(pending[:length], validate=True))
            pending = pending[length:]
        if pending:
            raise RuntimeError("Incomplete AOSP WebView download")
    verify(apk)
    print(f"Verified official AOSP WebView {VERSION}, source commit {COMMIT}, SHA256 {SHA256}")


if __name__ == "__main__":
    download(sys.argv[1])
