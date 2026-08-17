"""
Tests for the AI generation module.

Tests the domain detection, template matching, clarification logic,
and prompt building functionality.
"""

import pytest


class TestToolMapping:
    """Test the tool mapping and domain detection functions."""

    def test_detect_domain_security(self):
        """Should detect security domain from keywords."""
        from app.core.generation.tool_mapping import detect_domain

        assert detect_domain("A code reviewer for security vulnerabilities") == "code-review"
        assert detect_domain("Security audit tool for OWASP top 10") == "security"

    def test_detect_domain_testing(self):
        """Should detect testing domain from keywords."""
        from app.core.generation.tool_mapping import detect_domain

        assert detect_domain("A pytest test writer for Python") == "testing"
        assert detect_domain("Unit testing agent with coverage") == "testing"

    def test_detect_domain_documentation(self):
        """Should detect documentation domain from keywords."""
        from app.core.generation.tool_mapping import detect_domain

        assert detect_domain("Documentation writer for API docs") == "documentation"
        assert detect_domain("README generator") == "documentation"

    def test_detect_domain_exploration(self):
        """Should detect exploration domain from keywords."""
        from app.core.generation.tool_mapping import detect_domain

        assert detect_domain("Codebase explorer to find files") == "exploration"
        assert detect_domain("Search and navigate the codebase") == "exploration"

    def test_detect_domain_general(self):
        """Should default to general for vague descriptions."""
        from app.core.generation.tool_mapping import detect_domain

        assert detect_domain("A helpful assistant") == "general"
        assert detect_domain("A tool") == "general"

    def test_get_tools_for_domain_code_review(self):
        """Should return read-only tools for code review."""
        from app.core.generation.tool_mapping import get_tools_for_domain

        tools = get_tools_for_domain("code-review")
        assert tools is not None
        assert tools == ["Read", "Grep", "Glob"]
        assert "Write" not in tools
        assert "Edit" not in tools

    def test_get_tools_for_domain_testing(self):
        """Should return full tools for testing."""
        from app.core.generation.tool_mapping import get_tools_for_domain

        tools = get_tools_for_domain("testing")
        assert tools is not None
        assert "Read" in tools
        assert "Write" in tools
        assert "Edit" in tools
        assert "Bash" in tools

    def test_get_tools_for_domain_general(self):
        """Should return None (inherit all) for general domain."""
        from app.core.generation.tool_mapping import get_tools_for_domain

        tools = get_tools_for_domain("general")
        assert tools is None

    def test_get_model_for_domain(self):
        """Should return appropriate model for each domain."""
        from app.core.generation.tool_mapping import get_model_for_domain

        assert get_model_for_domain("exploration") == "haiku"  # Fast
        assert get_model_for_domain("security") == "opus"  # Complex analysis
        assert get_model_for_domain("code-review") == "sonnet"  # Balanced

    def test_get_all_available_tools(self):
        """Should return all available tools."""
        from app.core.generation.tool_mapping import get_all_available_tools

        tools = get_all_available_tools()
        assert "Read" in tools
        assert "Write" in tools
        assert "Edit" in tools
        assert "Bash" in tools
        assert "Grep" in tools
        assert "Glob" in tools


class TestTemplates:
    """Test the domain template functions."""

    def test_get_template_by_id(self):
        """Should retrieve template by ID."""
        from app.core.generation.templates import get_template

        template = get_template("code-reviewer")
        assert template is not None
        assert template["id"] == "code-reviewer"
        assert "tools" in template
        assert "model" in template

    def test_get_template_returns_none_for_invalid(self):
        """Should return None for invalid template ID."""
        from app.core.generation.templates import get_template

        template = get_template("nonexistent-template")
        assert template is None

    def test_get_template_by_keywords(self):
        """Should find template matching keywords."""
        from app.core.generation.templates import get_template_by_keywords

        template = get_template_by_keywords("security code review")
        assert template is not None
        assert template["id"] == "code-reviewer"

        template = get_template_by_keywords("write tests pytest")
        assert template is not None
        assert template["id"] == "test-writer"

    def test_get_template_by_keywords_returns_none(self):
        """Should return None when no keywords match."""
        from app.core.generation.templates import get_template_by_keywords

        template = get_template_by_keywords("something completely unrelated xyz")
        assert template is None

    def test_get_all_template_ids(self):
        """Should return all template IDs."""
        from app.core.generation.templates import get_all_template_ids

        ids = get_all_template_ids()
        assert "code-reviewer" in ids
        assert "test-writer" in ids
        assert "explorer" in ids
        assert "planner" in ids
        assert len(ids) >= 10  # We defined at least 10 templates


