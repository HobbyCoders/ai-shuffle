"""Unit tests for the fluid native-client credential bridge.

Covers ``app/core/provider/fluid_credentials.py`` (provider-row → auth bundle)
and ``harness_adapter._build_provider_client`` (auth bundle → fluid client):

  1. API-key rows: the decrypted ``auth_token`` reaches each client's ``api_key``.
  2. A ChatGPT-subscription (Codex) row yields a ``ResponsesAuth``-shaped object
     whose ``get_access_token`` decrypts the stored token and whose ``on_401``
     drives the shared refresh worker — and points the client at the Codex
     backend.
  3. No provider bound → the fluid client's env-var fallback is kept.
  4. No credentials anywhere → a clear ``ProviderCredentialError``.
  5. The hard constraint: a subscription-auth row never reaches AnthropicClient.

NO live API calls: the fluid client constructors are patched and every DB /
crypto / refresh call is faked.

Run:  python -m pytest tests/test_fluid_credentials.py -q
"""

from __future__ import annotations

import pytest

from app.core.provider import harness_adapter, fluid_credentials
from app.core.provider.fluid_credentials import (
    CodexSubscriptionAuth,
    ProviderCredentialError,
    resolve_provider_credentials,
)
from app.core.provider.types import SessionOptions

PID = "prov_1"


# ─── Fakes ───────────────────────────────────────────────────────────────────


class FakeClient:
    """Records the kwargs a fluid client constructor was called with."""

    last: dict = {}

    def __init__(self, api_key=None, **kwargs):
        type(self).last = {"api_key": api_key, **kwargs}
        self.api_key = api_key
        self.kwargs = kwargs


class FakeOpenAI(FakeClient):
    pass


class FakeOpenRouter(FakeClient):
    pass


class FakeAnthropic(FakeClient):
    pass


@pytest.fixture(autouse=True)
def patch_clients(monkeypatch):
    """Patch the fluid client constructors — never construct a real one."""
    monkeypatch.setattr(harness_adapter, "OpenAIResponsesClient", FakeOpenAI)
    monkeypatch.setattr(harness_adapter, "OpenRouterClient", FakeOpenRouter)
    monkeypatch.setattr(harness_adapter, "AnthropicClient", FakeAnthropic)
    FakeOpenAI.last = FakeOpenRouter.last = FakeAnthropic.last = {}


@pytest.fixture(autouse=True)
def no_env_keys(monkeypatch):
    for var in ("OPENAI_API_KEY", "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)


def fake_provider_rows(monkeypatch, row, codex=None):
    """Install a fake provider-row service (the only DB the bridge touches)."""
    from app.db import database as db

    monkeypatch.setattr(
        db, "get_model_provider", lambda pid, include_token=False: row, raising=False
    )
    monkeypatch.setattr(
        db, "get_codex_tokens", lambda pid: codex, raising=False
    )
    # Stored blobs are ``enc:``-prefixed; the crypto service is reused as-is, so
    # only its two entry points are stubbed here.
    monkeypatch.setattr(
        fluid_credentials.encryption, "is_encrypted", lambda v: bool(v) and v.startswith("enc:")
    )
    monkeypatch.setattr(
        fluid_credentials.encryption, "decrypt_value", lambda v: v[len("enc:"):]
    )


def _opts(provider_id=PID, model="gpt-5"):
    extra = {"provider_id": provider_id} if provider_id else {}
    return SessionOptions(model=model, extra=extra)


# ─── 1. API-key rows ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,provider_type,fake",
    [
        ("openai_responses", "openai", FakeOpenAI),
        ("openrouter", "openrouter", FakeOpenRouter),
        ("anthropic", "anthropic", FakeAnthropic),
    ],
)
async def test_api_key_row_passthrough(monkeypatch, kind, provider_type, fake):
    fake_provider_rows(
        monkeypatch,
        {
            "id": PID,
            "name": "Row",
            "provider_type": provider_type,
            "enabled": True,
            "auth_token": "enc:sk-secret",
        },
    )
    client, http_client = await harness_adapter._build_provider_client(kind, _opts())
    if http_client is not None:
        await http_client.aclose()
    assert isinstance(client, fake)
    assert fake.last["api_key"] == "sk-secret"
    # An API-key openai row stays on the OpenAI platform endpoint.
    if kind == "openai_responses":
        assert fake.last["auth"] is None
        assert fake.last["base_url"] == harness_adapter.OPENAI_RESPONSES_URL


