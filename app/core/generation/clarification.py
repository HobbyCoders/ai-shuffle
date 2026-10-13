"""
Clarification Logic for AI Generation

Analyzes descriptions to determine if clarification questions are needed
before generating agent/subagent configurations.
"""

import re
from typing import Dict, List, Optional
from dataclasses import dataclass


@dataclass
class ClarificationQuestion:
    """A clarification question to ask the user."""
    id: str
    question: str
    options: Optional[List[str]] = None
    context: Optional[str] = None


# ============================================================================
# Clarification Triggers
# ============================================================================

def _is_too_short(description: str) -> bool:
    """Check if description is too short to generate well."""
    words = description.split()
    return len(words) < 8


def _is_vague(description: str) -> bool:
    """Check if description uses vague terms without specifics."""
    vague_patterns = [
        r'\b(helper|assistant|tool|agent)\b(?!\s+(for|that|which|to))',
        r'^(a|an|the)\s+(good|nice|useful)\s+',
        r'\bsomething\s+(that|to|for)\b',
    ]
    description_lower = description.lower()
    for pattern in vague_patterns:
        if re.search(pattern, description_lower):
            # Check if there's more context
            if len(description.split()) < 15:
                return True
    return False


def _has_conflicting_requirements(description: str) -> bool:
    """Check for potentially conflicting requirements."""
    conflicts = [
        (r'\b(fast|quick|speed)', r'\b(thorough|comprehensive|detailed)'),
        (r'\b(simple|basic)', r'\b(advanced|complex)'),
        (r'\b(all\s+tools|full\s+access)', r'\b(restrict|limit|only)'),
    ]
    description_lower = description.lower()
    for pattern1, pattern2 in conflicts:
        if re.search(pattern1, description_lower) and re.search(pattern2, description_lower):
            return True
    return False


def _mentions_security_without_detail(description: str) -> bool:
    """Check if security is mentioned but without specifics."""
    security_keywords = [
        'security', 'secure', 'vulnerability', 'auth', 'authentication',
        'authorization', 'credential', 'secret', 'permission'
    ]
    description_lower = description.lower()

    has_security = any(kw in description_lower for kw in security_keywords)
    if not has_security:
        return False

    # Check if there's enough detail
    specific_patterns = [
        r'owasp', r'cve', r'injection', r'xss', r'csrf', r'sql',
        r'penetration', r'audit', r'vulnerability scan',
    ]
    has_specifics = any(re.search(p, description_lower) for p in specific_patterns)

    return has_security and not has_specifics and len(description.split()) < 20


def _unclear_scope(description: str) -> bool:
    """Check if the scope/domain is unclear."""
    # If it's short and doesn't mention a clear domain
    if len(description.split()) > 20:
        return False

    clear_domains = [
        r'\b(code\s+review|test|testing|document|debug|refactor)',
        r'\b(python|typescript|javascript|rust|go)\b',
        r'\b(api|database|frontend|backend|security)',
        r'\b(pr|pull\s+request|git)',
    ]
    description_lower = description.lower()
    has_domain = any(re.search(p, description_lower) for p in clear_domains)

    return not has_domain


# ============================================================================
# Clarification Questions
# ============================================================================

CLARIFICATION_QUESTIONS: Dict[str, ClarificationQuestion] = {
    "too_short": ClarificationQuestion(
        id="too_short",
        question="Could you provide more detail about what this agent should do?",
        options=None,
        context="A more detailed description helps generate better prompts and configurations."
    ),

    "vague_purpose": ClarificationQuestion(
        id="vague_purpose",
        question="What specific tasks should this agent handle?",
        options=[
            "Code review and analysis",
            "Writing and running tests",
            "Documentation",
            "Debugging and troubleshooting",
            "Research and exploration",
            "General coding assistance",
        ],
        context="Knowing the primary purpose helps configure appropriate tools and prompts."
    ),

    "conflicting_requirements": ClarificationQuestion(
        id="conflicting_requirements",
        question="I noticed potentially conflicting requirements. Which should be prioritized?",
        options=[
            "Speed - prioritize fast responses",
            "Thoroughness - prioritize comprehensive analysis",
            "Balance - find a middle ground",
        ],
        context="This helps determine the model and thinking mode."
    ),

    "security_detail": ClarificationQuestion(
        id="security_detail",
        question="What type of security analysis are you looking for?",
        options=[
            "General security code review",
            "OWASP Top 10 vulnerability scanning",
            "Authentication/Authorization review",
            "Cryptographic implementation audit",
            "Full security audit",
        ],
        context="Different security focuses require different approaches and severity levels."
    ),

    "unclear_scope": ClarificationQuestion(
        id="unclear_scope",
        question="What's the primary domain or technology for this agent?",
        options=[
            "Web development (frontend/backend)",
            "Python development",
            "TypeScript/JavaScript",
            "System/DevOps",
            "Data science/ML",
            "General purpose",
        ],
        context="This helps tailor the prompts and tool selection."
    ),
}


# ============================================================================
# Analysis Functions
# ============================================================================

def analyze_description(description: str) -> Dict[str, bool]:
    """
    Analyze a description for potential issues.

    Args:
        description: User's agent/subagent description

    Returns:
        Dict of trigger_name -> triggered (bool)
    """
    return {
        "too_short": _is_too_short(description),
        "vague_purpose": _is_vague(description),
        "conflicting_requirements": _has_conflicting_requirements(description),
        "security_detail": _mentions_security_without_detail(description),
        "unclear_scope": _unclear_scope(description),
    }


def should_ask_clarification(description: str) -> bool:
    """
    Determine if clarification questions should be asked.

    Args:
        description: User's agent/subagent description

    Returns:
        True if clarification is needed
    """
    analysis = analyze_description(description)

    # Always ask if too short
    if analysis["too_short"]:
        return True

    # Ask if multiple issues found
    issue_count = sum(1 for v in analysis.values() if v)
    if issue_count >= 2:
        return True

    # Don't interrupt for minor issues if description is reasonably detailed
    return False


def get_clarification_questions(description: str) -> List[ClarificationQuestion]:
    """
    Get the clarification questions to ask based on description analysis.

    Args:
        description: User's agent/subagent description

    Returns:
        List of ClarificationQuestion objects (max 3)
    """
    analysis = analyze_description(description)
    questions = []

    # Prioritized order
    priority = [
        "too_short",
        "vague_purpose",
        "security_detail",
        "unclear_scope",
        "conflicting_requirements",
    ]

    for trigger in priority:
        if analysis.get(trigger) and trigger in CLARIFICATION_QUESTIONS:
            questions.append(CLARIFICATION_QUESTIONS[trigger])
            if len(questions) >= 3:  # Max 3 questions
                break

    return questions


def format_clarification_context(
    answers: Dict[str, str],
    original_description: str,
) -> str:
    """
    Format clarification answers as context for generation.

    Args:
        answers: Dict of question_id -> user's answer
        original_description: The original description

    Returns:
        Formatted context string
    """
    lines = [f"Original description: {original_description}", ""]
    lines.append("User clarifications:")

    for question_id, answer in answers.items():
        if question_id in CLARIFICATION_QUESTIONS:
            q = CLARIFICATION_QUESTIONS[question_id]
            lines.append(f"- Q: {q.question}")
            lines.append(f"  A: {answer}")

    return "\n".join(lines)
