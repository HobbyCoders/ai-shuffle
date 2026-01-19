"""
AI Generation Module

Provides intelligent generation of agent and subagent configurations
with domain awareness, smart tool mapping, and interactive clarification.
"""

from app.core.generation.tool_mapping import (
    TOOL_CATEGORIES,
    DOMAIN_TOOLS,
    AI_TOOLS_BY_DOMAIN,
    get_tools_for_domain,
    get_ai_tools_for_domain,
    detect_domain,
)

from app.core.generation.templates import (
    DOMAIN_TEMPLATES,
    get_template,
    get_template_by_keywords,
    get_all_template_ids,
)

from app.core.generation.examples import (
    SUBAGENT_EXAMPLES,
    PROFILE_EXAMPLES,
    get_subagent_examples_text,
    get_profile_examples_text,
)

from app.core.generation.prompts import (
    build_profile_generation_prompt,
    build_subagent_generation_prompt,
)

from app.core.generation.clarification import (
    analyze_description,
    get_clarification_questions,
    should_ask_clarification,
)

__all__ = [
    # Tool mapping
    "TOOL_CATEGORIES",
    "DOMAIN_TOOLS",
    "AI_TOOLS_BY_DOMAIN",
    "get_tools_for_domain",
    "get_ai_tools_for_domain",
    "detect_domain",
    # Templates
    "DOMAIN_TEMPLATES",
    "get_template",
    "get_template_by_keywords",
    "get_all_template_ids",
    # Examples
    "SUBAGENT_EXAMPLES",
    "PROFILE_EXAMPLES",
    "get_subagent_examples_text",
    "get_profile_examples_text",
    # Prompts
    "build_profile_generation_prompt",
    "build_subagent_generation_prompt",
    # Clarification
    "analyze_description",
    "get_clarification_questions",
    "should_ask_clarification",
]
