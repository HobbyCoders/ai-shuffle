"""API tests for the provider catalog-fetch route (phase-2 Chunk D.1/D.2).

``POST /api/v1/model-providers/{id}/fetch-models`` returns (never persists) a
provider's live model roster for the curation UI:

* ChatGPT/Codex → ``GET https://chatgpt.com/backend-api/codex/models``
* OpenAI / xAI / OpenRouter / custom → OpenAI-compatible ``GET .../models``

All upstream HTTP is stubbed via ``httpx.MockTransport`` — NO network. The
shared client used by the route is swapped for a MockTransport-backed one via a
monkeypatch on ``app.core.http_client.get_http_client``.
"""

import httpx

from app.api import model_providers as mp_api
from app.core import encryption, model_catalog_presets
from app.db import database as db
from tests.helpers import unlock_encryption


ROOT = "/api/v1/model-providers"
PROVIDER_ID = "prov-fetch-1"
RAW_TOKEN = "sk-or-v1-abcdefghijklmnopqrstuvwxyz0123456789"


def test_codex_refresh_repairs_v1_null_despite_missing_curated_model(
    authenticated_client, mock_db, monkeypatch,
):
    """A partial live repair must tell the client to reload the persisted rows."""
    unlock_encryption(mock_db)
    _make_provider(provider_type="chatgpt", base_url="https://chatgpt.com/backend-api")
    _attach_subscription()
    mp_api._capability_results.clear()
    mp_api._capability_locks.clear()
    specs = [("gpt-6-astra", "medium", True), ("gpt-5.6-sol", "low", True),
             ("gpt-5.6-terra", "medium", True), ("gpt-5.6-luna", "medium", False)]
    unknown = {"version": 1, "effort_levels": None, "default_effort": None}
    curated = [{"id": model, "label": "My " + model, "reasoning_capabilities": dict(unknown)}
               for model, _, _ in specs]
    absent = {"id": "curated-missing", "label": "Keep this", "reasoning_capabilities": dict(unknown)}
    known = {"id": "known-model", "label": "Known", "reasoning_capabilities": {
        "version": 1, "effort_levels": ["high"], "default_effort": "high"}}
    curated.extend([absent, known])
    db.update_model_provider(PROVIDER_ID, models_json=curated, default_model="gpt-5.6-sol")
    before = db.get_model_provider(PROVIDER_ID)
    payload = {"models": [{"slug": model, "display_name": "Upstream " + model,
        "supported_reasoning_levels": [{"effort": value} for value in
            ["low", "medium", "high", "xhigh", "max"] + (["ultra"] if ultra else [])],
        "default_reasoning_level": default,
    } for model, default, ultra in specs] + [{"slug": "known-model"}, {"slug": "uncurated"}]}
    _install_mock_transport(monkeypatch, lambda request: httpx.Response(200, json=payload))
    try:
        response = authenticated_client.post(ROOT + "/refresh-capabilities", json={"provider_ids": [PROVIDER_ID]})
        assert response.status_code == 200, response.text
        assert response.json() == {"refreshed": [PROVIDER_ID], "failed": []}
        after = db.get_model_provider(PROVIDER_ID)
        assert after["default_model"] == before["default_model"]
        assert [(m["id"], m["label"]) for m in after["models_json"]] == [
            (m["id"], m["label"]) for m in before["models_json"]]
        for entry, (_, default, ultra) in zip(after["models_json"], specs):
            assert entry["reasoning_capabilities"] == {"version": 1,
                "effort_levels": ["low", "medium", "high", "xhigh", "max"] + (["ultra"] if ultra else []),
                "default_effort": default}
        assert after["models_json"][-2:] == before["models_json"][-2:]
        # Coalesced refreshes must also report the successful partial repair.
        assert authenticated_client.post(ROOT + "/refresh-capabilities", json={
            "provider_ids": [PROVIDER_ID]}).json() == response.json()
    finally:
        mp_api._capability_results.clear()
        mp_api._capability_locks.clear()


def _make_provider(
    *,
    provider_type="openrouter",
    base_url="https://openrouter.ai/api",
    auth_token=None,
):
    db.create_model_provider(
        provider_id=PROVIDER_ID,
        name="OpenRouter",
        provider_type=provider_type,
        base_url=base_url,
        auth_token=auth_token,
        enabled=True,
    )


