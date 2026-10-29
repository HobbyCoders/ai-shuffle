"""PR #840 regressions: durable pins, per-turn accounts and bound media jobs."""

import ast
import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.core import credential_gate, encryption, query_options
from app.core import query_engine as qe
from app.core.agent_provider_models import available_subagents
from app.core.provider import (
    fluid_credentials,
    harness_adapter,
    native_video,
    supergrok_images,
)
from app.core.provider.harness_adapter import HarnessAdapter
from app.db import database as db
from scripts.devloop.fake_wire import Chat, FakeProviderServer
from tests.core.engine_harness import StubPool
from tests.core.helpers import make_profile

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def accounts(mock_db: Any) -> None:
    """Two saved OpenRouter keys in one slot; "old" starts as the active one."""
    encryption.set_encryption_key("test-password", db)
    for account in ("old", "active"):
        db.create_model_provider(
            account,
            account,
            "openrouter",
            "https://openrouter.ai/api",
            auth_token=encryption.encrypt_value("key-" + account),
            models=[{"id": "same-model", "context_window": 200000}],
            default_model="same-model",
            enabled=account == "old",
        )


def test_default_provider_pin_selects_provider_task_map(
    accounts: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A default-provider turn pins Task to that provider's model map, not the native one."""
    monkeypatch.setattr(credential_gate, "claude_credential_ready", lambda *a: (False, "off"))
    caller = make_profile("unbound", {"cwd": str(tmp_path), "enabled_agents": ["worker"]})
    make_profile(
        "worker",
        {
            "provider_models": {
                "old": "prov:old:same-model",
                "__native__": "sonnet",
            }
        },
    )
    db.create_session("first-turn", profile_id="unbound")
    with patch.object(query_options, "get_security_instructions", return_value=""):
        options, _ = query_options.build_options_from_profile(
            profile=caller, session_id="first-turn"
        )
    assert options.aih_provider_id == "old"
    targets = available_subagents(caller["id"], options.aih_provider_pin_id)
    assert [(t.id, t.model) for t in targets] == [("worker", "same-model")]
    assert options.aih_provider_pin_id == "old"
    db.set_model_provider_enabled("active", True)
    with patch.object(query_options, "get_security_instructions", return_value=""):
        following, _ = query_options.build_options_from_profile(
            profile=caller, session_id="first-turn"
        )
    assert following.aih_provider_id == "active"
    assert following.aih_provider_pin_id == "old"
    assert available_subagents(caller["id"], following.aih_provider_pin_id)[0].model == "same-model"


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["websocket", "headless"])
async def test_late_switch_books_actual_credential_account(
    accounts: None, mock_db: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine: str
) -> None:
    """A switch before the lazy connect books usage on the account that actually ran."""
    # Exercise both real query engines and their unmocked settlement/attribution.
    # Only the upstream model output, profile build and unrelated memory IO are substituted.
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    with FakeProviderServer(Chat.text("active account answered")) as server:
        for account in ("old", "active"):
            db.update_model_provider(account, base_url=server.url)
        profile = make_profile(
            "late-switch",
            {
                "model": "same-model",
                "provider": "old",
                "cwd": str(tmp_path),
            },
        )
        sid = "late-" + engine
        db.create_session(sid, profile_id=profile["id"])
        db.set_session_run_binding(sid, model="prov:old:same-model")
        with patch.object(query_options, "get_security_instructions", return_value=""):
            options, _ = query_options.build_options_from_profile(profile=profile, session_id=sid)
        options.mcp_servers = {}
        assert options.aih_provider_id == "old"
        original = harness_adapter.resolve_provider_credentials

        async def switch_before_credentials(kind, provider_id):
            db.set_model_provider_enabled("active", True)
            return await original(kind, provider_id)

        monkeypatch.setattr(
            harness_adapter, "resolve_provider_credentials", switch_before_credentials
        )
        monkeypatch.setattr(qe, "get_profile", lambda *a: profile)
        monkeypatch.setattr(
            "app.core.sdk_options.build_session_options", lambda **kw: (options, None)
        )
        monkeypatch.setattr(qe, "reconcile_memory", AsyncMock())
        monkeypatch.setattr(qe, "push_memory", AsyncMock())
        adapter = HarnessAdapter()
        monkeypatch.setattr("app.core.provider.factory.make_adapter", lambda: adapter)
        monkeypatch.setattr(qe, "get_session_pool", lambda: StubPool())
        try:
            if engine == "headless":
                result = await asyncio.wait_for(
                    qe.execute_query("hello", profile["id"], session_id=sid), 20
                )
                assert not result["metadata"].get("is_error"), result
            else:

                async def stream():
                    return [
                        event
                        async for event in qe._stream_to_websocket_impl("hello", sid, profile["id"])
                    ]

                events = await asyncio.wait_for(stream(), 20)
                assert not [e for e in events if e.get("type") == "error"], events
        finally:
            await adapter.disconnect()
        assert len(server.requests) == 1
        assert {k.lower(): v for k, v in server.requests[0].headers.items()}[
            "authorization"
        ] == "Bearer key-active"
        ledger = mock_db.execute(
            "SELECT provider_id, provider_type, billing, tokens_in, tokens_out"
            " FROM usage_log WHERE session_id=?",
            (sid,),
        ).fetchall()
        assert len(ledger) == 1
        assert tuple(ledger[0])[:3] == ("active", "openrouter", "api")
        assert ledger[0][3] > 0 and ledger[0][4] > 0
        assert options.aih_provider_id == "active"
        assert options.aih_provider_pin_id == "old"
        assert db.get_session(sid)["model"] == "prov:old:same-model"


