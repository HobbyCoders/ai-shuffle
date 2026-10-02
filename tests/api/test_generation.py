"""
Tests for AI generation API endpoints.

Tests the profile and subagent generation functionality.
"""

import asyncio
import pytest
from unittest.mock import patch, MagicMock


# ---------------------------------------------------------------------------
# Adapter test doubles
#
# Generation runs through the provider adapter seam (make_adapter ->
# HarnessAdapter), NOT the SDK's query(). The adapter emits the app-local
# mirror classes from app.core.provider.events, so the doubles below yield
# those — SDK message classes would not match the isinstance chain.
# make_adapter is patched at the CONSUMER module (app.api.generation).
# ---------------------------------------------------------------------------

class FakeAdapter:
    """Minimal stand-in for HarnessAdapter's generate_with_claude lifecycle."""

    def __init__(
        self,
        messages=None,
        connect_error=None,
        send_error=None,
        hang=False,
    ):
        self.messages = list(messages or [])
        self.connect_error = connect_error
        self.send_error = send_error
        self.hang = hang
        self.connected = False
        self.disconnect_calls = 0
        self.options = None
        self.prompts = []

    async def connect_with_sdk_options(self, options):
        self.options = options
        if self.connect_error is not None:
            raise self.connect_error
        self.connected = True

    async def send_query(self, prompt):
        self.prompts.append(prompt)
        if self.send_error is not None:
            raise self.send_error

    async def receive_events(self):
        if self.hang:
            await asyncio.sleep(3600)
        for message in self.messages:
            yield message

    async def disconnect(self):
        self.disconnect_calls += 1


def assistant_text(text):
    """One AssistantMessage carrying a single TextBlock."""
    from app.core.provider.events import AssistantMessage, TextBlock

    return AssistantMessage(content=[TextBlock(text=text)], model="test-model")


def success_result():
    """A clean turn-terminal ResultMessage."""
    from app.core.provider.events import ResultMessage

    return ResultMessage(subtype="success", is_error=False)


def error_result(message="upstream exploded"):
    """A failed turn-terminal ResultMessage (harness ErrorEvent shape)."""
    from app.core.provider.events import ResultMessage

    return ResultMessage(
        subtype="error_during_execution",
        is_error=True,
        result=message,
        errors=[message],
    )


def text_turn(text):
    """The usual event sequence for a one-shot generation turn."""
    return [assistant_text(text), success_result()]


def patch_adapter(adapter):
    """Patch make_adapter at the consumer module to return `adapter`."""
    return patch("app.api.generation.make_adapter", return_value=adapter)