def _attach_subscription(*, auth_mode="chatgpt", account_id="acct-1"):
    db.set_codex_token_bundle(
        PROVIDER_ID,
        access_token=encryption.encrypt_value("codex-access"),
        refresh_token=encryption.encrypt_value("codex-refresh"),
        account_id=account_id,
        auth_mode=auth_mode,
    )


def _install_mock_transport(monkeypatch, handler):
    """Point the route's shared HTTP client at a MockTransport running ``handler``.

    Returns nothing; ``handler`` receives each ``httpx.Request`` and returns an
    ``httpx.Response``. The route imports ``get_http_client`` from
    ``app.core.http_client`` at call time, so patching that name suffices.
    """
    transport = httpx.MockTransport(handler)
    mock_client = httpx.AsyncClient(transport=transport)

    from app.core import http_client
    monkeypatch.setattr(http_client, "get_http_client", lambda: mock_client)
    return mock_client


class TestUnauthenticated:
    def test_requires_admin(self, client):
        resp = client.post(f"{ROOT}/{PROVIDER_ID}/fetch-models", json={})
        assert resp.status_code in (401, 403), resp.status_code


class TestLiveCatalogs:
    def test_chatgpt_hits_codex_models_endpoint(
        self, authenticated_client, mock_db, monkeypatch
    ):
        unlock_encryption(mock_db)
        _make_provider(
            provider_type="chatgpt",
            base_url="https://chatgpt.com/backend-api/codex",
        )
        _attach_subscription(auth_mode="chatgpt", account_id="acct-1")

        captured = {}

        def _handler(request):
            captured["url"] = str(request.url)
            captured["auth"] = request.headers.get("authorization")
            captured["account"] = request.headers.get("chatgpt-account-id")
            captured["originator"] = request.headers.get("originator")
            captured["ua"] = request.headers.get("user-agent")
            return httpx.Response(200, json={"models": [
                {"id": "gpt-5.6-sol", "name": "GPT-5.6 Sol"},
                {"id": "gpt-5.6-terra"},
                {"id": "codex-auto-review"},
            ]})

        _install_mock_transport(monkeypatch, _handler)
        resp = authenticated_client.post(f"{ROOT}/{PROVIDER_ID}/fetch-models")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["source"] == "upstream"
        assert body["models"] == [
            {"id": "gpt-5.6-sol", "label": "GPT-5.6 Sol",
             "reasoning_capabilities": {"version": 1, "effort_levels": None, "default_effort": None}},
            {"id": "gpt-5.6-terra", "label": "GPT-5.6 Terra",
             "reasoning_capabilities": {"version": 1, "effort_levels": None, "default_effort": None}},
        ]
        assert captured["url"].startswith(
            "https://chatgpt.com/backend-api/codex/models"
        )
        assert "client_version=1.0.0" in captured["url"]
        assert captured["auth"] == "Bearer codex-access"
        assert captured["account"] == "acct-1"
        assert captured["originator"] == "codex_cli_rs"
        assert captured["ua"] == "codex_cli_rs/1.0.0"

    def test_chatgpt_catalog_uses_slug_and_hides_visibility_hide(
        self, authenticated_client, mock_db, monkeypatch
    ):
        """Live Codex entries identify models with ``slug``, not ``id``.

        Refresh used to skip every row (no ``id`` / ``model``), so new slugs
        like gpt-6-astra never appeared on the ChatGPT sub picker even though
        the same account's OpenAI API catalog listed them.
        """
        unlock_encryption(mock_db)
        _make_provider(
            provider_type="chatgpt",
            base_url="https://chatgpt.com/backend-api/codex",
        )
        _attach_subscription(auth_mode="chatgpt", account_id="acct-1")

        def _handler(request):
            return httpx.Response(200, json={"models": [
                {
                    "slug": "gpt-6-astra",
                    "display_name": "GPT-6-Astra",
                    "context_window": 272000,
                    # Source-shaped object entries; the advertised values, not
                    # the slug, determine the available efforts and default.
                    "supported_reasoning_levels": [
                        {"effort": "high", "description": "More reasoning"},
                        {"effort": "low", "description": "Less reasoning"},
                    ],
                    "default_reasoning_level": "low",
                    "visibility": ["codex"],
                },
                {
                    "slug": "gpt-5.6-sol",
                    "display_name": "GPT-5.6-Sol",
                    "supported_reasoning_levels": [
                        {"effort": "medium", "description": "Balanced reasoning"},
                        {"effort": "xhigh", "description": "Extra reasoning"},
                    ],
                    "default_reasoning_level": "xhigh",
                    "visibility": ["codex"],
                },
                {
                    "slug": "gpt-reserve",
                    "display_name": "GPT-Reserve",
                    "visibility": "hide",
                },
                {
                    "slug": "codex-auto-review",
                    "display_name": "Codex Auto Review",
                    "visibility": "hide",
                },
            ]})

        _install_mock_transport(monkeypatch, _handler)
        resp = authenticated_client.post(f"{ROOT}/{PROVIDER_ID}/fetch-models")
        assert resp.status_code == 200, resp.text
        assert resp.json()["models"] == [
            {
                "id": "gpt-6-astra",
                "label": "GPT-6-Astra",
                "reasoning_capabilities": {
                    "version": 1, "effort_levels": ["low", "high"],
                    "default_effort": "low",
                },
                "context_window": 272000,
            },
            {
                "id": "gpt-5.6-sol", "label": "GPT-5.6-Sol",
                "reasoning_capabilities": {
                    "version": 1, "effort_levels": ["medium", "xhigh"],
                    "default_effort": "xhigh",
                },
            },
        ]

    def test_chatgpt_without_tokens_returns_409(
        self, authenticated_client, mock_db, monkeypatch
    ):
        unlock_encryption(mock_db)
        _make_provider(provider_type="chatgpt",
                       base_url="https://chatgpt.com/backend-api/codex")

        def _boom(request):  # pragma: no cover
            raise AssertionError("must 409 before any network call")

        _install_mock_transport(monkeypatch, _boom)
        resp = authenticated_client.post(f"{ROOT}/{PROVIDER_ID}/fetch-models")
        assert resp.status_code == 409, resp.text

    def test_chatgpt_401_refreshes_and_retries(
        self, authenticated_client, mock_db, monkeypatch
    ):
        unlock_encryption(mock_db)
        _make_provider(
            provider_type="chatgpt",
            base_url="https://chatgpt.com/backend-api/codex",
        )
        _attach_subscription()
        calls = {"n": 0}

        def _handler(request):
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(401, json={"error": "unauthorized"})
            return httpx.Response(200, json={"models": [{"id": "gpt-5.6-sol"}]})

        _install_mock_transport(monkeypatch, _handler)

        async def _refresh(provider_id, stale_access_token=None, client=None):
            db.set_codex_token_bundle(
                PROVIDER_ID,
                access_token=encryption.encrypt_value("codex-access-2"),
                refresh_token=encryption.encrypt_value("codex-refresh-2"),
                account_id="acct-1",
                auth_mode="chatgpt",
            )
            return True

        monkeypatch.setattr(mp_api.codex_token_refresh, "refresh_on_401_once", _refresh)
        resp = authenticated_client.post(f"{ROOT}/{PROVIDER_ID}/fetch-models")
        assert resp.status_code == 200, resp.text
        assert resp.json()["models"] == [
            {"id": "gpt-5.6-sol", "label": "GPT-5.6 Sol",
             "reasoning_capabilities": {"version": 1, "effort_levels": None, "default_effort": None}},
        ]
        assert calls["n"] == 2

    def test_openai_live_catalog_does_not_double_v1(
        self, authenticated_client, mock_db, monkeypatch
    ):
        unlock_encryption(mock_db)
        _make_provider(
            provider_type="openai",
            base_url="https://api.openai.com/v1",
            auth_token=encryption.encrypt_value(RAW_TOKEN),
        )

        captured = {}

        def _handler(request):
            captured["url"] = str(request.url)
            return httpx.Response(200, json={"data": [
                {"id": "gpt-5.6-sol", "name": "GPT-5.6 Sol"},
                {"id": "gpt-5.7"},
            ]})

        _install_mock_transport(monkeypatch, _handler)
        resp = authenticated_client.post(f"{ROOT}/{PROVIDER_ID}/fetch-models")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["source"] == "upstream"
        assert body["models"] == [
            {"id": "gpt-5.6-sol", "label": "GPT-5.6 Sol",
             "reasoning_capabilities": {"version": 1,
                 "effort_levels": ["none", "low", "medium", "high", "xhigh", "max"],
                 "default_effort": "medium"}},
            {"id": "gpt-5.7", "label": "GPT-5.7",
             "reasoning_capabilities": {"version": 1, "effort_levels": None, "default_effort": None}},
        ]
        assert captured["url"] == "https://api.openai.com/v1/models"
        row = db.get_model_provider(PROVIDER_ID)
        assert not (row.get("models_json") or [])

    def test_xai_live_catalog_does_not_double_v1(
        self, authenticated_client, mock_db, monkeypatch
    ):
        unlock_encryption(mock_db)
        _make_provider(
            provider_type="xai",
            base_url="https://api.x.ai/v1",
            auth_token=encryption.encrypt_value(RAW_TOKEN),
        )

        captured = {}

        def _handler(request):
            captured["url"] = str(request.url)
            captured["auth"] = request.headers.get("authorization")
            return httpx.Response(200, json={"data": [
                {"id": "grok-4.6"},
                {"id": "grok-4.7"},
            ]})

        _install_mock_transport(monkeypatch, _handler)
        resp = authenticated_client.post(f"{ROOT}/{PROVIDER_ID}/fetch-models")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["source"] == "upstream"
        assert [m["id"] for m in body["models"]] == ["grok-4.6", "grok-4.7"]
        assert captured["url"] == "https://api.x.ai/v1/models"
        assert captured["auth"] == f"Bearer {RAW_TOKEN}"

    def test_grok_subscription_uses_oauth_token(
        self, authenticated_client, mock_db, monkeypatch
    ):
        unlock_encryption(mock_db)
        _make_provider(provider_type="grok", base_url="https://api.x.ai/v1")
        _attach_subscription(auth_mode="grok")

        captured = {}

        def _handler(request):
            captured["url"] = str(request.url)
            captured["auth"] = request.headers.get("authorization")
            return httpx.Response(200, json={"data": [{"id": "grok-4.6"}]})

        _install_mock_transport(monkeypatch, _handler)
        resp = authenticated_client.post(f"{ROOT}/{PROVIDER_ID}/fetch-models")
        assert resp.status_code == 200, resp.text
        assert resp.json()["source"] == "upstream"
        assert captured["url"] == "https://api.x.ai/v1/models"
        assert captured["auth"] == "Bearer codex-access"

    def test_codex_roster_excludes_codex_auto_review(self):
        # codex-auto-review is special-purpose — must never appear in the picker.
        ids = {entry["id"] for entry in model_catalog_presets.CODEX_PRESET_MODELS}
        assert "codex-auto-review" not in ids
        assert "codex-auto-review" in mp_api._CODEX_EXCLUDED_MODEL_IDS


