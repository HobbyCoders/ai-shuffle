"""
Tool Mapping for AI Generation

Maps domains to recommended tools, ensuring minimal necessary toolsets
for security and performance.
"""

import re
from typing import Dict, List, Optional, Set

# ============================================================================
# Tool Categories
# ============================================================================

TOOL_CATEGORIES: Dict[str, List[str]] = {
    "read_only": ["Read", "Grep", "Glob"],
    "file_ops": ["Read", "Write", "Edit", "Glob", "Grep"],
    "execution": ["Bash", "KillShell"],
    "web": ["WebFetch", "WebSearch"],
    "management": ["TodoWrite", "Task"],
    "notebook": ["NotebookEdit"],
    "browser": [
        "browser_navigate", "browser_screenshot", "browser_click",
        "browser_type", "browser_scroll", "browser_get_content",
        "browser_get_elements", "browser_back", "browser_forward",
        "browser_evaluate", "browser_fill", "browser_select",
        "browser_wait", "browser_tab_new", "browser_tab_list",
        "browser_tab_switch", "browser_close",
    ],
    "all": None,  # Special: inherit all tools
}

# ============================================================================
# Domain to Tools Mapping
# ============================================================================

DOMAIN_TOOLS: Dict[str, Optional[List[str]]] = {
    # Analysis-only domains (read-only)
    "code-review": ["Read", "Grep", "Glob"],
    "exploration": ["Read", "Grep", "Glob"],
    "planning": ["Read", "Grep", "Glob"],
    "analysis": ["Read", "Grep", "Glob"],

    # Documentation and writing
    "documentation": ["Read", "Write", "Edit", "Glob", "Grep"],

    # Code modification domains
    "code-writing": ["Read", "Write", "Edit", "Bash", "Grep", "Glob"],
    "testing": ["Read", "Write", "Edit", "Bash", "Grep", "Glob"],
    "refactoring": ["Read", "Write", "Edit", "Bash", "Grep", "Glob"],
    "debugging": ["Read", "Write", "Edit", "Bash", "Grep", "Glob"],

    # Research and information gathering
    "research": ["Read", "Grep", "Glob", "WebFetch", "WebSearch"],
    "security": ["Read", "Grep", "Glob", "WebSearch"],

    # PR and Git workflows
    "pr-review": ["Read", "Grep", "Glob", "Bash"],
    "git-ops": ["Read", "Grep", "Glob", "Bash"],

    # Creative domains (may need AI tools)
    "creative": ["Read", "Write", "Edit", "Glob"],
    "design": ["Read", "Write", "Edit", "Glob"],

    # Data and notebooks
    "data-science": ["Read", "Write", "Edit", "Bash", "Grep", "Glob", "NotebookEdit"],

    # Web browsing
    "web-browsing": [
        "Read", "Grep", "Glob", "WebFetch", "WebSearch",
        "browser_navigate", "browser_screenshot", "browser_click",
        "browser_type", "browser_scroll", "browser_get_content",
        "browser_get_elements", "browser_back", "browser_forward",
        "browser_evaluate", "browser_fill", "browser_select",
        "browser_wait", "browser_tab_new", "browser_tab_list",
        "browser_tab_switch", "browser_close",
    ],

    # General - inherits all
    "general": None,
}

# ============================================================================
# Domain to AI Tools Mapping
# ============================================================================

AI_TOOLS_BY_DOMAIN: Dict[str, List[str]] = {
    "creative": ["image_generation", "video_generation"],
    "design": ["image_generation", "image_editing", "image_reference"],
    "3d-modeling": ["text_to_3d", "image_to_3d", "retexture_3d"],
    "video-production": ["video_generation", "video_with_audio", "image_to_video", "video_extend"],
    "research": [],  # Might use ai_collaborate
    "web-browsing": ["web_browsing"],
    "general": [],
}

# ============================================================================
# Domain Detection Keywords
# ============================================================================