class TestExtractJsonFromResponse:
    """Test the JSON extraction helper function."""

    def test_extract_plain_json(self):
        """Should extract plain JSON object."""
        from app.api.generation import extract_json_from_response

        text = '{"name": "Test Profile", "model": "sonnet"}'
        result = extract_json_from_response(text)

        assert result["name"] == "Test Profile"
        assert result["model"] == "sonnet"

    def test_extract_json_with_markdown_code_block(self):
        """Should extract JSON from markdown code block."""
        from app.api.generation import extract_json_from_response

        text = '''```json
{"name": "Test Profile", "model": "sonnet"}
```'''
        result = extract_json_from_response(text)

        assert result["name"] == "Test Profile"
        assert result["model"] == "sonnet"

    def test_extract_json_with_surrounding_text(self):
        """Should extract JSON from text with surrounding content."""
        from app.api.generation import extract_json_from_response

        text = '''Here is the configuration:
{"name": "Test Profile", "model": "sonnet"}
That's the generated config!'''
        result = extract_json_from_response(text)

        assert result["name"] == "Test Profile"
        assert result["model"] == "sonnet"

    def test_extract_nested_json(self):
        """Should handle nested JSON objects."""
        from app.api.generation import extract_json_from_response

        text = '{"name": "Test", "config": {"nested": true, "value": 123}}'
        result = extract_json_from_response(text)

        assert result["name"] == "Test"
        assert result["config"]["nested"] is True
        assert result["config"]["value"] == 123

    def test_no_json_raises_error(self):
        """Should raise ValueError when no JSON found."""
        from app.api.generation import extract_json_from_response

        with pytest.raises(ValueError, match="No JSON object found"):
            extract_json_from_response("No JSON here at all")

    def test_invalid_json_raises_error(self):
        """Should raise ValueError for invalid JSON."""
        from app.api.generation import extract_json_from_response

        with pytest.raises(ValueError, match="Invalid JSON"):
            extract_json_from_response('{"name": "Test", invalid}')

    def test_extract_json_with_arrays(self):
        """Should handle JSON with arrays."""
        from app.api.generation import extract_json_from_response

        text = '{"tools": ["Read", "Write", "Bash"], "model": "haiku"}'
        result = extract_json_from_response(text)

        assert result["tools"] == ["Read", "Write", "Bash"]
        assert result["model"] == "haiku"

    def test_extract_json_with_braces_in_strings(self):
        """Should correctly handle braces inside JSON string values."""
        from app.api.generation import extract_json_from_response

        # This is the bug case - braces inside string values should not
        # affect the brace counting for finding JSON boundaries
        text = '{"name": "Advanced {Pro} Profile", "description": "Uses {curly} braces"}'
        result = extract_json_from_response(text)

        assert result["name"] == "Advanced {Pro} Profile"
        assert result["description"] == "Uses {curly} braces"

    def test_extract_json_with_nested_braces_in_strings(self):
        """Should handle multiple nested brace patterns in strings."""
        from app.api.generation import extract_json_from_response

        text = '{"prompt": "Format: {{name}} and {{value}}", "model": "sonnet"}'
        result = extract_json_from_response(text)

        assert result["prompt"] == "Format: {{name}} and {{value}}"
        assert result["model"] == "sonnet"

    def test_extract_json_with_escaped_quotes(self):
        """Should handle escaped quotes in string values."""
        from app.api.generation import extract_json_from_response

        text = '{"name": "Test \\"Quoted\\" Name", "value": "ok"}'
        result = extract_json_from_response(text)

        assert result["name"] == 'Test "Quoted" Name'
        assert result["value"] == "ok"

    def test_extract_json_with_escaped_quotes_and_braces(self):
        """Should handle both escaped quotes and braces in strings."""
        from app.api.generation import extract_json_from_response

        text = '{"prompt": "Use format \\"{name}\\": {value}", "model": "haiku"}'
        result = extract_json_from_response(text)

        assert result["prompt"] == 'Use format "{name}": {value}'
        assert result["model"] == "haiku"

    def test_extract_json_complex_nested_with_braces_in_strings(self):
        """Should handle complex nested objects with braces in string values."""
        from app.api.generation import extract_json_from_response

        text = '''{"config": {"template": "Hello {user}!", "format": "{key}: {val}"}, "name": "Test"}'''
        result = extract_json_from_response(text)

        assert result["config"]["template"] == "Hello {user}!"
        assert result["config"]["format"] == "{key}: {val}"
        assert result["name"] == "Test"


