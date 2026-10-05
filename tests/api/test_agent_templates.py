"""GET /api/v1/agents/templates (ruling A12): built-in prompts as templates."""


def test_templates_listed(authenticated_client):
    resp = authenticated_client.get("/api/v1/agents/templates")
    assert resp.status_code == 200
    templates = resp.json()
    ids = {t["id"] for t in templates}
    assert "general-purpose" in ids and "Plan" in ids
    assert "Explore" not in ids
    for t in templates:
        assert t["prompt"], f"template {t['id']} has an empty prompt"
        assert "name" in t and "description" in t


def test_templates_route_not_shadowed_by_profile_lookup(authenticated_client):
    """/templates must not be swallowed by /{profile_id}."""
    resp = authenticated_client.get("/api/v1/agents/templates")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)
