"""Conservative prompt-injection screening for prompt-bound context files.

Instruction files (AGENTS.md, the identity file) enter the system prompt
verbatim, where the model has no chance to treat them as data — so unlike tool
results, which the base prompt already tells the model to distrust, a poisoned
context file has to be caught *before* injection. This screen is deliberately
narrow: it matches only text whose primary reading is an attempt to hijack the
prompt (override directives, role-tag smuggling, concealment orders), never
ordinary prose about security. A match blocks the whole file with a placeholder
naming the finding — recoverable by the user editing the file — and logs a
warning. False negatives are acceptable; false positives on real project docs
are not, so keep every pattern here strict enough to defend in a diff review.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# (finding label, pattern). Case-insensitive. Each pattern must require both an
# imperative and a prompt-machinery object — "ignore the linter" or a doc that
# merely mentions "system prompt" must not match.
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "override-directive",
        re.compile(
            r"\b(ignore|disregard|forget|override)\b[^.\n]{0,40}\b(previous|prior|"
            r"earlier|above|all|any|your)\b[^.\n]{0,40}\b(instructions?|directives?|"
            r"system prompt|rules)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "role-tag",
        re.compile(
            r"<\s*/?\s*(system|assistant)\s*>|<\|im_(start|end)\|>|\[/?(INST|SYS)\]",
            re.IGNORECASE,
        ),
    ),
    (
        "concealment-order",
        re.compile(
            r"\b(do not|don't|never)\b[^.\n]{0,40}\b(tell|reveal|show|mention)\b"
            r"[^.\n]{0,40}\b(the |your )?(user|human|operator)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "new-identity-order",
        re.compile(
            r"\byou are no longer\b[^.\n]{0,60}\b(assistant|agent|ai|model)\b",
            re.IGNORECASE,
        ),
    ),
)


def screen_findings(content: str) -> list[str]:
    """Finding labels for suspected injection patterns in ``content``."""
    return [label for label, pattern in _PATTERNS if pattern.search(content)]


def screen(content: str, label: str) -> str:
    """Return ``content``, or a blocking placeholder when it fails the screen.

    ``label`` names the source (a path, usually) in the placeholder and the
    log line. A leading UTF-8 BOM is an editor artifact, not a finding.
    """
    if content.startswith("﻿"):
        content = content[1:]
    findings = screen_findings(content)
    if not findings:
        return content
    logger.warning("blocked %s from the prompt: %s", label, ", ".join(findings))
    return (
        f"[blocked: {label} matched suspected prompt-injection pattern(s): "
        f"{', '.join(findings)}. The file was not loaded; edit it to clear the "
        f"match.]"
    )