class TestGenerateWithClaude:
    """Test the provider-adapter integration.

    AI generation runs through the SAME seam as chat
    (``app.core.provider.factory.make_adapter`` -> HarnessAdapter), so a
    Claude-subscription binding takes the delegated ``claude.exe`` leg and
    anything else takes the fluid native leg. There is no direct-HTTP path and
    no direct SDK ``query()`` call. These tests patch the profile lookup, the
    options builder and ``make_adapter`` so they need no DB and no subprocess.
    """

    @pytest.mark.asyncio
    async def test_generate_with_claude_success(self):
        """Should return text collected from the adapter's event stream."""
        from app.api.generation import generate_with_claude

        adapter = FakeAdapter(text_turn('{"name": "Generated"}'))
        with patch(
                    "app.core.generation_options.text_generation_config",
                    return_value={"model": "sonnet", "provider": None, "effort": None},
                ), \
                patch("app.core.generation_options.build_generation_options", return_value=MagicMock()), \
                patch_adapter(adapter):
            result = await generate_with_claude("Test prompt")

        assert result == '{"name": "Generated"}'
        assert adapter.connected is True
        assert adapter.prompts == ["Test prompt"]
        assert adapter.disconnect_calls == 1

    @pytest.mark.asyncio
    async def test_generate_with_claude_writes_usage_row(self):
        """A successful turn writes one usage_log row (session/profile NULL).

        Regression: the accounting block referenced an unbound ``profile``
        name, so every generation raised NameError inside the best-effort
        try and no usage was ever recorded.
        """
        from app.api.generation import generate_with_claude
        from app.core.provider.events import ResultMessage

        result = ResultMessage(
            subtype="success",
            is_error=False,
            usage={"input_tokens": 12, "output_tokens": 7},
            total_cost_usd=0.0042,
            duration_ms=321,
        )
        adapter = FakeAdapter([assistant_text("fix/thing"), result])
        log_usage = MagicMock()
        with patch(
                    "app.core.generation_options.text_generation_config",
                    return_value={"model": "sonnet", "provider": None, "effort": None},
                ),                 patch("app.core.generation_options.build_generation_options", return_value=MagicMock()),                 patch("app.api.generation.database.log_usage", log_usage),                 patch_adapter(adapter):
            text = await generate_with_claude("Test prompt")

        assert text == "fix/thing"
        log_usage.assert_called_once()
        kwargs = log_usage.call_args.kwargs
        assert kwargs["session_id"] is None
        assert kwargs["profile_id"] is None
        assert kwargs["model"] == "sonnet"
        assert kwargs["tokens_in"] == 12
        assert kwargs["tokens_out"] == 7
        assert kwargs["cost_usd"] == 0.0042
        assert kwargs["duration_ms"] == 321

    @pytest.mark.asyncio
    async def test_generate_with_claude_uses_app_local_event_classes(self):
        """Regression guard: the loop must match app.core.provider.events.

        The adapter emits the app-local mirror classes. If the isinstance
        checks were left pointing at ``claude_agent_sdk``'s classes the loop
        would silently collect nothing and generation would return "".
        """
        import app.api.generation as generation_mod
        from app.core.provider import events as provider_events

        assert generation_mod.AssistantMessage is provider_events.AssistantMessage
        assert generation_mod.TextBlock is provider_events.TextBlock
        assert generation_mod.ResultMessage is provider_events.ResultMessage

    @pytest.mark.asyncio
    async def test_generate_with_claude_with_model(self):
        """Should forward the model override into build_generation_options."""
        from app.api.generation import generate_with_claude

        adapter = FakeAdapter(text_turn('{"result": "ok"}'))
        with patch(
                    "app.core.generation_options.text_generation_config",
                    return_value={"model": "sonnet", "provider": None, "effort": None},
                ), \
                patch("app.core.generation_options.build_generation_options", return_value=MagicMock()) as mock_build, \
                patch_adapter(adapter):
            result = await generate_with_claude("Test prompt", model="opus")

        assert '{"result": "ok"}' in result
        # The model override flows to the options builder, not an HTTP call.
        assert mock_build.call_args.kwargs.get("model_override") == "opus"

    @pytest.mark.asyncio
    async def test_generate_with_claude_stops_at_result_message(self):
        """The ResultMessage is turn-terminal — later events are not collected."""
        from app.api.generation import generate_with_claude

        adapter = FakeAdapter([
            assistant_text("first"),
            success_result(),
            assistant_text("after-the-end"),
        ])
        with patch(
                    "app.core.generation_options.text_generation_config",
                    return_value={"model": "sonnet", "provider": None, "effort": None},
                ), \
                patch("app.core.generation_options.build_generation_options", return_value=MagicMock()), \
                patch_adapter(adapter):
            result = await generate_with_claude("Test prompt")

        assert result == "first"

    @pytest.mark.asyncio
    async def test_generate_with_claude_adapter_error(self):
        """Should raise HTTPException(500) when the adapter turn fails."""
        from app.api.generation import generate_with_claude
        from fastapi import HTTPException

        adapter = FakeAdapter(send_error=RuntimeError("harness connection failed"))
        with patch(
                    "app.core.generation_options.text_generation_config",
                    return_value={"model": "sonnet", "provider": None, "effort": None},
                ), \
                patch("app.core.generation_options.build_generation_options", return_value=MagicMock()), \
                patch_adapter(adapter):
            with pytest.raises(HTTPException) as exc_info:
                await generate_with_claude("Test prompt")

        assert exc_info.value.status_code == 500
        # api_error_handler("run AI generation") -> "Failed to run AI generation"
        assert "run AI generation" in exc_info.value.detail
        # The adapter is still torn down on the failure path.
        assert adapter.disconnect_calls == 1

    @pytest.mark.asyncio
    async def test_generate_with_claude_error_result_message_surfaces(self):
        """An is_error ResultMessage must not read back as an empty generation."""
        from app.api.generation import generate_with_claude
        from fastapi import HTTPException

        adapter = FakeAdapter([error_result("provider rejected the request")])
        with patch(
                    "app.core.generation_options.text_generation_config",
                    return_value={"model": "sonnet", "provider": None, "effort": None},
                ), \
                patch("app.core.generation_options.build_generation_options", return_value=MagicMock()), \
                patch_adapter(adapter):
            with pytest.raises(HTTPException) as exc_info:
                await generate_with_claude("Test prompt")

        assert exc_info.value.status_code == 500
        assert adapter.disconnect_calls == 1

    @pytest.mark.asyncio
    async def test_generate_with_claude_timeout_is_504_and_disconnects(self):
        """A stuck leg must 504 quickly AND still tear the adapter down."""
        from app.api.generation import generate_with_claude
        from fastapi import HTTPException

        adapter = FakeAdapter(hang=True)
        with patch(
                    "app.core.generation_options.text_generation_config",
                    return_value={"model": "sonnet", "provider": None, "effort": None},
                ), \
                patch("app.core.generation_options.build_generation_options", return_value=MagicMock()), \
                patch_adapter(adapter):
            with pytest.raises(HTTPException) as exc_info:
                await generate_with_claude("Test prompt", timeout_seconds=0.05)

        assert exc_info.value.status_code == 504
        assert exc_info.value.detail == "AI generation timed out"
        assert adapter.disconnect_calls == 1

    @pytest.mark.asyncio
    async def test_generate_with_claude_never_uses_http_path(self):
        """Should NOT call the removed direct-HTTP generation path.

        Regression guard for the refactor: even if a stored anthropic_api_key
        exists, generation must go through the adapter seam, never an
        api.anthropic.com HTTP POST from this module. We assert the adapter is
        used and that no HTTP generation symbols are importable here.
        """
        import app.api.generation as generation_mod
        from app.api.generation import generate_with_claude

        # The removed HTTP-path symbols must no longer exist on the module.
        assert not hasattr(generation_mod, "generate_via_http")
        assert not hasattr(generation_mod, "is_http_generation_available")
        assert not hasattr(generation_mod, "_extract_system_prompt_text")
        # The SDK one-shot entrypoint is gone from this module entirely.
        assert not hasattr(generation_mod, "query")

        adapter = FakeAdapter(text_turn('{"ok": true}'))
        with patch(
                    "app.core.generation_options.text_generation_config",
                    return_value={"model": "sonnet", "provider": None, "effort": None},
                ), \
                patch("app.core.generation_options.build_generation_options", return_value=MagicMock()), \
                patch_adapter(adapter), \
                patch("httpx.AsyncClient.post", side_effect=AssertionError("HTTP path must not be used")):
            result = await generate_with_claude("Test prompt")

        assert result == '{"ok": true}'
        assert adapter.connected is True

    @pytest.mark.asyncio
    async def test_generate_with_claude_provider_config_error_is_400(self):
        """FIX 7: a ProviderConfigError from build_generation_options (deleted /
        disabled / undecryptable provider) surfaces as HTTP 400 carrying the
        provider message — NOT a generic 500 'Internal server error:' (the call
        is outside the api_error_handler block)."""
        from app.api.generation import generate_with_claude
        from app.core.environment import ProviderConfigError
        from fastapi import HTTPException

        with patch(
                    "app.core.generation_options.text_generation_config",
                    return_value={"model": "sonnet", "provider": None, "effort": None},
                ), \
                patch(
                    "app.core.generation_options.build_generation_options",
                    side_effect=ProviderConfigError(
                        "Model provider 'op' is disabled."
                    ),
                ):
            with pytest.raises(HTTPException) as exc_info:
                await generate_with_claude("Test prompt")

        assert exc_info.value.status_code == 400
        assert "Model provider 'op' is disabled." in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_generate_with_claude_credential_error_is_400(self):
        """A provider-credential failure at connect must be an actionable 400.

        ProviderCredentialError subclasses ProviderConfigError, so the connect
        guard catches both shapes.
        """
        from app.api.generation import generate_with_claude
        from app.core.provider.fluid_credentials import ProviderCredentialError
        from fastapi import HTTPException

        adapter = FakeAdapter(
            connect_error=ProviderCredentialError(
                "Model provider 'gpt-row' has no credentials configured."
            )
        )
        with patch(
                    "app.core.generation_options.text_generation_config",
                    return_value={"model": "sonnet", "provider": None, "effort": None},
                ), \
                patch("app.core.generation_options.build_generation_options", return_value=MagicMock()), \
                patch_adapter(adapter):
            with pytest.raises(HTTPException) as exc_info:
                await generate_with_claude("Test prompt")

        assert exc_info.value.status_code == 400
        assert "no credentials configured" in exc_info.value.detail
        assert adapter.disconnect_calls == 1

    @pytest.mark.asyncio
    async def test_generate_with_claude_does_not_depend_on_stored_key(self):
        """Generation must succeed whether or not an Anthropic key is stored.

        The key is now an auth credential consumed by the options/env builder,
        not a precondition for generation. With the key lookup patched to
        return a value, generation still routes through the adapter unchanged.
        """
        from app.api.generation import generate_with_claude

        adapter = FakeAdapter(text_turn('{"name": "X"}'))
        with patch(
                    "app.core.generation_options.text_generation_config",
                    return_value={"model": "sonnet", "provider": None, "effort": None},
                ), \
                patch("app.core.generation_options.build_generation_options", return_value=MagicMock()), \
                patch("app.api.settings_crypto.get_decrypted_api_key", return_value="sk-ant-stored"), \
                patch_adapter(adapter):
            result = await generate_with_claude("Test prompt")

        assert result == '{"name": "X"}'


