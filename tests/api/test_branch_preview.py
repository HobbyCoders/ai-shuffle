"""Branch preview uses real local-listener auth, not a permissive proxy fixture."""
from contextlib import asynccontextmanager
import json
import subprocess

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app import preview
from app.core.config import settings
from app.serve import IngressApplication


@pytest.fixture
def checkout(tmp_path):
    root = tmp_path / "checkout"
    root.mkdir()
    subprocess.run(["git", "init", "-b", "preview-fixture", str(root)], check=True, capture_output=True)
    (root / ".gitignore").write_text("frontend/build/\n", encoding="utf-8")
    source = root / "frontend" / "src"
    source.mkdir(parents=True)
    (source / "app.js").write_text("branch source", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "fixture"], check=True, capture_output=True)
    build = root / "frontend" / "build"
    (build / "_app").mkdir(parents=True)
    (build / "icons").mkdir()
    for name, content in {"index.html": "<title>Branch fixture</title><button>Branch UI</button>", "_app/app.js": "branch bundle", "icons/icon.svg": "<svg/>", "sw.js": "// fixture", "favicon.svg": "<svg/>", "manifest.json": "{}"}.items():
        (build / name).write_text(content, encoding="utf-8")
    preview.stamp_build(root)
    return root


@pytest.fixture
def app(checkout, monkeypatch):
    monkeypatch.setenv("AI_SHUFFLE_PREVIEW", "1")
    monkeypatch.setattr(settings, "shuffle_local_access", True)
    app = FastAPI()
    assert preview.install_preview(app, checkout)
    return IngressApplication(app, local_port=18090)


def client(app, port=18090, peer="127.0.0.1"):
    return TestClient(app, base_url=f"http://127.0.0.1:{port}", client=(peer, 54321))


def test_identity_and_assets_are_the_stamped_branch(app, checkout):
    c = client(app)
    response = c.get("/api/preview")
    assert response.status_code == 200
    identity = response.json()
    assert identity == json.loads((checkout / "frontend/build" / preview.MANIFEST).read_text())
    assert identity["branch"] == "preview-fixture"
    assert str(checkout) not in response.text
    for path, content in (("/plaid-link?oauth_state_id=resume", "Branch UI"), ("/chat/real-session", "Branch UI"), ("/_app/app.js", "branch bundle"), ("/static/index.html", "Branch UI"), ("/icons/icon.svg", "<svg/>"), ("/favicon.svg?filename=../../.git/config", "<svg/>")):
        page = c.get(path)
        assert page.status_code == 200
        assert content in page.text
        assert page.headers["x-ai-shuffle-preview"] == identity["build_sha256"]
        assert page.headers["cache-control"] == "no-store"
    assert c.get("/_app/missing.js").status_code == 404
    assert c.get("/api/not-a-route").status_code == 404


@pytest.mark.parametrize("header", ["Forwarded", "X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto", "CF-Connecting-IP", "CF-Ray"])
@pytest.mark.parametrize("path", ["/api/preview", "/chat", "/_app/app.js", "/static/index.html", "/sw.js"])
def test_proxy_headers_cannot_borrow_local_owner_auth(app, header, path):
    # Even an empty forwarded header disqualifies transport-derived authority.
    assert client(app).get(path, headers={header: ""}).status_code == 403


@pytest.mark.parametrize("port,peer,headers", [
    (8001, "127.0.0.1", {}),
    (18091, "127.0.0.1", {}),
    (18090, "203.0.113.4", {}),
    (18090, "127.0.0.1", {"Host": "untrusted.example"}),
    (18090, "127.0.0.1", {"Origin": "https://untrusted.example"}),
    (18090, "127.0.0.1", {"Sec-Fetch-Site": "cross-site"}),
    (18090, "127.0.0.1", {"Authorization": "Bearer not-an-owner"}),
])
def test_remote_ingress_and_forged_authority_are_denied(app, port, peer, headers):
    for path in ("/api/preview", "/chat", "/_app/app.js"):
        assert client(app, port=port, peer=peer).get(path, headers=headers).status_code == 403


def test_preview_disabled_does_not_inspect_build_or_change_app(tmp_path, monkeypatch):
    monkeypatch.delenv("AI_SHUFFLE_PREVIEW", raising=False)
    app = FastAPI()
    routes = list(app.routes)
    assert preview.install_preview(app, tmp_path) is False
    assert app.routes == routes


@pytest.mark.parametrize("changed", ["source", "build", "branch", "missing-stamp"])
def test_stale_or_unstamped_build_fails_closed(checkout, monkeypatch, changed):
    monkeypatch.setenv("AI_SHUFFLE_PREVIEW", "1")
    if changed == "source":
        (checkout / "frontend/src/app.js").write_text("changed source")
    elif changed == "build":
        (checkout / "frontend/build/_app/app.js").write_text("changed output")
    elif changed == "branch":
        subprocess.run(["git", "-C", str(checkout), "checkout", "-b", "another-branch"], check=True, capture_output=True)
    else:
        (checkout / "frontend/build" / preview.MANIFEST).unlink()
    with pytest.raises(ValueError, match="stamp"):
        preview.install_preview(FastAPI(), checkout)


def test_preview_preserves_existing_lifespan_and_api_routes(checkout, monkeypatch):
    monkeypatch.setenv("AI_SHUFFLE_PREVIEW", "1")
    calls = []

    @asynccontextmanager
    async def lifespan(app):
        calls.append("start")
        yield
        calls.append("stop")

    app = FastAPI(lifespan=lifespan)

    @app.get("/api/existing")
    async def existing():
        return {"existing": True}

    assert preview.install_preview(app, checkout)
    assert calls == []  # installing the UI never starts a worker
    with client(IngressApplication(app, 18090)) as c:
        assert c.get("/api/existing").json() == {"existing": True}
        assert calls == ["start"]
    assert calls == ["start", "stop"]