class TestUpstreamPath:
    def test_openai_shape(self, authenticated_client, mock_db, monkeypatch):
        unlock_encryption(mock_db)
        _make_provider(auth_token=encryption.encrypt_value(RAW_TOKEN))

        def _handler(request):
            return httpx.Response(200, json={"data": [
                {"id": "openai/gpt-5.6", "name": "GPT-5.6"},
                {"id": "meta-llama/llama-3.1"},  # label is derived from the id
            ]})

        _install_mock_transport(monkeypatch, _handler)
        resp = authenticated_client.post(f"{ROOT}/{PROVIDER_ID}/fetch-models")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["source"] == "upstream"
        assert body["models"] == [
            {"id": "openai/gpt-5.6", "label": "GPT-5.6",
             "reasoning_capabilities": {"version": 1, "effort_levels": [], "default_effort": None}},
            {"id": "meta-llama/llama-3.1", "label": "Llama 3.1",
             "reasoning_capabilities": {"version": 1, "effort_levels": [], "default_effort": None}},
        ]

    def test_openrouter_strips_author_prefix_from_names(
        self, authenticated_client, mock_db, monkeypatch
    ):
        unlock_encryption(mock_db)
        _make_provider(auth_token=encryption.encrypt_value(RAW_TOKEN))

        def _handler(request):
            return httpx.Response(200, json={"data": [
                {"id": "anthropic/claude-fable-5.1",
                 "name": "Anthropic: Claude Fable 5.1"},
                {"id": "z-ai/glm-5.3-flash", "name": "Z.ai: GLM 5.3 Flash"},
            ]})

        _install_mock_transport(monkeypatch, _handler)
        resp = authenticated_client.post(f"{ROOT}/{PROVIDER_ID}/fetch-models")
        assert resp.status_code == 200, resp.text
        assert resp.json()["models"] == [
            {"id": "anthropic/claude-fable-5.1", "label": "Claude Fable 5.1",
             "reasoning_capabilities": {"version": 1, "effort_levels": [], "default_effort": None}},
            {"id": "z-ai/glm-5.3-flash", "label": "GLM 5.3 Flash",
             "reasoning_capabilities": {"version": 1, "effort_levels": [], "default_effort": None}},
        ]

    def test_bare_list_shape(self, authenticated_client, mock_db, monkeypatch):
        unlock_encryption(mock_db)
        _make_provider(auth_token=encryption.encrypt_value(RAW_TOKEN))

        def _handler(request):
            return httpx.Response(200, json=[
                {"id": "a", "label": "Model A"},
                "bare-string-id",
            ])

        _install_mock_transport(monkeypatch, _handler)
        resp = authenticated_client.post(f"{ROOT}/{PROVIDER_ID}/fetch-models")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["source"] == "upstream"
        assert body["models"] == [
            {"id": "a", "label": "Model A",
             "reasoning_capabilities": {"version": 1, "effort_levels": [], "default_effort": None}},
            {"id": "bare-string-id", "label": "Bare String Id"},
        ]

    def test_upstream_500_maps_to_502(self, authenticated_client, mock_db, monkeypatch):
        unlock_encryption(mock_db)
        _make_provider(auth_token=encryption.encrypt_value(RAW_TOKEN))

        def _handler(request):
            return httpx.Response(500, text="upstream boom")

        _install_mock_transport(monkeypatch, _handler)
        resp = authenticated_client.post(f"{ROOT}/{PROVIDER_ID}/fetch-models")
        assert resp.status_code == 502, resp.text

    def test_list_capped_at_2000(self, authenticated_client, mock_db, monkeypatch):
        unlock_encryption(mock_db)
        _make_provider(auth_token=encryption.encrypt_value(RAW_TOKEN))

        def _handler(request):
            return httpx.Response(200, json={"data": [
                {"id": f"model-{i}"} for i in range(2500)
            ]})

        _install_mock_transport(monkeypatch, _handler)
        resp = authenticated_client.post(f"{ROOT}/{PROVIDER_ID}/fetch-models")
        assert resp.status_code == 200, resp.text
        assert len(resp.json()["models"]) == 2000

    def test_auth_header_carries_decrypted_token(self, authenticated_client, mock_db, monkeypatch):
        unlock_encryption(mock_db)
        _make_provider(auth_token=encryption.encrypt_value(RAW_TOKEN))

        captured = {}

        def _handler(request):
            captured["auth"] = request.headers.get("authorization")
            captured["url"] = str(request.url)
            return httpx.Response(200, json={"data": []})

        _install_mock_transport(monkeypatch, _handler)
        resp = authenticated_client.post(f"{ROOT}/{PROVIDER_ID}/fetch-models")
        assert resp.status_code == 200, resp.text
        # The DECRYPTED plaintext token must ride the request, not the enc: blob.
        assert captured["auth"] == f"Bearer {RAW_TOKEN}"
        assert captured["url"] == "https://openrouter.ai/api/v1/models"

    def test_encryption_locked_returns_400(self, authenticated_client, mock_db, monkeypatch):
        # Provider carries a token blob, but encryption is NOT unlocked → cannot
        # decrypt → 400 (before any network call).
        encryption.clear_encryption_key()
        _make_provider(auth_token="enc:not-decryptable-while-locked")

        def _boom(request):  # pragma: no cover
            raise AssertionError("must 400 before any network call")

        _install_mock_transport(monkeypatch, _boom)
        resp = authenticated_client.post(f"{ROOT}/{PROVIDER_ID}/fetch-models")
        assert resp.status_code == 400, resp.text

    def test_provider_not_found_404(self, authenticated_client, mock_db):
        unlock_encryption(mock_db)
        resp = authenticated_client.post(f"{ROOT}/does-not-exist/fetch-models")
        assert resp.status_code == 404, resp.text