@pytest.mark.real_agent_gate
class TestGenerationCredentialGate:
    """Generation on the Claude Code CLI leg passes the same gate as a turn."""

    @pytest.mark.asyncio
    async def test_claude_off_refuses_generation_on_the_cli_leg(self, mock_db):
        from fastapi import HTTPException
        from app.api.generation import generate_with_claude
        from app.core import claude_auth_mode, credential_gate

        claude_auth_mode.set_mode("off")
        adapter = FakeAdapter(text_turn("never"))
        with patch(
                    "app.core.generation_options.text_generation_config",
                    return_value={"model": "sonnet", "provider": None, "effort": None},
                ),                 patch_adapter(adapter):
            with pytest.raises(HTTPException) as exc_info:
                await generate_with_claude("Test prompt")

        assert exc_info.value.status_code == 503
        assert exc_info.value.detail == credential_gate.OFF_MESSAGE
        assert exc_info.value.headers == {"X-Error-Code": credential_gate.SETUP_CODE}
        assert adapter.connected is False and adapter.prompts == []

    @pytest.mark.asyncio
    async def test_claude_off_leaves_a_provider_leg_alone(self, mock_db):
        from app.api.generation import generate_with_claude
        from app.core import claude_auth_mode

        claude_auth_mode.set_mode("off")
        adapter = FakeAdapter(text_turn("feat: thing"))
        with patch(
                    "app.core.generation_options.text_generation_config",
                    return_value={"model": "gpt-x", "provider": "prov-1", "effort": None},
                ),                 patch("app.core.generation_options.build_generation_options", return_value=MagicMock()),                 patch("app.core.credential_gate.leg_for_options", return_value="openai_responses"),                 patch_adapter(adapter):
            assert await generate_with_claude("Test prompt") == "feat: thing"
        assert adapter.connected is True