@pytest.mark.asyncio
async def test_plaintext_auth_token_still_works(monkeypatch):
    """Migration compatibility: a not-yet-encrypted token passes through."""
    fake_provider_rows(
        monkeypatch,
        {"id": PID, "name": "Row", "provider_type": "openrouter",
         "enabled": True, "auth_token": "plain-key"},
    )
    await harness_adapter._build_provider_client("openrouter", _opts())
    assert FakeOpenRouter.last["api_key"] == "plain-key"


# ─── 2. Codex (ChatGPT-subscription) OAuth ───────────────────────────────────


CODEX_ROW = {
    "id": PID,
    "name": "GPT Agent",
    "provider_type": "chatgpt",
    "auth_mode": "chatgpt",
    "enabled": True,
    "codex_account_id": "acct_42",
    "auth_token": None,
}
CODEX_TOKENS = {
    "auth_mode": "chatgpt",
    "codex_access_token": "enc:access-1",
    "codex_refresh_token": "enc:refresh-1",
    "codex_account_id": "acct_42",
    "needs_reauth": False,
}


@pytest.mark.asyncio
async def test_codex_subscription_builds_auth_object(monkeypatch):
    fake_provider_rows(monkeypatch, CODEX_ROW, codex=dict(CODEX_TOKENS))

    client, http_client = await harness_adapter._build_provider_client(
        "openai_responses", _opts()
    )
    if http_client is not None:
        await http_client.aclose()
    assert isinstance(client, FakeOpenAI)
    # OAuth mode: no api_key, an auth object, and the Codex backend URL.
    assert FakeOpenAI.last["api_key"] is None
    assert FakeOpenAI.last["base_url"] == harness_adapter.CODEX_RESPONSES_URL

    auth = FakeOpenAI.last["auth"]
    # Satisfies fluid's ResponsesAuth protocol.
    from fluid.providers.openai_responses.client import ResponsesAuth

    assert isinstance(auth, ResponsesAuth)
    assert auth.account_id == "acct_42"
    assert await auth.get_access_token() == "access-1"


@pytest.mark.asyncio
async def test_codex_token_reread_each_call(monkeypatch):
    """A rotation performed elsewhere is picked up without rebuilding."""
    tokens = dict(CODEX_TOKENS)
    fake_provider_rows(monkeypatch, CODEX_ROW, codex=tokens)
    auth = CodexSubscriptionAuth(PID, "acct_42")
    assert await auth.get_access_token() == "access-1"
    tokens["codex_access_token"] = "enc:access-2"
    assert await auth.get_access_token() == "access-2"


@pytest.mark.asyncio
async def test_codex_on_401_triggers_refresh(monkeypatch):
    fake_provider_rows(monkeypatch, CODEX_ROW, codex=dict(CODEX_TOKENS))
    seen = {}

    async def fake_refresh(provider_id, *, stale_access_token=None, client=None):
        seen["provider_id"] = provider_id
        seen["stale"] = stale_access_token
        return True

    from app.core import codex_token_refresh

    monkeypatch.setattr(codex_token_refresh, "refresh_on_401_once", fake_refresh)

    auth = CodexSubscriptionAuth(PID, "acct_42")
    await auth.get_access_token()
    await auth.on_401()
    # The token that got the 401 is handed to the refresh worker so a peer's
    # rotation is detected instead of firing a second (reuse-tripping) POST.
    assert seen == {"provider_id": PID, "stale": "access-1"}


@pytest.mark.asyncio
async def test_codex_needs_reauth_is_a_clear_error(monkeypatch):
    tokens = dict(CODEX_TOKENS, needs_reauth=True)
    fake_provider_rows(monkeypatch, CODEX_ROW, codex=tokens)
    with pytest.raises(ProviderCredentialError, match="reconnected"):
        await harness_adapter._build_provider_client("openai_responses", _opts())


@pytest.mark.asyncio
async def test_codex_missing_bundle_is_a_clear_error(monkeypatch):
    fake_provider_rows(monkeypatch, CODEX_ROW, codex=None)
    with pytest.raises(ProviderCredentialError, match="no credentials configured"):
        await harness_adapter._build_provider_client("openai_responses", _opts())


