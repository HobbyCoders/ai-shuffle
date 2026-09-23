"""Routes that used to answer without auth now require it (SEC-09)."""

import pytest


@pytest.mark.parametrize("path", [
    "/api/v1/auth/github/status",
    "/api/v1/documentation",
    "/api/v1/documentation/search?q=setup",
    "/api/v1/documentation/reload",
    "/api/v1/documentation/getting-started",
    "/api/v1/documentation/getting-started/installation",
])
def test_route_rejects_unauthenticated_request(client, path):
    response = client.get(path)
    assert response.status_code in (401, 403), response.text


@pytest.mark.parametrize("path", ["/openapi.json", "/docs", "/redoc"])
def test_api_docs_are_not_served(client, path):
    response = client.get(path)
    assert '"openapi"' not in response.text
    assert "swagger-ui" not in response.text.lower()
    assert "redoc.standalone" not in response.text


def test_documentation_still_works_when_authenticated(authenticated_client):
    response = authenticated_client.get("/api/v1/documentation")
    assert response.status_code == 200
    assert "categories" in response.json()
