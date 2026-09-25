"""MCP server ``args`` are masked for a non-owner, like env and header values.

A stdio server's command line routinely carries a token (``--token=...``,
``--api-key KEY``, ``-H "Authorization: Bearer ..."``, ``API_KEY=... node``),
and the list routes returned it verbatim to API keys and non-owner devices.
"""

import pytest

from app.api.auth import hash_api_key
from app.api.mcp import _SECRET_MASK as M, _mask_args
from app.db import database

KEY = "aih_mcp_args_masking_key"
AUTH = {"Authorization": f"Bearer {KEY}"}
GH = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"


@pytest.mark.parametrize(("args", "expected"), [
    (["--token=" + GH], ["--token=" + M]),
    (["--api-key", "k-123"], ["--api-key", M]),
    (["--client-secret", "s"], ["--client-secret", M]),
    (["-H", "Authorization: Bearer abc"], ["-H", "Authorization: " + M]),
    (["--header", "Authorization", "Bearer abc"], ["--header", "Authorization", M]),
    (["--header=Authorization: abc"], ["--header=Authorization: " + M]),
    (["--env=OPENAI_API_KEY=sk-x"], ["--env=OPENAI_API_KEY=" + M]),
    (["API_KEY=v", "node", "s.js"], ["API_KEY=" + M, "node", "s.js"]),
    (["X-Api-Key: v"], ["X-Api-Key: " + M]),
    (["--db", "postgres://u:pw@h/db"], ["--db", "postgres://***@h/db"]),
    (["https://h/cb?access_token=abc&x=1"], ["https://h/cb?access_token=" + M + "&x=1"]),
    (["serve", GH], ["serve", M]),  # a bare known-prefix token
    (["aB3dE5fG7hJ9kL1mN3pQ5rS7tU9vW1xY"], [M]),  # a long high-entropy value
])
def test_secret_looking_args_are_masked(args, expected):
    assert _mask_args(args) == expected


@pytest.mark.parametrize("args", [
    ["-y", "@modelcontextprotocol/server-filesystem", "/home/user/projects"],
    ["--key-file", "/etc/ssl/k.pem", "--author", "Jane", "--port", "8080"],
    ["--model=claude-sonnet-4-5-20250929", "src/components/Button.svelte"],
    ["--url", "https://api.example.com/v1", "uvx", "mcp-server-fetch==1.2.3"],
    ["--verbose", "--token", "--other"],  # a bare flag masks no following flag
])
def test_ordinary_args_stay_readable(args):
    assert _mask_args(args) == args


def _servers(tmp_path, monkeypatch):
    from app.core import mcp_service
    from app.core.mcp_service import McpScope, McpServerConfig, McpServerType

    monkeypatch.setattr(mcp_service, "_get_claude_dir", lambda: tmp_path / ".claude")
    monkeypatch.setattr(mcp_service, "_get_user_claude_json", lambda: tmp_path / ".claude.json")
    monkeypatch.setattr("app.api.mcp.get_mcp_service", lambda *a, **k: mcp_service.McpService(*a, **k))
    mcp_service.McpService().add_server("gh", McpServerConfig(
        name="gh", type=McpServerType.STDIO, command="npx",
        args=["-y", "gh-mcp", "--token=" + GH, "--api-key", "OWNER_ARG_SECRET"], scope="user",
    ), McpScope.USER)


def test_api_key_sees_masked_args(client, mock_db, tmp_path, monkeypatch):
    _servers(tmp_path, monkeypatch)
    database.create_api_user("u1", "U1", hash_api_key(KEY), project_id=None, profile_ids=None)

    r = client.get("/api/v1/mcp/servers", headers=AUTH)

    assert r.status_code == 200
    assert GH not in r.text and "OWNER_ARG_SECRET" not in r.text
    [server] = [s for s in r.json() if s["name"] == "gh"]
    assert server["args"] == ["-y", "gh-mcp", "--token=" + M, "--api-key", M]


def test_owner_sees_args_verbatim(client, mock_db, tmp_path, monkeypatch):
    from app.api import auth

    _servers(tmp_path, monkeypatch)
    monkeypatch.setattr(auth.owner_auth, "local_access", lambda request: True)

    r = client.get("/api/v1/mcp/servers")

    assert r.status_code == 200
    assert GH in r.text and "OWNER_ARG_SECRET" in r.text