# ─── 3. Env fallback (no provider bound) ─────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,env_var,fake",
    [
        ("openai_responses", "OPENAI_API_KEY", FakeOpenAI),
        ("openrouter", "OPENROUTER_API_KEY", FakeOpenRouter),
        ("anthropic", "ANTHROPIC_API_KEY", FakeAnthropic),
    ],
)
async def test_env_fallback_when_no_provider_bound(monkeypatch, kind, env_var, fake):
    monkeypatch.setenv(env_var, "env-key")
    client, http_client = await harness_adapter._build_provider_client(
        kind, _opts(provider_id=None)
    )
    # Constructed bare — the fluid client reads the env var itself.
    assert http_client is None
    assert isinstance(client, fake)
    assert fake.last == {"api_key": None}


# ─── 4. No credentials at all ────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,env_var",
    [
        ("openai_responses", "OPENAI_API_KEY"),
        ("openrouter", "OPENROUTER_API_KEY"),
        ("anthropic", "ANTHROPIC_API_KEY"),
    ],
)
async def test_no_provider_and_no_env_raises(kind, env_var):
    with pytest.raises(ProviderCredentialError, match=env_var):
        await harness_adapter._build_provider_client(kind, _opts(provider_id=None))


@pytest.mark.asyncio
async def test_bound_provider_without_key_raises(monkeypatch):
    fake_provider_rows(
        monkeypatch,
        {"id": PID, "name": "Keyless", "provider_type": "openrouter",
         "enabled": True, "auth_token": None},
    )
    with pytest.raises(ProviderCredentialError, match="has no credentials configured"):
        await harness_adapter._build_provider_client("openrouter", _opts())


@pytest.mark.asyncio
async def test_missing_row_raises(monkeypatch):
    fake_provider_rows(monkeypatch, None)
    with pytest.raises(ProviderCredentialError, match="was not found"):
        await resolve_provider_credentials("openrouter", PID)


@pytest.mark.asyncio
async def test_disabled_row_raises(monkeypatch):
    fake_provider_rows(
        monkeypatch,
        {"id": PID, "name": "Off", "provider_type": "openrouter",
         "enabled": False, "auth_token": "enc:k"},
    )
    with pytest.raises(ProviderCredentialError, match="disabled"):
        await resolve_provider_credentials("openrouter", PID)


# ─── 5. HARD CONSTRAINT: no subscription OAuth into AnthropicClient ──────────


@pytest.mark.asyncio
@pytest.mark.parametrize("auth_mode", ["oauth", "subscription", "claude", "max", "pro"])
async def test_subscription_row_never_reaches_anthropic_client(monkeypatch, auth_mode):
    fake_provider_rows(
        monkeypatch,
        {"id": PID, "name": "Claude sub", "provider_type": "anthropic",
         "auth_mode": auth_mode, "enabled": True, "auth_token": "enc:oauth-token"},
    )
    with pytest.raises(ProviderCredentialError, match="delegated Claude"):
        await harness_adapter._build_provider_client("anthropic", _opts(model="claude-x"))
    # And nothing was constructed with that token.
    assert FakeAnthropic.last == {}


# ─── 6. Plumbing: provider_id reaches SessionOptions.extra ───────────────────


@pytest.mark.asyncio
async def test_sdk_options_provider_id_projects_onto_extra():
    class FakeSdkOptions:
        model = "gpt-5"
        system_prompt = None
        cwd = None
        resume = None
        permission_mode = None
        mcp_servers = None
        env = None
        include_partial_messages = False
        can_use_tool = None
        aih_provider_id = PID

    adapter = harness_adapter.HarnessAdapter()
    captured = {}

    async def fake_connect(opts):
        captured["extra"] = dict(opts.extra)

    adapter.connect = fake_connect  # type: ignore[method-assign]
    await adapter.connect_with_sdk_options(FakeSdkOptions())
    assert captured["extra"]["provider_id"] == PID


@pytest.mark.asyncio
async def test_sdk_options_without_provider_id_leaves_extra_empty():
    class FakeSdkOptions:
        model = "claude-sonnet-4-5"
        aih_provider_id = None

    adapter = harness_adapter.HarnessAdapter()
    captured = {}

    async def fake_connect(opts):
        captured["extra"] = dict(opts.extra)

    adapter.connect = fake_connect  # type: ignore[method-assign]
    await adapter.connect_with_sdk_options(FakeSdkOptions())
    assert captured["extra"] == {}