class TestClarification:
    """Test the clarification logic."""

    def test_is_too_short_triggers_clarification(self):
        """Should trigger clarification for very short descriptions."""
        from app.core.generation.clarification import should_ask_clarification

        assert should_ask_clarification("a tool") is True
        assert should_ask_clarification("helper") is True

    def test_detailed_description_no_clarification(self):
        """Should not trigger clarification for detailed descriptions."""
        from app.core.generation.clarification import should_ask_clarification

        description = "A Python test writer that uses pytest and generates comprehensive unit tests with fixtures, mocks, and edge case coverage"
        assert should_ask_clarification(description) is False

    def test_analyze_description_flags(self):
        """Should analyze description and return flags."""
        from app.core.generation.clarification import analyze_description

        # Short description
        analysis = analyze_description("a tool")
        assert analysis["too_short"] is True

        # Vague description
        analysis = analyze_description("a nice helper assistant")
        assert analysis["vague_purpose"] is True

        # Detailed description
        analysis = analyze_description(
            "A Python test writer using pytest with comprehensive fixtures and mocking support"
        )
        assert analysis["too_short"] is False

    def test_get_clarification_questions(self):
        """Should return questions for unclear descriptions."""
        from app.core.generation.clarification import get_clarification_questions

        questions = get_clarification_questions("a tool")
        assert len(questions) > 0
        assert all(q.id for q in questions)
        assert all(q.question for q in questions)

    def test_clarification_questions_have_options(self):
        """Clarification questions should have options when applicable."""
        from app.core.generation.clarification import get_clarification_questions

        questions = get_clarification_questions("a tool")

        # At least one question should have options
        has_options = any(q.options and len(q.options) > 0 for q in questions)
        assert has_options

    def test_format_clarification_context(self):
        """Should format answers into context string."""
        from app.core.generation.clarification import format_clarification_context

        context = format_clarification_context(
            {"vague_purpose": "Code review"},
            "a tool"
        )

        assert "a tool" in context
        assert "Code review" in context


class TestExamples:
    """Test the few-shot examples."""

    def test_subagent_examples_exist(self):
        """Should have subagent examples defined."""
        from app.core.generation.examples import SUBAGENT_EXAMPLES

        assert len(SUBAGENT_EXAMPLES) >= 3

    def test_profile_examples_exist(self):
        """Should have profile examples defined."""
        from app.core.generation.examples import PROFILE_EXAMPLES

        assert len(PROFILE_EXAMPLES) >= 2

    def test_subagent_examples_have_required_fields(self):
        """Subagent examples should have all required fields."""
        from app.core.generation.examples import SUBAGENT_EXAMPLES

        for example in SUBAGENT_EXAMPLES:
            output = example["output"]
            assert "id" in output
            assert "name" in output
            assert "description" in output
            assert "prompt" in output
            assert len(output["prompt"]) > 200  # Should be comprehensive

    def test_profile_examples_have_required_fields(self):
        """Profile examples should have all required fields."""
        from app.core.generation.examples import PROFILE_EXAMPLES

        for example in PROFILE_EXAMPLES:
            output = example["output"]
            assert "name" in output
            assert "model" in output
            assert "permission_mode" in output

    def test_get_subagent_examples_text(self):
        """Should format examples as text."""
        from app.core.generation.examples import get_subagent_examples_text

        text = get_subagent_examples_text()
        assert "Example 1" in text
        assert "Example 2" in text
        assert "User Description" in text
        assert "Generated Output" in text

    def test_get_profile_examples_text(self):
        """Should format profile examples as text."""
        from app.core.generation.examples import get_profile_examples_text

        text = get_profile_examples_text()
        assert "Example 1" in text
        assert "User Description" in text
        assert "Generated Output" in text


