"""``GET/PUT /api/v1/settings/subagent-default`` (#1827, #1830) and the retired agent ``avatar_color``."""
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


class TestRetiredAgentAvatarColor:
    """The avatar feature was removed. Older clients may still send a colour:
    it is accepted, ignored and never returned."""

    @pytest.mark.parametrize("color", ["emerald", None, "red", "#ff0000", 3])
    def test_create_and_update_accept_and_ignore_a_colour(self, authenticated_client, mock_db, color):
        resp = authenticated_client.post("/api/v1/agents", json={
            "name": "Painter", "config": {}, "avatar_color": color})
        assert resp.status_code == 201, resp.text
        agent_id = resp.json()["id"]
        assert "avatar_color" not in resp.json()
        resp = authenticated_client.put(f"/api/v1/agents/{agent_id}", json={"name": "Painter 2", "avatar_color": color})
        assert resp.status_code == 200, resp.text
        assert resp.json()["name"] == "Painter 2" and "avatar_color" not in resp.json()
        assert "avatar_color" not in authenticated_client.get(f"/api/v1/agents/{agent_id}").json()
        assert db.get_profile(agent_id)["avatar_color"] is None

    def test_a_stored_colour_is_kept_but_never_shown(self, authenticated_client, mock_db):
        from app.db.base import get_db

        db.create_profile("peer", "Peer", None, {})
        with get_db() as conn:
            conn.cursor().execute("UPDATE agents SET avatar_color='blue' WHERE id='peer'")
        assert "avatar_color" not in authenticated_client.get("/api/v1/agents/peer").json()
        authenticated_client.put("/api/v1/agents/peer", json={"name": "Peer 2"})
        assert db.get_profile("peer")["avatar_color"] == "blue"

    def test_the_retired_column_still_travels_with_the_agent_row(self):
        # Kept so content hashes and old peers' records stay the same.
        from app.network.policy import COLLECTIONS

        assert "avatar_color" in COLLECTIONS["agents"].fields


def test_upgrade_adds_the_avatar_color_column(mock_db):
    """The column is retired but still added at startup, so upgraded and fresh
    schemas match; existing rows read as no colour."""
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
