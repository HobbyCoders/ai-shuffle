"""A signed UI bundle replaces compilation, never source verification."""
import hashlib
import io
import json
import zipfile

import pytest

from docker import application_updates as updates
from tests.test_application_updates import B, feed
from tests.test_docker_dependency_reuse import build_feed, npm_installs

VERSION = "1.1.0-dev.1"


def bundle(revision=B, version=VERSION):
    files = {"index.html": "verified desktop UI", "mobile/index.html": "verified phone UI",
             "ui-build.json": json.dumps({"format": 1, "revision": revision, "version": version})}
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, content in files.items():
            archive.writestr(f"ai-shuffle-{B}/{name}", content)
    data = output.getvalue()
    descriptor = {"format": 1, "revision": B, "version": VERSION,
                  "sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
                  "tree": hashlib.sha256(updates.tree_lines([(name, hashlib.sha256(text.encode()).hexdigest())
                                                            for name, text in files.items()])).hexdigest()}
    return data, descriptor


def serve(feed, data, descriptor):
    name = f"{B}-{descriptor['sha256']}-ui.zip"
    feed[4][f"/HobbyCoders/ai-shuffle/releases/download/desktop-sources/{name}"] = data


def test_signed_bundle_hit_runs_no_local_frontend_install_or_build(build_feed, feed):
    updater, stage, commands = build_feed
    data, ui = bundle()
    serve(feed, data, ui)
    stage(B, 2, ui=ui, version=VERSION)
    assert "frontend" not in npm_installs(commands)
    assert not any(command == ["npm", "run", "build"] and cwd.name == "frontend" for command, cwd in commands)
    static = updater.directory(B) / "source/app/static"
    assert (static / "index.html").read_text() == "verified desktop UI"
    assert (static / "mobile/index.html").read_text() == "verified phone UI"
    assert any(request.url.host == "github.com" for request in feed[3])
    assert all("authorization" not in request.headers for request in feed[3])


@pytest.mark.parametrize("failure", ["digest", "revision", "version", "inner-revision", "missing", "offline", "tree"])
def test_invalid_or_unavailable_bundle_falls_back_before_staging(build_feed, feed, failure):
    updater, stage, commands = build_feed
    data, ui = bundle(revision="c" * 40 if failure == "inner-revision" else B)
    if failure in ("digest", "tree"):
        ui[{"digest": "sha256", "tree": "tree"}[failure]] = "f" * 64
    if failure == "revision":
        ui["revision"] = "c" * 40
    if failure == "version":
        ui["version"] = "9.9.9"
    if failure != "offline":
        serve(feed, data, ui)
    stage(B, 2, ui=None if failure == "missing" else ui, version=VERSION)
    assert "frontend" in npm_installs(commands)
    assert any(command == ["npm", "run", "build"] and cwd.name == "frontend" for command, cwd in commands)
    assert (updater.directory(B) / "source/app/static/index.html").read_text() == B
    assert updater.state["pending"] == B


def test_ui_kill_switch_does_not_download_optional_bundle(build_feed, feed, monkeypatch):
    updater, stage, commands = build_feed
    data, ui = bundle()
    serve(feed, data, ui)
    monkeypatch.setenv("AI_SHUFFLE_PERF_PREBUILT_UI", "0")
    stage(B, 2, ui=ui, version=VERSION)
    assert "frontend" in npm_installs(commands)
    assert all(request.url.host != "github.com" for request in feed[3])