class TestHelperParsing:
    """Direct unit coverage of the normalize helper (shape edge cases)."""

    def test_parse_skips_entries_without_id(self):
        out = mp_api._parse_models_response({"data": [
            {"label": "no id"}, {"id": "keep", "label": "Keep"}, 42,
        ]})
        assert out == [{"id": "keep", "label": "Keep",
         "reasoning_capabilities": {"version": 1, "effort_levels": None, "default_effort": None}}]

    def test_parse_strips_openrouter_author_prefix(self):
        out = mp_api._parse_models_response(
            {"data": [
                {"id": "anthropic/claude-fable-5.1",
                 "name": "Anthropic: Claude Fable 5.1"},
                {"id": "z-ai/glm-5.3", "name": "Z.ai: GLM 5.3"},
                {"id": "x-ai/grok-4.6", "name": "Grok 4.6"},
            ]},
            provider_type="openrouter",
        )
        assert [m["label"] for m in out] == [
            "Claude Fable 5.1", "GLM 5.3", "Grok 4.6",
        ]

    def test_parse_keeps_author_prefix_for_non_openrouter(self):
        out = mp_api._parse_models_response(
            {"data": [{"id": "x", "name": "Anthropic: Claude Fable 5.1"}]},
            provider_type="custom",
        )
        assert out == [{"id": "x", "label": "Anthropic: Claude Fable 5.1",
         "reasoning_capabilities": {"version": 1, "effort_levels": None, "default_effort": None}}]

    def test_parse_carries_through_upstream_context_length(self):
        """OpenRouter-shaped windows survive normalization as ``context_window``.
        They used to be stripped, forcing every model onto the row's single
        shared column."""
        out = mp_api._parse_models_response({"data": [
            {"id": "a", "label": "A", "context_length": 128000},
            {"id": "b", "label": "B", "top_provider": {"context_length": 1000000}},
            {"id": "c", "label": "C"},
        ]})
        assert out == [
            {"id": "a", "label": "A", "context_window": 128000,
             "reasoning_capabilities": {"version": 1, "effort_levels": None, "default_effort": None}},
            {"id": "b", "label": "B", "context_window": 1000000,
             "reasoning_capabilities": {"version": 1, "effort_levels": None, "default_effort": None}},
            {"id": "c", "label": "C",
             "reasoning_capabilities": {"version": 1, "effort_levels": None, "default_effort": None}},
        ]

    def test_parse_prefers_top_provider_context_length(self):
        """``top_provider`` is the actual serving provider's window — it wins."""
        out = mp_api._parse_models_response({"data": [
            {"id": "a", "label": "A", "context_length": 128000,
             "top_provider": {"context_length": 64000}},
        ]})
        assert out == [{"id": "a", "label": "A", "context_window": 64000,
         "reasoning_capabilities": {"version": 1, "effort_levels": None, "default_effort": None}}]

    def test_parse_preserves_only_pricing_needed_for_cost_projection(self):
        out = mp_api._parse_models_response({"data": [{
            "id": "x-ai/grok-4.6",
            "name": "Grok 4.6",
            "pricing": {
                "prompt": "0.000001",
                "completion": "0.000002",
                "input_cache_read": "0.0000002",
                "irrelevant": "must-not-persist",
            },
        }]})
        assert out == [{
            "id": "x-ai/grok-4.6",
            "label": "Grok 4.6",
            "reasoning_capabilities": {"version": 1, "effort_levels": None, "default_effort": None},
            "pricing": {
                "prompt": "0.000001",
                "completion": "0.000002",
                "input_cache_read": "0.0000002",
            },
        }]

    def test_parse_ignores_unusable_context_length(self):
        out = mp_api._parse_models_response({"data": [
            {"id": "a", "label": "A", "context_length": "huge"},
            {"id": "b", "label": "B", "context_length": 0},
            {"id": "c", "label": "C", "context_length": None},
        ]})
        assert out == [
            {"id": "a", "label": "A",
             "reasoning_capabilities": {"version": 1, "effort_levels": None, "default_effort": None}},
            {"id": "b", "label": "B",
             "reasoning_capabilities": {"version": 1, "effort_levels": None, "default_effort": None}},
            {"id": "c", "label": "C",
             "reasoning_capabilities": {"version": 1, "effort_levels": None, "default_effort": None}},
        ]

    def test_parse_non_list_returns_empty(self):
        assert mp_api._parse_models_response({"data": "nope"}) == []
        assert mp_api._parse_models_response(123) == []

    def test_parse_unnamed_models_uses_display_names_and_keeps_ids(self):
        ids = ["gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"]
        out = mp_api._parse_models_response(
            {"data": [{"id": model_id} for model_id in ids]}, provider_type="openai",
        )
        assert [model["id"] for model in out] == ids
        assert [model["label"] for model in out] == [
            "GPT-6 Astra", "GPT-5.6 Sol", "GPT-5.6 Terra", "GPT-5.6 Luna",
        ]
        bare = mp_api._parse_models_response(["grok-4.6"], provider_type="xai")
        assert (bare[0]["id"], bare[0]["label"]) == ("grok-4.6", "Grok 4.6")

    def test_parse_codex_models_key(self):
        out = mp_api._parse_models_response({"models": [
            {"id": "gpt-5.6-sol"},
            {"id": "gpt-5.7", "name": "GPT-5.7"},
        ]})
        assert out == [
            {"id": "gpt-5.6-sol", "label": "GPT-5.6 Sol",
             "reasoning_capabilities": {"version": 1, "effort_levels": None, "default_effort": None}},
            {"id": "gpt-5.7", "label": "GPT-5.7",
             "reasoning_capabilities": {"version": 1, "effort_levels": None, "default_effort": None}},
        ]

    def test_parse_codex_slug_display_name_and_context_window(self):
        """The real Codex catalog has no ``id`` / ``name`` / ``context_length``."""
        out = mp_api._parse_models_response({"models": [
            {
                "slug": "gpt-6-astra",
                "display_name": "GPT-6-Astra",
                "context_window": 272000,
            },
            {"slug": "gpt-5.6-sol", "display_name": "GPT-5.6-Sol"},
        ]})
        assert out == [
            {
                "id": "gpt-6-astra",
                "label": "GPT-6-Astra",
                "reasoning_capabilities": {"version": 1, "effort_levels": None, "default_effort": None},
                "context_window": 272000,
            },
            {"id": "gpt-5.6-sol", "label": "GPT-5.6-Sol",
             "reasoning_capabilities": {"version": 1, "effort_levels": None, "default_effort": None}},
        ]

    def test_codex_visible_payload_drops_hide(self):
        payload = {"models": [
            {"slug": "gpt-6-astra", "visibility": ["codex"]},
            {"slug": "gpt-reserve", "visibility": "hide"},
        ]}
        out = mp_api._codex_visible_payload(payload)
        assert [e["slug"] for e in out["models"]] == ["gpt-6-astra"]

    def test_models_catalog_url_avoids_double_v1(self):
        assert mp_api._models_catalog_url("https://api.openai.com/v1") == (
            "https://api.openai.com/v1/models"
        )
        assert mp_api._models_catalog_url("https://openrouter.ai/api") == (
            "https://openrouter.ai/api/v1/models"
        )
        assert mp_api._models_catalog_url("https://api.x.ai/v1/") == (
            "https://api.x.ai/v1/models"
        )


