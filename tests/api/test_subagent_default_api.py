"""``GET/PUT /api/v1/settings/subagent-default`` (#1827, #1830) and agent ``avatar_color``."""
import pytest

from app.db import database as db


ENDPOINT = "/api/v1/settings/subagent-default"


def _provider():
    db.create_model_provider(
        "lane", "Lane", "openai", "https://example.invalid/v1",
        models=[{"id": "astra"}, {"id": "sol"}],
    )


class TestSubagentDefaultApi:
    @pytest.mark.parametrize("method", ["get", "put"])
    def test_requires_auth(self, client, method):
        resp = getattr(client, method)(ENDPOINT, **({"json": {}} if method == "put" else {}))
        assert resp.status_code in (401, 403)

    def test_unset_by_default(self, authenticated_client, mock_db):
        assert authenticated_client.get(ENDPOINT).json() == {"model": None}

    def test_put_roundtrip_normalizes(self, authenticated_client, mock_db):
        _provider()
        resp = authenticated_client.put(ENDPOINT, json={"model": "claude-haiku-5-5"})
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"model": "haiku"}
        assert authenticated_client.get(ENDPOINT).json() == resp.json()

    def test_an_older_pages_allowed_list_is_accepted_and_ignored(self, authenticated_client, mock_db):
        """The "models agents may choose from" list is gone (#1830)."""
        _provider()
        resp = authenticated_client.put(ENDPOINT, json={
            "model": "prov:lane:sol", "allowed": ["opus", "prov:nowhere:sol"]})
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"model": "prov:lane:sol"}
        assert "allowed" not in authenticated_client.get(ENDPOINT).json()

    def test_null_model_clears_it(self, authenticated_client, mock_db):
        authenticated_client.put(ENDPOINT, json={"model": "sonnet"})
        body = authenticated_client.put(ENDPOINT, json={"model": None}).json()
        assert body == {"model": None}

    @pytest.mark.parametrize("body", [
        {"model": "gpt-5"},
        {"model": "prov:lane:unlisted"},
        {"model": "prov:nowhere:sol"},
    ])
    def test_invalid_ids_are_400_and_nothing_is_saved(self, authenticated_client, mock_db, body):
        _provider()
        authenticated_client.put(ENDPOINT, json={"model": "opus"})
        resp = authenticated_client.put(ENDPOINT, json=body)
        assert resp.status_code == 400
        assert authenticated_client.get(ENDPOINT).json()["model"] == "opus"

    def test_shape_errors_are_422(self, authenticated_client, mock_db):
        resp = authenticated_client.put(ENDPOINT, json={"model": ["sonnet"]})
        assert resp.status_code == 422

    def test_the_setting_is_shared_across_workers(self):
        from app.core.subagent_default import SETTING_KEY
        from app.network.policy import is_shared_setting_key

        assert is_shared_setting_key(SETTING_KEY)


class TestAgentAvatarColor:
    def test_create_read_update_and_clear(self, authenticated_client, mock_db):
        resp = authenticated_client.post("/api/v1/agents", json={
            "name": "Painter", "config": {}, "avatar_color": "emerald"})
        assert resp.status_code == 201, resp.text
        agent_id = resp.json()["id"]
        assert resp.json()["avatar_color"] == "emerald"
        assert authenticated_client.get(f"/api/v1/agents/{agent_id}").json()["avatar_color"] == "emerald"

        # Omitted keeps it; a name-only update does not clear the colour.
        body = authenticated_client.put(f"/api/v1/agents/{agent_id}", json={"name": "Painter 2"}).json()
        assert body["avatar_color"] == "emerald"
        body = authenticated_client.put(f"/api/v1/agents/{agent_id}", json={"avatar_color": "rose"}).json()
        assert body["avatar_color"] == "rose"
        body = authenticated_client.put(f"/api/v1/agents/{agent_id}", json={"avatar_color": None}).json()
        assert body["avatar_color"] is None

    @pytest.mark.parametrize("color", ["red", "#ff0000", "Emerald", 3])
    def test_unknown_colours_are_refused(self, authenticated_client, mock_db, color):
        resp = authenticated_client.post("/api/v1/agents", json={
            "name": "Bad", "config": {}, "avatar_color": color})
        assert resp.status_code == 422
        db.create_profile("ok", "Ok", None, {})
        resp = authenticated_client.put("/api/v1/agents/ok", json={"avatar_color": color})
        assert resp.status_code == 422

    def test_allowed_colours_match_the_frontend_accent_tokens(self):
        """Every allowed name is a colour token app.css defines for both themes."""
        from pathlib import Path

        from app.core.models import AGENT_AVATAR_COLORS

        css = (Path(__file__).resolve().parents[2] / "frontend" / "src" / "app.css").read_text(
            encoding="utf-8")
        for name in AGENT_AVATAR_COLORS:
            assert css.count(f"--{name}:") >= 2, name

    def test_an_unknown_stored_colour_reads_as_null(self, authenticated_client, mock_db):
        db.create_profile("peer", "Peer", None, {}, avatar_color="ultraviolet")
        assert authenticated_client.get("/api/v1/agents/peer").json()["avatar_color"] is None

    def test_the_colour_syncs_with_the_agent_row(self):
        from app.network.policy import COLLECTIONS

        assert "avatar_color" in COLLECTIONS["agents"].fields


def test_upgrade_adds_the_avatar_color_column(mock_db):
    """An install whose agents table predates the column gets it at startup;
    existing rows read as no colour."""
    from app.db.base import column_exists, get_db
    from app.db import database

    db.create_profile("old", "Old", None, {})
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("ALTER TABLE agents DROP COLUMN avatar_color")
        assert not column_exists(cursor, "agents", "avatar_color")
        database._run_column_migrations(cursor)
        assert column_exists(cursor, "agents", "avatar_color")
    assert db.get_profile("old")["avatar_color"] is None
