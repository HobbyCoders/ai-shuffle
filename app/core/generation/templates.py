"""
Domain Templates for AI Generation

Pre-configured templates for common agent archetypes that provide
comprehensive default configurations.
"""

from typing import Dict, Any, List, Optional



# ============================================================================
# Domain Templates
# ============================================================================

DOMAIN_TEMPLATES: Dict[str, Dict[str, Any]] = {
    "code-reviewer": {
        "id": "code-reviewer",
        "name_pattern": "{focus} Code Reviewer",
        "description_pattern": "Use this agent to review code for {focus_areas}. Invoke when reviewing PRs, auditing existing code, or ensuring code quality.",
        "domain": "code-review",
        "tools": ["Read", "Grep", "Glob"],
        "model": "sonnet",
        "thinking_mode": "think",
        "ai_tools": [],
        "prompt_sections": [
            "core_identity",
            "review_methodology",
            "output_format",
            "tool_guidelines",
            "anti_patterns",
        ],
        "focus_options": {
            "general": {
                "name": "Code Reviewer",
                "focus_areas": "code quality, maintainability, and best practices",
                "specific_checks": [
                    "Code readability and documentation",
                    "Function/method complexity",
                    "Error handling patterns",
                    "Naming conventions",
                    "DRY principle violations",
                ],
            },
            "security": {
                "name": "Security Code Reviewer",
                "focus_areas": "security vulnerabilities, unsafe patterns, and potential attack vectors",
                "specific_checks": [
                    "OWASP Top 10 vulnerabilities",
                    "Authentication/authorization flaws",
                    "Input validation and sanitization",
                    "Cryptographic implementation",
                    "Secret/credential exposure",
                ],
                "model": "opus",  # Security needs extra scrutiny
                "thinking_mode": "think_harder",
            },
            "performance": {
                "name": "Performance Code Reviewer",
                "focus_areas": "performance issues, inefficiencies, and optimization opportunities",
                "specific_checks": [
                    "Algorithm complexity (Big O)",
                    "Memory allocation patterns",
                    "Database query efficiency",
                    "Caching opportunities",
                    "Resource cleanup",
                ],
            },
        },
    },

    "test-writer": {
        "id": "test-writer",
        "name_pattern": "{lang} Test Writer",
        "description_pattern": "Use this agent to write {test_type} tests for {lang} code. Invoke when adding test coverage, implementing TDD, or verifying functionality.",
        "domain": "testing",
        "tools": ["Read", "Write", "Edit", "Bash", "Grep", "Glob"],
        "model": "sonnet",
        "thinking_mode": "think",
        "ai_tools": [],
        "prompt_sections": [
            "core_identity",
            "test_methodology",
            "test_structure",
            "coverage_guidelines",
            "output_format",
            "tool_guidelines",
        ],
        "lang_options": {
            "python": {
                "framework": "pytest",
                "conventions": "PEP 8, docstrings, type hints",
            },
            "typescript": {
                "framework": "vitest or jest",
                "conventions": "TypeScript strict mode, JSDoc",
            },
            "javascript": {
                "framework": "jest or vitest",
                "conventions": "ESLint rules, JSDoc",
            },
            "general": {
                "framework": "appropriate test framework",
                "conventions": "language-specific best practices",
            },
        },
    },

    "security-auditor": {
        "id": "security-auditor",
        "name_pattern": "Security Auditor",
        "description_pattern": "Use this agent for comprehensive security audits. Invoke for penetration testing preparation, vulnerability assessment, or security compliance reviews.",
        "domain": "security",
        "tools": ["Read", "Grep", "Glob", "WebSearch"],
        "model": "opus",
        "thinking_mode": "think_harder",
        "ai_tools": [],
        "prompt_sections": [
            "core_identity",
            "vulnerability_taxonomy",
            "audit_methodology",
            "severity_classification",
            "output_format",
            "tool_guidelines",
            "safety_constraints",
        ],
    },

    "documentation-writer": {
        "id": "documentation-writer",
        "name_pattern": "{doc_type} Documentation Writer",
        "description_pattern": "Use this agent to write {doc_type} documentation. Invoke when creating READMEs, API docs, architecture docs, or inline documentation.",
        "domain": "documentation",
        "tools": ["Read", "Write", "Edit", "Glob", "Grep"],
        "model": "sonnet",
        "thinking_mode": "default",
        "ai_tools": [],
        "prompt_sections": [
            "core_identity",
            "documentation_standards",
            "structure_guidelines",
            "output_format",
            "tool_guidelines",
        ],
        "doc_options": {
            "api": {
                "focus": "API reference documentation with examples",
                "format": "OpenAPI/Swagger style or framework-specific",
            },
            "readme": {
                "focus": "Project READMEs with setup and usage instructions",
                "format": "Markdown with sections for install, usage, configuration",
            },
            "architecture": {
                "focus": "System architecture and design decisions",
                "format": "Diagrams, component descriptions, data flow",
            },
            "general": {
                "focus": "Clear, comprehensive documentation",
                "format": "Context-appropriate format",
            },
        },
    },

    "pr-analyzer": {
        "id": "pr-analyzer",
        "name_pattern": "Pull Request Analyzer",
        "description_pattern": "Use this agent to analyze pull requests for quality, impact, and potential issues. Invoke before merging PRs or when reviewing complex changes.",
        "domain": "pr-review",
        "tools": ["Read", "Grep", "Glob", "Bash"],
        "model": "sonnet",
        "thinking_mode": "think",
        "ai_tools": [],
        "prompt_sections": [
            "core_identity",
            "pr_analysis_workflow",
            "review_criteria",
            "output_format",
            "tool_guidelines",
        ],
    },

    "research-agent": {
        "id": "research-agent",
        "name_pattern": "{topic} Research Agent",
        "description_pattern": "Use this agent to research {topic}. Invoke when exploring new technologies, comparing solutions, or gathering technical information.",
        "domain": "research",
        "tools": ["Read", "Grep", "Glob", "WebFetch", "WebSearch"],
        "model": "sonnet",
        "thinking_mode": "think",
        "ai_tools": [],
        "prompt_sections": [
            "core_identity",
            "research_methodology",
            "source_evaluation",
            "output_format",
            "tool_guidelines",
        ],
    },

    "refactor-agent": {
        "id": "refactor-agent",
        "name_pattern": "{scope} Refactoring Agent",
        "description_pattern": "Use this agent for code refactoring tasks. Invoke when restructuring code, extracting modules, or improving code organization.",
        "domain": "refactoring",
        "tools": ["Read", "Write", "Edit", "Grep", "Glob", "Bash"],
        "model": "opus",
        "thinking_mode": "think_harder",
        "ai_tools": [],
        "prompt_sections": [
            "core_identity",
            "refactoring_principles",
            "safety_guidelines",
            "output_format",
            "tool_guidelines",
            "anti_patterns",
        ],
    },

    "explorer": {
        "id": "explorer",
        "name_pattern": "Codebase Explorer",
        "description_pattern": "Use this agent to quickly explore and understand codebases. Invoke when you need to find files, understand structure, or locate specific code.",
        "domain": "exploration",
        "tools": ["Read", "Grep", "Glob"],
        "model": "haiku",  # Fast for exploration
        "thinking_mode": "default",
        "ai_tools": [],
        "prompt_sections": [
            "core_identity",
            "exploration_strategy",
            "output_format",
            "tool_guidelines",
        ],
    },

    "planner": {
        "id": "planner",
        "name_pattern": "{scope} Implementation Planner",
        "description_pattern": "Use this agent to plan implementations and design solutions. Invoke before starting complex features, refactors, or architectural changes.",
        "domain": "planning",
        "tools": ["Read", "Grep", "Glob"],
        "model": "opus",
        "thinking_mode": "ultra_think",
        "ai_tools": [],
        "prompt_sections": [
            "core_identity",
            "planning_methodology",
            "considerations",
            "output_format",
            "tool_guidelines",
        ],
    },

    "debugger": {
        "id": "debugger",
        "name_pattern": "{lang} Debugger",
        "description_pattern": "Use this agent to debug issues and trace problems. Invoke when encountering errors, unexpected behavior, or performance issues.",
        "domain": "debugging",
        "tools": ["Read", "Write", "Edit", "Bash", "Grep", "Glob"],
        "model": "sonnet",
        "thinking_mode": "think",
        "ai_tools": [],
        "prompt_sections": [
            "core_identity",
            "debugging_methodology",
            "diagnosis_workflow",
            "output_format",
            "tool_guidelines",
        ],
    },

    "creative-assistant": {
        "id": "creative-assistant",
        "name_pattern": "Creative Assistant",
        "description_pattern": "Use this agent for creative tasks involving images, design, or visual content. Invoke when generating images, creating visuals, or design work.",
        "domain": "creative",
        "tools": ["Read", "Write", "Edit", "Glob"],
        "model": "sonnet",
        "thinking_mode": "default",
        "ai_tools": ["image_generation", "image_editing", "image_reference"],
        "prompt_sections": [
            "core_identity",
            "creative_guidelines",
            "ai_tools_usage",
            "output_format",
        ],
    },
}


