"""The release tag is the version source; the checked-in fallbacks must agree.

Release builds inject the tag (APP_VERSION for the backend and frontend,
electron-builder extraMetadata for desktop, ANDROID_VERSION_NAME for Android).
Source runs fall back to literals in four places, and this test stops them
drifting apart the way 0.8.9 did while the tags moved on to 0.8.18.
"""

import json
import re
from pathlib import Path

import pytest

from app import __version__

ROOT = Path(__file__).resolve().parents[2]


def _package_version(rel: str) -> str:
    return json.loads((ROOT / rel).read_text(encoding="utf-8"))["version"]


def test_frontend_package_matches_backend_fallback():
    assert _package_version("frontend/package.json") == __version__


def test_desktop_package_matches_backend_fallback():
    assert _package_version("desktop/package.json") == __version__


@pytest.mark.parametrize("directory", ["frontend", "desktop"])
def test_package_lock_versions_match_backend_fallback(directory):
    lock = json.loads((ROOT / directory / "package-lock.json").read_text())
    assert lock["version"] == __version__
    assert lock["packages"][""]["version"] == __version__


def test_android_fallback_matches_backend_fallback():
    gradle = (ROOT / "android/app/build.gradle.kts").read_text(encoding="utf-8")
    match = re.search(r'ANDROID_VERSION_NAME"\)\s*\?:\s*"([^"]+)"', gradle)
    assert match, "versionName fallback not found in android/app/build.gradle.kts"
    assert match.group(1) == __version__


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("v1.2.3", "1.2.3"), ("1.2.3", "1.2.3"), ("0.9.0-dev.4+gabc1234", "0.9.0-dev.4+gabc1234"), ("", __version__)],
)
def test_app_version_env_sets_settings_version(monkeypatch, raw, expected):
    from app.core.config import Settings

    monkeypatch.setenv("APP_VERSION", raw)
    assert Settings().version == expected