class TestGenerateProfileEndpoint:
    """Test the profile generation endpoint."""

    def test_generate_profile_requires_auth(self, client):
        """Should require authentication."""
        response = client.post(
            "/api/v1/generate/profile",
            json={"description": "A Python development profile"}
        )

        # Without auth, should get 401 or 403
        assert response.status_code in [401, 403]

    def test_generate_profile_empty_description(self, authenticated_client):
        """Should reject empty description."""
        response = authenticated_client.post(
            "/api/v1/generate/profile",
            json={"description": ""}
        )

        assert response.status_code == 422  # Validation error

    def test_generate_profile_description_too_short(self, authenticated_client):
        """Should reject description that's too short."""
        response = authenticated_client.post(
            "/api/v1/generate/profile",
            json={"description": "short"}
        )

        assert response.status_code == 422  # Validation error (min_length=10)

    def test_generate_profile_success(self, authenticated_client):
        """Should generate profile configuration from description."""
        mock_response = '''{
            "name": "Python Developer",
            "description": "A profile for Python development",
            "model": "sonnet",
            "permission_mode": "default",
            "system_prompt_content": "You are a Python development assistant.",
            "allowed_tools": null,
            "disallowed_tools": null,
            "thinking_mode": "think",
            "system_prompt_preset": "claude_code"
        }'''

        with patch_adapter(FakeAdapter(text_turn(mock_response))):
            response = authenticated_client.post(
                "/api/v1/generate/profile",
                json={
                    "description": "A Python developer profile with extended thinking",
                    "skip_clarification": True
                }
            )

        assert response.status_code == 200
        data = response.json()

        # Check response wrapper
        assert data["status"] == "complete"
        assert data["generation_type"] == "profile"

        # Check export format in result
        result = data["result"]
        assert result["version"] == "1.0"
        assert result["type"] == "ai-shuffle-agent"
        assert "exported_at" in result
        assert "agent" in result

        # Check agent data
        agent = result["agent"]
        assert agent["name"] == "Python Developer"
        assert agent["system_prompt_content"] == "You are a Python development assistant."
        # A model / permission mode the generator still emits must NOT survive
        # into the generated agent: an agent carries no run binding, and a chat
        # picks both when it starts.
        assert agent["model"] is None
        assert agent["permission_mode"] is None
        # Dead SDK-era fields emitted by the model must not survive into the
        # generated config — the fluid harness ignores them (ruling A12)
        assert "thinking_mode" not in agent
        assert "system_prompt_preset" not in agent
        assert "system_prompt_type" not in agent
        # setting_sources is a live field again (fluid honors it as its scope
        # filter); the generator didn't set one, so it exports as null.
        assert agent["setting_sources"] is None

    def test_generate_profile_with_model_param(self, authenticated_client):
        """Should accept optional model parameter."""
        mock_response = '{"name": "Test", "description": "Test profile", "model": "sonnet"}'

        with patch_adapter(FakeAdapter(text_turn(mock_response))):
            response = authenticated_client.post(
                "/api/v1/generate/profile",
                json={
                    "description": "A test profile for unit testing",
                    "model": "opus",
                    "skip_clarification": True
                }
            )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "complete"

    def test_generate_profile_invalid_ai_response(self, authenticated_client):
        """Should handle invalid AI response gracefully."""
        with patch_adapter(FakeAdapter(text_turn("This is not valid JSON at all"))):
            response = authenticated_client.post(
                "/api/v1/generate/profile",
                json={
                    "description": "A profile that will fail to generate",
                    "skip_clarification": True
                }
            )

        assert response.status_code == 500
        assert "Failed to parse" in response.json()["detail"]

    def test_generate_profile_missing_name(self, authenticated_client):
        """Should handle AI response missing required name field."""
        mock_response = '{"description": "No name field", "model": "sonnet"}'

        with patch_adapter(FakeAdapter(text_turn(mock_response))):
            response = authenticated_client.post(
                "/api/v1/generate/profile",
                json={
                    "description": "A profile without a generated name",
                    "skip_clarification": True
                }
            )

        assert response.status_code == 500
        assert "did not generate a profile name" in response.json()["detail"]