class TestPrompts:
    """Test the prompt building functions."""

    def test_build_subagent_generation_prompt(self):
        """Should build comprehensive subagent generation prompt."""
        from app.core.generation.prompts import build_subagent_generation_prompt

        prompt = build_subagent_generation_prompt("A code review agent")

        # Should contain key sections
        assert "subagent" in prompt.lower()
        assert "A code review agent" in prompt
        assert "id" in prompt
        assert "name" in prompt
        assert "description" in prompt
        assert "prompt" in prompt
        assert "tools" in prompt
        assert "model" in prompt

        # Should contain examples
        assert "Example" in prompt

        # Should contain tool guidance
        assert "Read" in prompt
        assert "Grep" in prompt

    def test_build_subagent_prompt_with_clarification(self):
        """Should include clarification context when provided."""
        from app.core.generation.prompts import build_subagent_generation_prompt

        prompt = build_subagent_generation_prompt(
            "A tool",
            clarification_context="User wants: Code review for security"
        )

        assert "User wants: Code review for security" in prompt

    def test_build_profile_generation_prompt(self):
        """Should build comprehensive profile generation prompt."""
        from app.core.generation.prompts import build_profile_generation_prompt

        prompt = build_profile_generation_prompt("A Python development profile")

        # Should contain key sections
        assert "profile" in prompt.lower()
        assert "A Python development profile" in prompt
        assert "model" in prompt
        assert "permission_mode" in prompt
        assert "system_prompt_content" in prompt

        # Dead SDK-era fields must not be offered to the generator —
        # the fluid harness ignores them (ruling A12)
        assert "thinking_mode" not in prompt
        assert "system_prompt_preset" not in prompt
        assert "system_prompt_append" not in prompt
        assert "setting_sources" not in prompt
        assert "max_buffer_size" not in prompt
        assert "enable_file_checkpointing" not in prompt

        # Should contain examples
        assert "Example" in prompt

    def test_build_profile_prompt_with_clarification(self):
        """Should include clarification context when provided."""
        from app.core.generation.prompts import build_profile_generation_prompt

        prompt = build_profile_generation_prompt(
            "A profile",
            clarification_context="User wants: Fast exploration"
        )

        assert "User wants: Fast exploration" in prompt

    def test_prompts_contain_domain_templates(self):
        """Prompts should reference domain templates."""
        from app.core.generation.prompts import build_subagent_generation_prompt

        prompt = build_subagent_generation_prompt("A test agent")

        # Should mention some domain templates
        assert "code-review" in prompt.lower() or "reviewer" in prompt.lower()
        assert "test" in prompt.lower()


class TestGenerationModuleIntegration:
    """Integration tests for the generation module."""

    def test_module_exports(self):
        """Should export all expected functions."""
        from app.core.generation import (
            detect_domain,
            get_tools_for_domain,
            get_template,
            get_template_by_keywords,
            build_profile_generation_prompt,
            build_subagent_generation_prompt,
            should_ask_clarification,
            get_clarification_questions,
        )

        # All should be callable
        assert callable(detect_domain)
        assert callable(get_tools_for_domain)
        assert callable(get_template)
        assert callable(get_template_by_keywords)
        assert callable(build_profile_generation_prompt)
        assert callable(build_subagent_generation_prompt)
        assert callable(should_ask_clarification)
        assert callable(get_clarification_questions)

    def test_domain_to_tools_consistency(self):
        """Tools for detected domain should match domain tools."""
        from app.core.generation import detect_domain, get_tools_for_domain

        description = "A code reviewer for security"
        domain = detect_domain(description)
        tools = get_tools_for_domain(domain)

        # Code review domain should have read-only tools
        assert domain == "code-review"
        assert tools == ["Read", "Grep", "Glob"]

    def test_template_to_domain_consistency(self):
        """Template domain should match detected domain."""
        from app.core.generation import get_template, detect_domain

        template = get_template("security-auditor")
        assert template is not None
        assert template["domain"] == "security"

        # Detecting from security-related description should find security domain
        domain = detect_domain("security vulnerability scanner and auditor")
        assert domain == "security"
