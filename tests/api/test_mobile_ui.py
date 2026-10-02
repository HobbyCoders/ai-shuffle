"""The worker serves the phone UI at /mobile/; the Android and iOS apps load it from there."""
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app import mobile_ui


@pytest.fixture
def build(tmp_path):
    directory = tmp_path / "static" / "mobile"
    (directory / "_app" / "immutable").mkdir(parents=True)
    (directory / "index.html").write_text("<title>Phone UI</title>", encoding="utf-8")
    (directory / "_app" / "immutable" / "start.js").write_text("phone bundle", encoding="utf-8")
    (directory / "robots.txt").write_text("phone robots", encoding="utf-8")
    (tmp_path / "static" / "secret.txt").write_text("outside the phone build", encoding="utf-8")
    return directory


@pytest.fixture
def client(build):
    app = FastAPI()
    assert mobile_ui.install_mobile_ui(app, build)
    return TestClient(app)


def test_every_ui_route_gets_the_entry_page(client):
    for path in ("/mobile/", "/mobile/chat/abc", "/mobile/settings", "/mobile/demo"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert "Phone UI" in response.text
        assert response.headers["content-type"].startswith("text/html")


def test_bare_path_redirects_to_the_slash_and_keeps_the_query(client):
    response = client.get("/mobile?session=abc", follow_redirects=False)
    assert response.status_code == 308
    assert response.headers["location"] == "/mobile/?session=abc"


def test_hashed_assets_and_real_files_are_served(client):
    assert client.get("/mobile/_app/immutable/start.js").text == "phone bundle"
    assert client.get("/mobile/robots.txt").text == "phone robots"


def test_missing_files_are_404_not_the_entry_page(client):
    assert client.get("/mobile/_app/immutable/missing.js").status_code == 404
    assert client.get("/mobile/missing.png").status_code == 404


@pytest.mark.parametrize("path", ["/mobile/../secret.txt", "/mobile/%2e%2e/secret.txt", "/mobile/..%2fsecret.txt"])
def test_paths_cannot_leave_the_build(client, path):
    response = client.get(path)
    assert "outside the phone build" not in response.text


def test_resolve_refuses_escapes(build):
    assert mobile_ui.resolve(build, "../secret.txt") is None
    assert mobile_ui.resolve(build, "") == build.resolve() / "index.html"
    assert mobile_ui.resolve(build, "chat/x") == build.resolve() / "index.html"


def test_no_build_installs_nothing(tmp_path):
    app = FastAPI()
    assert not mobile_ui.install_mobile_ui(app, tmp_path / "missing")
    assert TestClient(app).get("/mobile/").status_code == 404


def test_the_frontend_build_emits_the_phone_ui():
    """Every install path copies frontend/build into app/static, so the build must produce build/mobile."""
    import json
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    scripts = json.loads((root / "frontend/package.json").read_text())["scripts"]
    assert "build-mobile-ui.mjs" in scripts["postbuild"]
    assert "paths" in (root / "mobile/svelte.config.js").read_text()
    assert "base: '/mobile'" in (root / "mobile/svelte.config.js").read_text()
