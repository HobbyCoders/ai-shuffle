"""Provider settings import (``/api/settings/import``) end to end.

The real preview/apply service and Fluid's copier run against temporary
Claude and Codex homes: the source is never modified, an existing destination
is never overwritten, only ready items from a fresh preview are applied, and
outside local mode the source home is confined to the configured mount.
"""
from pathlib import Path

import pytest

from app.api.auth import hash_api_key
from app.core import settings_import_service as service
from app.db import database

URL = "/api/settings/import"
USER_KEY = "aih_import_user_key"


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def homes(tmp_path, monkeypatch):
    source = tmp_path / "legacy home"
    destination = tmp_path / "fluid home"
    workspace = tmp_path / "workspace"
    for path in (source, destination, workspace):
        path.mkdir()
    monkeypatch.setenv("SETTINGS_IMPORT_SOURCE_HOME", str(source))
    monkeypatch.setenv("SETTINGS_IMPORT_DESTINATION_HOME", str(destination))
    monkeypatch.delenv("SETTINGS_IMPORT_ALLOWED_HOMES", raising=False)
    monkeypatch.setattr(service, "is_local_mode", lambda: True)
    monkeypatch.setattr(service.settings, "workspace_dir", workspace)
    _write(source / ".claude" / "skills" / "fresh" / "SKILL.md", "---\nname: fresh\n---\nnew skill")
    _write(source / ".claude" / "skills" / "taken" / "SKILL.md", "---\nname: taken\n---\nlegacy copy")
    _write(destination / ".fluid" / "skills" / "taken" / "SKILL.md", "native copy")
    _write(source / ".codex" / "AGENTS.md", "codex rules")
    return source, destination, workspace


@pytest.fixture
def c(authenticated_client, homes):
    return authenticated_client


@pytest.fixture
def api_user(mock_db):
    database.create_api_user("import-user", "Import User", hash_api_key(USER_KEY))
    return {"Authorization": f"Bearer {USER_KEY}"}


def _items(preview: dict) -> dict:
    return {(item["source_system"], Path(item["source"]).name): item for item in preview["items"]}