class TestGenerateSubagentEndpoint:
    """Test the subagent generation endpoint."""

    def test_generate_subagent_requires_auth(self, client):
        """Should require authentication."""
        response = client.post(
            "/api/v1/generate/subagent",
            json={"description": "A code review subagent"}
        )

        # Without auth, should get 401 or 403
        assert response.status_code in [401, 403]

    def test_generate_subagent_empty_description(self, authenticated_client):
        """Should reject empty description."""
        response = authenticated_client.post(
            "/api/v1/generate/subagent",
            json={"description": ""}
        )

        assert response.status_code == 422  # Validation error

    def test_generate_subagent_success(self, authenticated_client):
        """Should generate subagent configuration from description."""
        mock_response = '''{
            "id": "code-reviewer",
            "name": "Code Reviewer",
            "description": "Use this agent to review code for bugs and style issues",
            "prompt": "You are an expert code reviewer. Analyze the provided code for bugs, security issues, and style problems.",
            "tools": ["Read", "Glob", "Grep"],
            "model": "sonnet"
        }'''

        with patch_adapter(FakeAdapter(text_turn(mock_response))):
            response = authenticated_client.post(
                "/api/v1/generate/subagent",
                json={
                    "description": "A code review agent that checks for bugs and style issues",
                    "skip_clarification": True
                }
            )

        assert response.status_code == 200
        data = response.json()

        # Check response wrapper
        assert data["status"] == "complete"
        assert data["generation_type"] == "subagent"

        # Check export format in result
        result = data["result"]
        assert result["version"] == "1.0"
        assert result["type"] == "ai-shuffle-subagent"
        assert "exported_at" in result
        assert "subagent" in result

        # Check subagent data
        subagent = result["subagent"]
        assert subagent["id"] == "code-reviewer"
        assert subagent["name"] == "Code Reviewer"
        assert "description" in subagent
        assert "prompt" in subagent
        assert subagent["tools"] == ["Read", "Glob", "Grep"]
        assert subagent["model"] == "sonnet"

    def test_generate_subagent_sanitizes_id(self, authenticated_client):
        """Should sanitize generated subagent ID."""
        mock_response = '''{
            "id": "Code_Review Agent!!",
            "name": "Code Review Agent",
            "description": "Reviews code",
            "prompt": "You review code.",
            "tools": null,
            "model": null
        }'''

        with patch_adapter(FakeAdapter(text_turn(mock_response))):
            response = authenticated_client.post(
                "/api/v1/generate/subagent",
                json={
                    "description": "A code review agent that sanitizes IDs properly",
                    "skip_clarification": True
                }
            )

        assert response.status_code == 200
        data = response.json()

        # ID should be sanitized to lowercase with hyphens only
        subagent = data["result"]["subagent"]
        assert subagent["id"] == "code-review-agent"

    def test_generate_subagent_missing_required_fields(self, authenticated_client):
        """Should handle AI response missing required fields."""
        # Missing id, name, description, and prompt
        mock_response = '{"tools": ["Read"]}'

        with patch_adapter(FakeAdapter(text_turn(mock_response))):
            response = authenticated_client.post(
                "/api/v1/generate/subagent",
                json={
                    "description": "An incomplete subagent that is missing fields",
                    "skip_clarification": True
                }
            )

        assert response.status_code == 500
        assert "did not generate" in response.json()["detail"]

    def test_generate_subagent_with_model_param(self, authenticated_client):
        """Should accept optional model parameter."""
        mock_response = '''{
            "id": "test-agent",
            "name": "Test Agent",
            "description": "For testing",
            "prompt": "You are a test agent.",
            "tools": null,
            "model": "haiku"
        }'''

        with patch_adapter(FakeAdapter(text_turn(mock_response))):
            response = authenticated_client.post(
                "/api/v1/generate/subagent",
                json={
                    "description": "A test subagent for unit testing purposes",
                    "model": "sonnet",
                    "skip_clarification": True
                }
            )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "complete"

    def test_generate_subagent_invalid_ai_response(self, authenticated_client):
        """Should handle invalid AI response gracefully."""
        with patch_adapter(FakeAdapter(text_turn("Not JSON"))):
            response = authenticated_client.post(
                "/api/v1/generate/subagent",
                json={
                    "description": "A subagent that will fail to generate properly",
                    "skip_clarification": True
                }
            )

        assert response.status_code == 500
        assert "Failed to parse" in response.json()["detail"]