# ============================================================================
# Helper Functions
# ============================================================================

def get_template(template_id: str) -> Optional[Dict[str, Any]]:
    """
    Get a template by ID.

    Args:
        template_id: Template identifier (e.g., "code-reviewer")

    Returns:
        Template dictionary or None if not found
    """
    return DOMAIN_TEMPLATES.get(template_id)


def get_template_by_keywords(description: str) -> Optional[Dict[str, Any]]:
    """
    Find the best matching template for a description.

    Args:
        description: User's description of the desired agent

    Returns:
        Best matching template or None
    """
    description_lower = description.lower()

    # Direct template matching keywords
    template_keywords = {
        "code-reviewer": ["review", "reviewer", "code review", "audit code"],
        "test-writer": ["test", "testing", "write test", "unit test", "coverage"],
        "security-auditor": ["security audit", "vulnerability scan", "penetration", "security assessment"],
        "documentation-writer": ["document", "documentation", "docs", "readme", "api doc"],
        "pr-analyzer": ["pr review", "pull request", "merge request", "pr analysis"],
        "research-agent": ["research", "investigate", "explore topic", "study"],
        "refactor-agent": ["refactor", "restructure", "reorganize", "clean up code"],
        "explorer": ["explore codebase", "find code", "search codebase", "understand code"],
        "planner": ["plan", "architect", "design", "implementation plan"],
        "debugger": ["debug", "troubleshoot", "fix bug", "diagnose"],
        "creative-assistant": ["creative", "image", "visual", "design", "artwork"],
    }

    best_match = None
    best_score = 0

    for template_id, keywords in template_keywords.items():
        score = 0
        for keyword in keywords:
            if keyword in description_lower:
                # Weight by keyword length (longer = more specific)
                score += len(keyword.split())

        if score > best_score:
            best_score = score
            best_match = template_id

    if best_match and best_score > 0:
        return DOMAIN_TEMPLATES.get(best_match)

    return None