def test_preview_lists_claude_and_codex_items_without_touching_anything(c, homes):
    source, destination, _ = homes
    r = c.post(f"{URL}/preview", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["providers"] == ["codex", "claude"]
    assert body["source_display_path"] == str(source.resolve())
    assert body["destination_display_path"] == str(destination.resolve() / ".fluid")
    assert body["source_untouched"] is True and body["overwrite_existing"] is False
    assert body["token"] == body["hash"] and len(body["token"]) == 64
    items = _items(body)
    assert items[("claude", "fresh")]["status"] == "ready"
    assert items[("claude", "taken")]["status"] == "conflict"
    assert items[("codex", "AGENTS.md")]["status"] == "ready"
    assert "native destination already exists; it wins" in body["conflicts"]
    public = {i["id"]: i for cat in body["categories"] for i in cat["items"]}
    assert public[items[("claude", "taken")]["id"]]["selectable"] is False
    assert public[items[("claude", "fresh")]["id"]]["selectable"] is True
    assert not (destination / ".fluid" / "skills" / "fresh").exists()
    assert not (destination / ".fluid" / "AGENTS.md").exists()


def test_apply_copies_ready_items_and_leaves_source_alone(c, homes):
    source, destination, _ = homes
    preview = c.post(f"{URL}/preview", json={}).json()
    items = _items(preview)
    selected = [items[("claude", "fresh")]["id"], items[("codex", "AGENTS.md")]["id"]]
    r = c.post(f"{URL}/apply", json={"token": preview["token"], "selected_item_ids": selected})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source_untouched"] is True and body["overwrite_existing"] is False
    assert body["errors"] == [] and body["imported_projects"] == []
    assert len(body["imported"]) == 2
    assert (destination / ".fluid" / "skills" / "fresh" / "SKILL.md").read_text() == "---\nname: fresh\n---\nnew skill"
    assert (destination / ".fluid" / "AGENTS.md").read_text() == "codex rules"
    assert (source / ".codex" / "AGENTS.md").read_text() == "codex rules"
    assert (source / ".claude" / "skills" / "fresh" / "SKILL.md").exists()


def test_apply_never_overwrites_an_existing_destination(c, homes):
    _, destination, _ = homes
    preview = c.post(f"{URL}/preview", json={}).json()
    taken = _items(preview)[("claude", "taken")]["id"]
    r = c.post(f"{URL}/apply", json={"token": preview["token"], "selected_item_ids": [taken]})
    assert r.status_code == 400
    assert r.json()["detail"] == "Only ready items from the current preview can be imported"
    assert (destination / ".fluid" / "skills" / "taken" / "SKILL.md").read_text() == "native copy"


def test_apply_refuses_ids_not_in_the_preview(c):
    preview = c.post(f"{URL}/preview", json={}).json()
    r = c.post(f"{URL}/apply", json={"token": preview["token"], "selected_item_ids": ["../../etc/passwd"]})
    assert r.status_code == 400
    assert r.json()["detail"] == "The selection contains items that were not in the preview"


def test_apply_with_a_stale_preview_is_409_and_copies_nothing(c, homes):
    source, destination, _ = homes
    preview = c.post(f"{URL}/preview", json={}).json()
    fresh = _items(preview)[("claude", "fresh")]["id"]
    _write(source / ".claude" / "skills" / "fresh" / "SKILL.md", "changed after preview")
    r = c.post(f"{URL}/apply", json={"token": preview["token"], "selected_item_ids": [fresh]})
    assert r.status_code == 409
    assert r.json()["detail"] == "Provider settings changed after preview; preview again before importing"
    assert not (destination / ".fluid" / "skills" / "fresh").exists()


def test_apply_requires_a_token(c):
    assert c.post(f"{URL}/apply", json={"token": "", "selected_item_ids": []}).status_code == 422


@pytest.mark.parametrize("body,detail", [
    ({"providers": ["cursor"]}, "Unsupported settings provider: cursor"),
    ({"source_home": "/definitely/not/here"}, "Settings import home does not exist:"),
])
def test_preview_rejects_unknown_provider_or_missing_home(c, body, detail):
    r = c.post(f"{URL}/preview", json=body)
    assert r.status_code == 400
    assert r.json()["detail"].startswith(detail)


def test_docker_mode_confines_the_source_to_the_configured_mount(c, homes, tmp_path, monkeypatch):
    source, destination, _ = homes
    monkeypatch.setattr(service, "is_local_mode", lambda: False)
    elsewhere = tmp_path / "someone else"
    _write(elsewhere / ".claude" / "skills" / "stolen" / "SKILL.md", "---\nname: stolen\n---\n")
    for route in ("preview", "claude/preview"):
        r = c.post(f"{URL}/{route}", json={"source_home": str(elsewhere)})
        assert r.status_code == 400, route
        assert r.json()["detail"] == "Docker imports are limited to the configured read-only settings import mount"
    # A traversal that resolves outside the mount is refused the same way.
    sneaky = str(source / ".." / "someone else")
    assert c.post(f"{URL}/preview", json={"source_home": sneaky}).status_code == 400
    r = c.post(f"{URL}/apply", json={"token": "x" * 64, "selected_item_ids": [], "source_home": str(elsewhere)})
    assert r.status_code == 400
    assert not (destination / ".fluid" / "skills" / "stolen").exists()
    # The configured mount itself is allowed, as is an extra allowed home.
    assert c.post(f"{URL}/preview", json={"source_home": str(source)}).status_code == 200
    monkeypatch.setenv("SETTINGS_IMPORT_ALLOWED_HOMES", str(elsewhere))
    assert c.post(f"{URL}/preview", json={"source_home": str(elsewhere)}).status_code == 200


def test_docker_mode_does_not_register_projects_outside_the_workspace(c, homes, tmp_path, monkeypatch):
    source, _, workspace = homes
    monkeypatch.setattr(service, "is_local_mode", lambda: False)
    outside = tmp_path / "outside project"
    inside = workspace / "inside project"
    outside.mkdir()
    inside.mkdir()
    _write(source / ".claude.json", '{"projects": {%s: {}, %s: {}}}' % (
        f'"{outside}"', f'"{inside}"'))
    preview = c.post(f"{URL}/preview", json={"providers": ["claude"]}).json()
    projects = {Path(i["source"]).name: i for i in preview["items"] if i["kind"] == "project_registration"}
    assert projects["outside project"]["status"] == "diagnostic"
    assert projects["outside project"]["message"] == "Project is outside the mounted Docker workspace"
    assert projects["inside project"]["status"] == "ready"
    r = c.post(f"{URL}/apply", json={
        "token": preview["token"], "selected_item_ids": [projects["inside project"]["id"]], "providers": ["claude"]})
    assert r.status_code == 200, r.text
    [registered] = r.json()["imported_projects"]
    assert registered["status"] == "imported"
    assert database.get_project(registered["project_id"])["path"] == "inside project"


def test_legacy_claude_routes_only_see_claude(c, homes):
    _, destination, _ = homes
    preview = c.post(f"{URL}/claude/preview", json={"providers": ["codex"]}).json()
    assert preview["providers"] == ["claude"]
    assert {i["source_system"] for i in preview["items"]} == {"claude"}
    fresh = _items(preview)[("claude", "fresh")]["id"]
    r = c.post(f"{URL}/claude/apply", json={"token": preview["token"], "selected_item_ids": [fresh]})
    assert r.status_code == 200
    assert (destination / ".fluid" / "skills" / "fresh" / "SKILL.md").exists()
    stale = c.post(f"{URL}/claude/apply", json={"token": "0" * 64, "selected_item_ids": []})
    assert stale.status_code == 409
    bad = c.post(f"{URL}/claude/apply", json={"token": preview["token"], "selected_item_ids": ["nope"]})
    assert bad.status_code in (400, 409)


@pytest.mark.parametrize("route", ["preview", "apply", "claude/preview", "claude/apply"])
def test_import_is_owner_only(client, api_user, homes, route):
    _, destination, _ = homes
    r = client.post(f"{URL}/{route}", json={"token": "t", "selected_item_ids": []}, headers=api_user)
    assert r.status_code == 403
    assert "fresh" not in r.text
    assert not (destination / ".fluid" / "skills" / "fresh").exists()
