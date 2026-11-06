"""Stock CLI fixtures are pinned, checksum-verified, and never overwrite files."""
import hashlib
import io
import json
import urllib.error
from types import SimpleNamespace

import pytest

from scripts.ci import fetch_claude_fixture as downloader
from scripts.ci import publication_read_retry as read_retry


@pytest.fixture(autouse=True)
def backoffs(monkeypatch):
    sleeps = []
    monkeypatch.setattr(read_retry, "time", SimpleNamespace(sleep=sleeps.append))
    return sleeps


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
def test_fixture_requires_a_literal_release_version(tmp_path, monkeypatch, bad, backoffs):
    _, seen = responses(monkeypatch)
    with pytest.raises(ValueError, match="release version"):
        downloader.fetch(bad, tmp_path / "claude")
    assert seen == [] and backoffs == []


@pytest.mark.parametrize("metadata", [{"checksum": "bad"}, {"size": 1}])
def test_bad_fixture_is_not_left_executable(tmp_path, monkeypatch, metadata, backoffs):
    _, seen = responses(monkeypatch, **metadata)
    target = tmp_path / "claude"
    with pytest.raises(ValueError, match="checksum or size"):
        downloader.fetch("2.1.314", target)
    assert not target.exists()
    assert len(seen) == 2 and backoffs == []


def test_fixture_never_overwrites_an_installed_binary(tmp_path, monkeypatch):
    responses(monkeypatch)
    target = tmp_path / "claude"
    target.write_bytes(b"existing binary")
    with pytest.raises(FileExistsError):
        downloader.fetch("2.1.314", target)
    assert target.read_bytes() == b"existing binary"

def failure(kind):
    if kind == "reset":
        return ConnectionResetError("connection reset")
    if kind == "wrapped-reset":
        return urllib.error.URLError(ConnectionResetError("TLS handshake reset"))
    if kind == "timeout":
        return TimeoutError("read timed out")
    if kind == "wrapped-timeout":
        return urllib.error.URLError(TimeoutError("connect timed out"))
    if kind == "certificate":
        return urllib.error.URLError("certificate verify failed")
    return urllib.error.HTTPError("https://fixture.invalid", int(kind), "HTTP error", {}, None)


def failing_responses(monkeypatch, stage, kind, failures):
    content, _ = responses(monkeypatch)
    respond = downloader.urllib.request.urlopen
    attempts = []

    def urlopen(url, *, timeout):
        if url.endswith("manifest.json") == (stage == "manifest"):
            attempts.append(url)
            if len(attempts) <= failures:
                raise failure(kind)
        return respond(url, timeout=timeout)

    monkeypatch.setattr(downloader.urllib.request, "urlopen", urlopen)
    return content, attempts


@pytest.mark.parametrize("stage", ["manifest", "binary"])
@pytest.mark.parametrize("kind", ["reset", "wrapped-reset", "timeout", "wrapped-timeout", "500", "503"])
def test_temporary_fetch_error_recovers(tmp_path, monkeypatch, stage, kind, backoffs):
    content, attempts = failing_responses(monkeypatch, stage, kind, failures=2)
    target = tmp_path / "claude"
    downloader.fetch("2.1.314", target)
    assert target.read_bytes() == content
    assert len(attempts) == 3 and backoffs == [1, 2]


@pytest.mark.parametrize("stage", ["manifest", "binary"])
@pytest.mark.parametrize("kind", ["reset", "wrapped-reset", "timeout", "503"])
def test_temporary_fetch_error_exhausts_its_budget(tmp_path, monkeypatch, stage, kind, backoffs):
    _, attempts = failing_responses(monkeypatch, stage, kind, failures=10)
    target = tmp_path / "claude"
    with pytest.raises(type(failure(kind))):
        downloader.fetch("2.1.314", target)
    assert len(attempts) == 3 and backoffs == [1, 2]
    assert not target.exists()


@pytest.mark.parametrize("stage", ["manifest", "binary"])
@pytest.mark.parametrize("kind", ["400", "401", "404", "429", "certificate"])
def test_permanent_fetch_error_is_not_retried(tmp_path, monkeypatch, stage, kind, backoffs):
    _, attempts = failing_responses(monkeypatch, stage, kind, failures=10)
    target = tmp_path / "claude"
    with pytest.raises(type(failure(kind))):
        downloader.fetch("2.1.314", target)
    assert len(attempts) == 1 and backoffs == []
    assert not target.exists()


@pytest.mark.parametrize("kind", ["reset", "timeout"])
@pytest.mark.parametrize("recover", [False, True])
def test_partial_binary_is_discarded_before_retry(tmp_path, monkeypatch, kind, recover, backoffs):
    content, _ = responses(monkeypatch)
    respond = downloader.urllib.request.urlopen
    attempts = []
    partial = b"bytes from failed attempt"
    target = tmp_path / "claude"

    class Interrupted(io.BytesIO):
        def read(self, size=-1):
            if self.tell():
                raise failure(kind)
            return super().read(size)

    def urlopen(url, *, timeout):
        if not url.endswith("manifest.json"):
            attempts.append(url)
            # urlopen happens only AFTER opening this attempt's empty file.
            assert target.read_bytes() == b""
            if not recover or len(attempts) == 1:
                return Interrupted(partial)
        return respond(url, timeout=timeout)

    monkeypatch.setattr(downloader.urllib.request, "urlopen", urlopen)
    if recover:
        downloader.fetch("2.1.314", target)
        assert target.read_bytes() == content  # also proves the digest was reset
        assert len(attempts) == 2 and backoffs == [1]
    else:
        with pytest.raises(type(failure(kind))):
            downloader.fetch("2.1.314", target)
        assert not target.exists()
        assert len(attempts) == 3 and backoffs == [1, 2]

@pytest.mark.parametrize("kind", ["reset", "timeout"])
def test_manifest_read_failure_is_retried(tmp_path, monkeypatch, kind, backoffs):
    content, _ = responses(monkeypatch)
    respond = downloader.urllib.request.urlopen
    attempts = []

    class Interrupted(io.BytesIO):
        def read(self, size=-1):
            raise failure(kind)

    def urlopen(url, *, timeout):
        if url.endswith("manifest.json"):
            attempts.append(url)
            if len(attempts) == 1:
                return Interrupted()
        return respond(url, timeout=timeout)

    monkeypatch.setattr(downloader.urllib.request, "urlopen", urlopen)
    target = tmp_path / "claude"
    downloader.fetch("2.1.314", target)
    assert target.read_bytes() == content
    assert len(attempts) == 2 and backoffs == [1]


def test_malformed_manifest_is_not_retried(tmp_path, monkeypatch, backoffs):
    seen = []

    def urlopen(url, *, timeout):
        seen.append(url)
        return io.BytesIO(b"not json")

    monkeypatch.setattr(downloader.urllib.request, "urlopen", urlopen)
    target = tmp_path / "claude"
    with pytest.raises(ValueError):
        downloader.fetch("2.1.314", target)
    assert seen == [f"{downloader.BASE}/2.1.314/manifest.json"]
    assert backoffs == [] and not target.exists()