class TestGenerationRequestModels:
    """Test request model validation."""

    def test_profile_request_min_length(self, authenticated_client):
        """Description must be at least 10 characters."""
        response = authenticated_client.post(
            "/api/v1/generate/profile",
            json={"description": "short"}
        )
        assert response.status_code == 422

    def test_profile_request_max_length(self, authenticated_client):
        """Description must be at most 5000 characters."""
        response = authenticated_client.post(
            "/api/v1/generate/profile",
            json={"description": "x" * 5001}
        )
        assert response.status_code == 422

    def test_profile_request_reference_material_max_length(self, authenticated_client):
        """Reference material must be at most 15000 characters."""
        response = authenticated_client.post(
            "/api/v1/generate/profile",
            json={
                "description": "A valid description for testing",
                "reference_material": "x" * 15001
            }
        )
        assert response.status_code == 422

    def test_subagent_request_valid_length(self, authenticated_client):
        """Description within valid length should be accepted (if generation works)."""
        mock_response = '''{
            "id": "test",
            "name": "Test",
            "description": "Test agent",
            "prompt": "Test prompt",
            "tools": null,
            "model": null
        }'''

        with patch_adapter(FakeAdapter(text_turn(mock_response))):
            response = authenticated_client.post(
                "/api/v1/generate/subagent",
                json={
                    "description": "A valid description that is long enough",
                    "skip_clarification": True
                }
            )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "complete"