@pytest.mark.asyncio
@pytest.mark.parametrize("media", ["image", "video"])
async def test_media_retry_stays_on_accepted_jobs_account(
    mock_db: Any, monkeypatch: pytest.MonkeyPatch, media: str
) -> None:
    """A 401 retry on an accepted media job keeps using the submitting account."""
    from app.core import xai_token_refresh
    from app.core.native_generation_settings import (
        NativeImageSettings,
        NativeVideoSettings,
    )

    encryption.set_encryption_key("test-password", db)
    for account in ("old", "active"):
        db.create_model_provider(
            account, account, "custom", "https://example.invalid", enabled=False
        )
        db.set_codex_token_bundle(
            account,
            access_token=encryption.encrypt_value("token-" + account),
            refresh_token=encryption.encrypt_value("refresh-" + account),
            account_id=account,
            auth_mode="grok",
            activate=account == "old",
        )
    seen = []

    async def refresh(account, *, stale_access_token):
        assert account == "old" and stale_access_token == "token-old"
        db.set_codex_token_bundle(
            account,
            access_token=encryption.encrypt_value("rotated-old"),
            refresh_token=encryption.encrypt_value("rotated-refresh"),
            account_id=account,
            auth_mode="grok",
        )
        return True

    renewal = AsyncMock(side_effect=refresh)
    monkeypatch.setattr(xai_token_refresh, "refresh_on_401_once", renewal)

    def upstream(request):
        seen.append((request.method, request.url.path, request.headers["authorization"]))
        if len(seen) == 1:
            assert seen[-1][2] == "Bearer token-old"
            db.set_model_provider_enabled("active", True)
            if media == "video":
                return httpx.Response(200, json={"request_id": "job-owned-by-old"})
        if len(seen) == (2 if media == "video" else 1):
            return httpx.Response(401)
        assert request.headers["authorization"] == "Bearer rotated-old"
        return httpx.Response(
            200,
            json=(
                {"status": "done", "video": {"url": "https://media.x.ai/clip.mp4"}}
                if media == "video"
                else {"data": [{"b64_json": "fake-image"}]}
            ),
        )

    client = lambda: httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    if media == "video":
        monkeypatch.setattr(native_video, "http_client", client)
        monkeypatch.setattr(native_video, "POLL_INTERVAL", 0)
        monkeypatch.setattr(native_video, "_download", AsyncMock(return_value=b"mp4"))
        result = await native_video.generate(
            "old",
            "circle",
            NativeVideoSettings(
                model="grok-imagine-video-1.5-lite",
                duration=1,
                resolution="480p",
                aspect_ratio="16:9",
            ),
        )
        assert seen[-1][2] == "Bearer rotated-old", seen
        assert result["success"], result
        assert [s[0] for s in seen] == ["POST", "GET", "GET"]
        assert seen[1][1] == seen[2][1] == "/v1/videos/job-owned-by-old"
    else:
        monkeypatch.setattr(supergrok_images, "http_client", client)
        result, error = await supergrok_images.generate(
            "old",
            "circle",
            NativeImageSettings(model="grok-imagine-image", size="1k", format="jpeg"),
        )
        assert seen[-1][2] == "Bearer rotated-old", seen
        assert error is None and result, error
        assert [s[0] for s in seen] == ["POST", "POST"]
    renewal.assert_awaited_once()
    assert db.get_model_provider("active")["enabled"]
    assert not db.get_model_provider("old")["enabled"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["chatgpt", "grok"])
