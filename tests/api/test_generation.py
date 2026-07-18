"""
Tests for AI generation API endpoints.

Tests the profile and subagent generation functionality.
"""

import pytest
from unittest.mock import patch, MagicMock


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
    """Test the Claude SDK integration.

    AI generation always runs through the throwaway Claude Agent SDK
    ``query()`` subprocess — there is no direct-HTTP path. The subprocess
    authenticates the same way as the rest of Claude Code (the stored
    anthropic_api_key, when set, is injected as ANTHROPIC_API_KEY by the
    options builder; otherwise the OAuth account is used). These tests patch
    the profile lookup and options builder so they need no DB and assert the
    SDK path is the ONLY path.
    """

    @staticmethod
    def _mock_query_yielding(text):
        """Build an async-generator factory yielding one AssistantMessage."""
        from claude_agent_sdk import AssistantMessage, TextBlock

        mock_text_block = MagicMock(spec=TextBlock)
        mock_text_block.text = text
        mock_message = MagicMock(spec=AssistantMessage)
        mock_message.content = [mock_text_block]

        async def mock_query_gen(*args, **kwargs):
            yield mock_message

        return mock_query_gen

    @pytest.mark.asyncio
    async def test_generate_with_claude_success(self):
        """Should return text response from Claude via the SDK query() path."""
        from app.api.generation import generate_with_claude

        with patch("app.core.profiles.get_default_ai_profile", return_value=None), \
                patch("app.core.generation_options.build_generation_options", return_value=MagicMock()), \
                patch("app.api.generation.query", return_value=self._mock_query_yielding('{"name": "Generated"}')()):
            result = await generate_with_claude("Test prompt")

        assert result == '{"name": "Generated"}'

    @pytest.mark.asyncio
    async def test_generate_with_claude_with_model(self):
        """Should forward the model override into build_generation_options."""
        from app.api.generation import generate_with_claude

        with patch("app.core.profiles.get_default_ai_profile", return_value=None), \
                patch("app.core.generation_options.build_generation_options", return_value=MagicMock()) as mock_build, \
                patch("app.api.generation.query", return_value=self._mock_query_yielding('{"result": "ok"}')()):
            result = await generate_with_claude("Test prompt", model="opus")

        assert '{"result": "ok"}' in result
        # The model override flows to the options builder, not an HTTP call.
        assert mock_build.call_args.kwargs.get("model_override") == "opus"

    @pytest.mark.asyncio
    async def test_generate_with_claude_sdk_error(self):
        """Should raise HTTPException(500) when the SDK query() fails."""
        from app.api.generation import generate_with_claude
        from fastapi import HTTPException

        with patch("app.core.profiles.get_default_ai_profile", return_value=None), \
                patch("app.core.generation_options.build_generation_options", return_value=MagicMock()), \
                patch("app.api.generation.query", side_effect=RuntimeError("SDK connection failed")):
            with pytest.raises(HTTPException) as exc_info:
                await generate_with_claude("Test prompt")

        assert exc_info.value.status_code == 500
        # api_error_handler("run AI generation") -> "Failed to run AI generation"
        assert "run AI generation" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_generate_with_claude_never_uses_http_path(self):
        """Should NOT call the removed direct-HTTP generation path.

        Regression guard for the refactor: even if a stored anthropic_api_key
        exists, generation must go through query() (the SDK subprocess), never
        an api.anthropic.com HTTP POST. We assert query() is invoked and that
        no HTTP generation symbols are importable from the generation module.
        """
        import app.api.generation as generation_mod
        from app.api.generation import generate_with_claude

        # The removed HTTP-path symbols must no longer exist on the module.
        assert not hasattr(generation_mod, "generate_via_http")
        assert not hasattr(generation_mod, "is_http_generation_available")
        assert not hasattr(generation_mod, "_extract_system_prompt_text")

        query_called = {"n": 0}

        def _spy_query(*args, **kwargs):
            query_called["n"] += 1
            return self._mock_query_yielding('{"ok": true}')()

        # Even with a key "set", no HTTP client is constructed — query() runs.
        with patch("app.core.profiles.get_default_ai_profile", return_value=None), \
                patch("app.core.generation_options.build_generation_options", return_value=MagicMock()), \
                patch("app.api.generation.query", side_effect=_spy_query), \
                patch("httpx.AsyncClient.post", side_effect=AssertionError("HTTP path must not be used")):
            result = await generate_with_claude("Test prompt")

        assert result == '{"ok": true}'
        assert query_called["n"] == 1

    @pytest.mark.asyncio
    async def test_generate_with_claude_provider_config_error_is_400(self):
        """FIX 7: a ProviderConfigError from build_generation_options (deleted /
        disabled / undecryptable provider) surfaces as HTTP 400 carrying the
        provider message — NOT a generic 500 'Internal server error:' (the call
        is outside the api_error_handler block)."""
        from app.api.generation import generate_with_claude
        from app.core.environment import ProviderConfigError
        from fastapi import HTTPException

        with patch("app.core.profiles.get_default_ai_profile", return_value=None), \
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
    async def test_generate_with_claude_does_not_depend_on_stored_key(self):
        """Generation must succeed whether or not an Anthropic key is stored.

        The key is now an auth credential consumed by the options/env builder,
        not a precondition for generation. With the key lookup patched to
        return a value, generation still routes through query() unchanged.
        """
        from app.api.generation import generate_with_claude

        with patch("app.core.profiles.get_default_ai_profile", return_value=None), \
                patch("app.core.generation_options.build_generation_options", return_value=MagicMock()), \
                patch("app.api.settings_crypto.get_decrypted_api_key", return_value="sk-ant-stored"), \
                patch("app.api.generation.query", return_value=self._mock_query_yielding('{"name": "X"}')()):
            result = await generate_with_claude("Test prompt")

        assert result == '{"name": "X"}'


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
        from claude_agent_sdk import AssistantMessage, TextBlock

        mock_response = '''{
            "name": "Python Developer",
            "description": "A profile for Python development",
            "model": "sonnet",
            "permission_mode": "default",
            "thinking_mode": "think",
            "system_prompt_type": "preset",
            "system_prompt_preset": "claude_code",
            "allowed_tools": null,
            "disallowed_tools": null
        }'''

        mock_text_block = MagicMock(spec=TextBlock)
        mock_text_block.text = mock_response

        mock_message = MagicMock(spec=AssistantMessage)
        mock_message.content = [mock_text_block]

        async def mock_query_gen(*args, **kwargs):
            yield mock_message

        with patch("app.api.generation.query", return_value=mock_query_gen()):
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
        assert agent["model"] == "sonnet"

    def test_generate_profile_with_model_param(self, authenticated_client):
        """Should accept optional model parameter."""
        from claude_agent_sdk import AssistantMessage, TextBlock

        mock_response = '{"name": "Test", "description": "Test profile", "model": "sonnet"}'

        mock_text_block = MagicMock(spec=TextBlock)
        mock_text_block.text = mock_response

        mock_message = MagicMock(spec=AssistantMessage)
        mock_message.content = [mock_text_block]

        async def mock_query_gen(*args, **kwargs):
            yield mock_message

        with patch("app.api.generation.query", return_value=mock_query_gen()):
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
        from claude_agent_sdk import AssistantMessage, TextBlock

        mock_text_block = MagicMock(spec=TextBlock)
        mock_text_block.text = "This is not valid JSON at all"

        mock_message = MagicMock(spec=AssistantMessage)
        mock_message.content = [mock_text_block]

        async def mock_query_gen(*args, **kwargs):
            yield mock_message

        with patch("app.api.generation.query", return_value=mock_query_gen()):
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
        from claude_agent_sdk import AssistantMessage, TextBlock

        mock_response = '{"description": "No name field", "model": "sonnet"}'

        mock_text_block = MagicMock(spec=TextBlock)
        mock_text_block.text = mock_response

        mock_message = MagicMock(spec=AssistantMessage)
        mock_message.content = [mock_text_block]

        async def mock_query_gen(*args, **kwargs):
            yield mock_message

        with patch("app.api.generation.query", return_value=mock_query_gen()):
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
        from claude_agent_sdk import AssistantMessage, TextBlock

        mock_response = '''{
            "id": "code-reviewer",
            "name": "Code Reviewer",
            "description": "Use this agent to review code for bugs and style issues",
            "prompt": "You are an expert code reviewer. Analyze the provided code for bugs, security issues, and style problems.",
            "tools": ["Read", "Glob", "Grep"],
            "model": "sonnet"
        }'''

        mock_text_block = MagicMock(spec=TextBlock)
        mock_text_block.text = mock_response

        mock_message = MagicMock(spec=AssistantMessage)
        mock_message.content = [mock_text_block]

        async def mock_query_gen(*args, **kwargs):
            yield mock_message

        with patch("app.api.generation.query", return_value=mock_query_gen()):
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
        from claude_agent_sdk import AssistantMessage, TextBlock

        mock_response = '''{
            "id": "Code_Review Agent!!",
            "name": "Code Review Agent",
            "description": "Reviews code",
            "prompt": "You review code.",
            "tools": null,
            "model": null
        }'''

        mock_text_block = MagicMock(spec=TextBlock)
        mock_text_block.text = mock_response

        mock_message = MagicMock(spec=AssistantMessage)
        mock_message.content = [mock_text_block]

        async def mock_query_gen(*args, **kwargs):
            yield mock_message

        with patch("app.api.generation.query", return_value=mock_query_gen()):
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
        from claude_agent_sdk import AssistantMessage, TextBlock

        # Missing id, name, description, and prompt
        mock_response = '{"tools": ["Read"]}'

        mock_text_block = MagicMock(spec=TextBlock)
        mock_text_block.text = mock_response

        mock_message = MagicMock(spec=AssistantMessage)
        mock_message.content = [mock_text_block]

        async def mock_query_gen(*args, **kwargs):
            yield mock_message

        with patch("app.api.generation.query", return_value=mock_query_gen()):
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
        from claude_agent_sdk import AssistantMessage, TextBlock

        mock_response = '''{
            "id": "test-agent",
            "name": "Test Agent",
            "description": "For testing",
            "prompt": "You are a test agent.",
            "tools": null,
            "model": "haiku"
        }'''

        mock_text_block = MagicMock(spec=TextBlock)
        mock_text_block.text = mock_response

        mock_message = MagicMock(spec=AssistantMessage)
        mock_message.content = [mock_text_block]

        async def mock_query_gen(*args, **kwargs):
            yield mock_message

        with patch("app.api.generation.query", return_value=mock_query_gen()):
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
        from claude_agent_sdk import AssistantMessage, TextBlock

        mock_text_block = MagicMock(spec=TextBlock)
        mock_text_block.text = "Not JSON"

        mock_message = MagicMock(spec=AssistantMessage)
        mock_message.content = [mock_text_block]

        async def mock_query_gen(*args, **kwargs):
            yield mock_message

        with patch("app.api.generation.query", return_value=mock_query_gen()):
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
        from claude_agent_sdk import AssistantMessage, TextBlock

        mock_response = '''{
            "id": "test",
            "name": "Test",
            "description": "Test agent",
            "prompt": "Test prompt",
            "tools": null,
            "model": null
        }'''

        mock_text_block = MagicMock(spec=TextBlock)
        mock_text_block.text = mock_response

        mock_message = MagicMock(spec=AssistantMessage)
        mock_message.content = [mock_text_block]

        async def mock_query_gen(*args, **kwargs):
            yield mock_message

        with patch("app.api.generation.query", return_value=mock_query_gen()):
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