DOMAIN_KEYWORDS: Dict[str, List[str]] = {
    "code-review": [
        "review", "reviewer", "check", "audit", "inspect", "analyze code",
        "code quality", "lint", "standards", "best practices"
    ],
    "security": [
        "security", "vulnerability", "exploit", "attack", "cve", "owasp",
        "penetration", "audit security", "secure", "injection", "xss", "csrf"
    ],
    "testing": [
        "test", "testing", "unit test", "integration test", "e2e", "coverage",
        "pytest", "jest", "vitest", "spec", "assertion"
    ],
    "documentation": [
        "document", "documentation", "docs", "readme", "api doc", "jsdoc",
        "docstring", "comment", "explain", "description"
    ],
    "refactoring": [
        "refactor", "restructure", "reorganize", "clean up", "improve structure",
        "simplify", "extract", "rename", "modularize"
    ],
    "exploration": [
        "explore", "find", "search", "locate", "discover", "understand",
        "codebase", "navigate", "overview"
    ],
    "planning": [
        "plan", "architect", "design system", "strategy", "approach",
        "implementation plan", "roadmap", "structure"
    ],
    "research": [
        "research", "investigate", "learn about", "study", "compare",
        "evaluate options", "find information"
    ],
    "debugging": [
        "debug", "fix bug", "troubleshoot", "error", "issue", "problem",
        "diagnose", "trace", "investigate bug"
    ],
    "pr-review": [
        "pr review", "pull request", "merge request", "code change",
        "diff review", "commit review"
    ],
    "creative": [
        "creative", "generate image", "create visual", "artwork", "design",
        "illustration", "graphic"
    ],
    "data-science": [
        "data", "analysis", "notebook", "jupyter", "pandas", "visualization",
        "statistics", "machine learning", "ml"
    ],
    "web-browsing": [
        "browse", "browser", "website", "web page", "open url", "fill form",
        "web browser", "click button", "scrape", "web scraping", "visit site",
        "interact with page", "web automation"
    ],
    "general": [
        "general", "assistant", "helper", "agent", "tool"
    ],
}

# ============================================================================
# Model Recommendations by Domain
# ============================================================================

MODEL_BY_DOMAIN: Dict[str, str] = {
    "code-review": "sonnet",
    "security": "opus",  # Security needs careful analysis
    "testing": "sonnet",
    "documentation": "sonnet",
    "refactoring": "opus",  # Complex restructuring
    "exploration": "haiku",  # Fast searches
    "planning": "opus",  # Complex reasoning
    "research": "sonnet",
    "debugging": "sonnet",
    "pr-review": "sonnet",
    "creative": "sonnet",
    "data-science": "sonnet",
    "web-browsing": "sonnet",
    "general": "sonnet",
}

# ============================================================================
# Thinking Mode by Domain
# ============================================================================

THINKING_MODE_BY_DOMAIN: Dict[str, str] = {
    "code-review": "think",
    "security": "think_harder",
    "testing": "think",
    "documentation": "default",
    "refactoring": "think_harder",
    "exploration": "default",
    "planning": "ultra_think",
    "research": "think",
    "debugging": "think",
    "pr-review": "think",
    "creative": "default",
    "data-science": "think",
    "web-browsing": "think",
    "general": "default",
}


# ============================================================================
# Helper Functions
# ============================================================================

def detect_domain(description: str) -> str:
    """
    Detect the most likely domain from a description.

    Args:
        description: User's description of the agent/subagent

    Returns:
        Domain identifier (e.g., "code-review", "security")
    """
    description_lower = description.lower()

    # Score each domain by keyword matches
    scores: Dict[str, int] = {}

    for domain, keywords in DOMAIN_KEYWORDS.items():
        score = 0
        for keyword in keywords:
            # Use word boundary matching for more accuracy
            pattern = r'\b' + re.escape(keyword) + r'\b'
            matches = len(re.findall(pattern, description_lower))
            score += matches * len(keyword.split())  # Weight multi-word keywords higher
        scores[domain] = score

    # Return domain with highest score, default to "general"
    if not scores or max(scores.values()) == 0:
        return "general"

    return max(scores, key=lambda k: scores[k])


def get_tools_for_domain(domain: str) -> Optional[List[str]]:
    """
    Get recommended tools for a domain.

    Args:
        domain: Domain identifier

    Returns:
        List of tool names, or None for "inherit all"
    """
    return DOMAIN_TOOLS.get(domain, DOMAIN_TOOLS.get("general"))


def get_ai_tools_for_domain(domain: str) -> List[str]:
    """
    Get recommended AI tools for a domain.

    Args:
        domain: Domain identifier

    Returns:
        List of AI tool IDs to enable
    """
    return AI_TOOLS_BY_DOMAIN.get(domain, [])


def get_model_for_domain(domain: str) -> str:
    """
    Get recommended model for a domain.

    Args:
        domain: Domain identifier

    Returns:
        Model name (haiku, sonnet, opus)
    """
    return MODEL_BY_DOMAIN.get(domain, "sonnet")


def get_thinking_mode_for_domain(domain: str) -> str:
    """
    Get recommended thinking mode for a domain.

    Args:
        domain: Domain identifier

    Returns:
        Thinking mode (default, think, think_harder, ultra_think)
    """
    return THINKING_MODE_BY_DOMAIN.get(domain, "default")


def get_all_available_tools() -> List[str]:
    """Get list of all available tool names."""
    all_tools: Set[str] = set()
    for tools in TOOL_CATEGORIES.values():
        if tools:
            all_tools.update(tools)
    return sorted(list(all_tools))