class TestMediaModelFiltering:
    """The provider list is a TEXT-generation picker.

    Audio, voice, image and video endpoints are configured elsewhere (AI tools,
    and Settings > Voice), so they are split out of ``models`` and reported in
    ``hidden`` instead of being offered as chat models.
    """

    def test_media_models_are_split_out_on_the_generic_path(
        self, authenticated_client, mock_db, monkeypatch
    ):
        unlock_encryption(mock_db)
        _make_provider(auth_token=encryption.encrypt_value(RAW_TOKEN))

        def _handler(request):
            return httpx.Response(200, json={"data": [
                {"id": "gpt-5.6-sol"},
                {"id": "gpt-realtime-2.1"},
                {"id": "whisper-1"},
                {"id": "tts-1-hd"},
                {"id": "dall-e-3"},
                # Chat model that merely speaks — must survive.
                {"id": "gpt-4o-audio-preview"},
            ]})

        _install_mock_transport(monkeypatch, _handler)
        resp = authenticated_client.post(f"{ROOT}/{PROVIDER_ID}/fetch-models")
        assert resp.status_code == 200, resp.text
        body = resp.json()

        assert [m["id"] for m in body["models"]] == [
            "gpt-5.6-sol",
            "gpt-4o-audio-preview",
        ]
        assert [m["id"] for m in body["hidden"]] == [
            "gpt-realtime-2.1",
            "whisper-1",
            "tts-1-hd",
            "dall-e-3",
        ]

    def test_hidden_entries_carry_their_destination_kind(
        self, authenticated_client, mock_db, monkeypatch
    ):
        """The UI switches on this to say where the model actually lives."""
        unlock_encryption(mock_db)
        _make_provider(auth_token=encryption.encrypt_value(RAW_TOKEN))

        def _handler(request):
            return httpx.Response(200, json={"data": [
                {"id": "gpt-realtime-2.1"},
                {"id": "whisper-1"},
                {"id": "sora-2"},
            ]})

        _install_mock_transport(monkeypatch, _handler)
        body = authenticated_client.post(f"{ROOT}/{PROVIDER_ID}/fetch-models").json()

        kinds = {m["id"]: m["media_kind"] for m in body["hidden"]}
        assert kinds == {
            "gpt-realtime-2.1": "voice",
            "whisper-1": "audio",
            "sora-2": "video",
        }

    def test_an_all_text_roster_reports_nothing_hidden(
        self, authenticated_client, mock_db, monkeypatch
    ):
        unlock_encryption(mock_db)
        _make_provider(auth_token=encryption.encrypt_value(RAW_TOKEN))

        def _handler(request):
            return httpx.Response(200, json={"data": [
                {"id": "gpt-5.6-sol"},
                {"id": "anthropic/claude-fable-5.1"},
            ]})

        _install_mock_transport(monkeypatch, _handler)
        body = authenticated_client.post(f"{ROOT}/{PROVIDER_ID}/fetch-models").json()

        assert len(body["models"]) == 2
        assert body["hidden"] == []

    def test_unknown_ids_are_never_hidden(
        self, authenticated_client, mock_db, monkeypatch
    ):
        """A custom gateway serves ids this rule has never seen; hiding them
        would empty its picker."""
        unlock_encryption(mock_db)
        _make_provider(auth_token=encryption.encrypt_value(RAW_TOKEN))

        def _handler(request):
            return httpx.Response(200, json={"data": [
                {"id": "acme-llm-7b"},
                {"id": "some-future-model-9"},
            ]})

        _install_mock_transport(monkeypatch, _handler)
        body = authenticated_client.post(f"{ROOT}/{PROVIDER_ID}/fetch-models").json()

        assert len(body["models"]) == 2
        assert body["hidden"] == []


