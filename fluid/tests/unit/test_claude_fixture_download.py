"""Stock CLI fixtures are pinned, checksum-verified, and never overwrite files."""
import hashlib
import io
import json

import pytest

from scripts.ci import fetch_claude_fixture as downloader


def responses(monkeypatch, *, checksum=None, size=None):
    content = b"offline fixture"
    manifest = {"platforms": {"linux-x64": {
        "checksum": checksum or hashlib.sha256(content).hexdigest(),
        "size": len(content) if size is None else size,
    }}}
    seen = []

    def urlopen(url, *, timeout):
        seen.append(url)
        assert timeout > 0
        return io.BytesIO(json.dumps(manifest).encode() if url.endswith("manifest.json") else content)

    monkeypatch.setattr(downloader.urllib.request, "urlopen", urlopen)
    return content, seen


def test_fixture_is_verified_and_executable(tmp_path, monkeypatch):
    content, seen = responses(monkeypatch)
    target = tmp_path / "claude"
    downloader.fetch("2.1.314", target)
    assert target.read_bytes() == content
    assert target.stat().st_mode & 0o700 == 0o700
    assert seen == [f"{downloader.BASE}/2.1.314/manifest.json",
                    f"{downloader.BASE}/2.1.314/linux-x64/claude"]


@pytest.mark.parametrize("bad", ["../latest", "latest", "2.1.314/other"])
def test_fixture_requires_a_literal_release_version(tmp_path, bad):
    with pytest.raises(ValueError, match="release version"):
        downloader.fetch(bad, tmp_path / "claude")


@pytest.mark.parametrize("metadata", [{"checksum": "bad"}, {"size": 1}])
def test_bad_fixture_is_not_left_executable(tmp_path, monkeypatch, metadata):
    responses(monkeypatch, **metadata)
    target = tmp_path / "claude"
    with pytest.raises(ValueError, match="checksum or size"):
        downloader.fetch("2.1.314", target)
    assert not target.exists()


def test_fixture_never_overwrites_an_installed_binary(tmp_path, monkeypatch):
    responses(monkeypatch)
    target = tmp_path / "claude"
    target.write_bytes(b"existing binary")
    with pytest.raises(FileExistsError):
        downloader.fetch("2.1.314", target)
    assert target.read_bytes() == b"existing binary"