def get_all_template_ids() -> List[str]:
    """Get list of all template IDs."""
    return list(DOMAIN_TEMPLATES.keys())


def apply_template_overrides(
    template: Dict[str, Any],
    focus: Optional[str] = None,
    lang: Optional[str] = None,
    doc_type: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Apply focus/language/type overrides to a template.

    Args:
        template: Base template dictionary
        focus: Focus area (for code-reviewer)
        lang: Language (for test-writer, debugger)
        doc_type: Documentation type (for documentation-writer)

    Returns:
        Template with overrides applied
    """
    result = template.copy()

    # Apply focus options (code-reviewer)
    if "focus_options" in template and focus:
        focus_config = template["focus_options"].get(focus, template["focus_options"].get("general", {}))
        result.update({k: v for k, v in focus_config.items() if k not in ["name", "focus_areas", "specific_checks"]})

    # Apply language options (test-writer)
    if "lang_options" in template and lang:
        lang_config = template["lang_options"].get(lang, template["lang_options"].get("general", {}))
        result["lang_config"] = lang_config

    # Apply doc type options (documentation-writer)
    if "doc_options" in template and doc_type:
        doc_config = template["doc_options"].get(doc_type, template["doc_options"].get("general", {}))
        result["doc_config"] = doc_config

    return result