class TestRecommendedModels:
    """``recommended``: the preset roster ids the upstream list holds, the only
    models the setup wizard may pre-check."""

    def test_openai_lists_its_roster_models_in_roster_order(
        self, authenticated_client, mock_db, monkeypatch
    ):
        unlock_encryption(mock_db)
        _make_provider(provider_type="openai", base_url="https://api.openai.com/v1",
                       auth_token=encryption.encrypt_value(RAW_TOKEN))

        def _handler(request):
            return httpx.Response(200, json={"data": [
                {"id": "text-embedding-3-small"},
                {"id": "omni-moderation-latest"},
                {"id": "babbage-002"},
                {"id": "gpt-5.4-mini"},
                {"id": "gpt-5.6-terra"},
            ]})

        _install_mock_transport(monkeypatch, _handler)
        resp = authenticated_client.post(f"{ROOT}/{PROVIDER_ID}/fetch-models")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # The upstream list is unsorted and keeps non-chat models.
        assert body["models"][0]["id"] == "text-embedding-3-small"
        assert body["recommended"] == ["gpt-5.6-terra", "gpt-5.4-mini"]

    def test_a_provider_without_a_roster_recommends_nothing(
        self, authenticated_client, mock_db, monkeypatch
    ):
        unlock_encryption(mock_db)
        _make_provider(auth_token=encryption.encrypt_value(RAW_TOKEN))

        def _handler(request):
            return httpx.Response(200, json={"data": [{"id": "openai/gpt-5.6-sol"}]})

        _install_mock_transport(monkeypatch, _handler)
        resp = authenticated_client.post(f"{ROOT}/{PROVIDER_ID}/fetch-models")
        assert resp.status_code == 200, resp.text
        assert resp.json()["recommended"] == []