async def test_native_subscription_auth_refresh_stays_on_bound_account(
    mock_db: Any, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """ChatGPT image Responses and native Grok auth already reload the exact row."""
    from app.core import codex_token_refresh, xai_token_refresh

    encryption.set_encryption_key("test-password", db)
    for account in ("old", "active"):
        db.create_model_provider(
            account, account, "custom", "https://example.invalid", enabled=False
        )
        db.set_codex_token_bundle(
            account,
            access_token=encryption.encrypt_value("token-" + account),
            refresh_token=encryption.encrypt_value("refresh-" + account),
            account_id=account,
            auth_mode=mode,
            activate=account == "old",
        )
    credentials = await fluid_credentials.resolve_provider_credentials("openai_responses", "old")
    assert await credentials.auth.get_access_token() == "token-old"
    db.set_model_provider_enabled("active", True)

    async def refresh(account, *, stale_access_token):
        assert account == "old" and stale_access_token == "token-old"
        db.set_codex_token_bundle(
            account,
            access_token=encryption.encrypt_value("rotated-old"),
            refresh_token=encryption.encrypt_value("rotated-refresh"),
            account_id=account,
            auth_mode=mode,
        )
        return True

    service = codex_token_refresh if mode == "chatgpt" else xai_token_refresh
    renewal = AsyncMock(side_effect=refresh)
    monkeypatch.setattr(service, "refresh_on_401_once", renewal)
    await credentials.auth.on_401()
    renewal.assert_awaited_once()
    assert credentials.auth.provider_id == "old"
    assert await credentials.auth.get_access_token() == "rotated-old"
    if mode == "chatgpt":
        assert credentials.auth.account_id == "old"


@pytest.mark.parametrize(
    "module",
    [
        "app/core/claude_accounts.py",
        "app/db/model_provider_slots.py",
        "app/api/settings_claude_accounts.py",
    ],
)
def test_new_public_account_apis_documented_and_typed(module: str) -> None:
    """New public account APIs carry type hints and docstrings (CONTRIBUTING rules)."""
    tree = ast.parse((ROOT / module).read_text())
    failures = []
    for node in tree.body:
        if not isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ) or node.name.startswith("_"):
            continue
        if not ast.get_docstring(node):
            failures.append(node.name + ": docstring")
        if isinstance(node, ast.ClassDef):
            continue
        if node.returns is None:
            failures.append(node.name + ": return type")
        args = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
        args += [a for a in (node.args.vararg, node.args.kwarg) if a]
        failures += [node.name + ": " + a.arg for a in args if a.annotation is None]
    assert not failures, failures


def test_account_switch_acceptance_coverage_registered() -> None:
    """The Settings switch journey and live-smoke scenarios stay registered."""
    journey = ROOT / "frontend/tests/journeys/provider-account-switch.journey.mjs"
    assert journey.is_file(), "saved-account Settings journey is missing"
    mapping = json.loads((journey.parent / "map.json").read_text())
    assert any("provider-account-switch" in rule["journeys"] for rule in mapping["rules"])
    catalog = json.loads((ROOT / "scripts/live_smoke/scenarios.json").read_text())
    rows = {row["id"]: row for suite in catalog["suites"] for row in suite["rows"]}
    for scenario in ("runtime-pin-follow", "second-subscription-sign-in"):
        row = rows["harness-basics.pr840-" + scenario]
        assert "provider" in row["needs"]
        assert row["how"] and row["pass_when"]
