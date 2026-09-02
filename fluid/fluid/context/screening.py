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
        # fluid's own control channels: a genuine system-reminder, steering
        # or peer-message marker is emitted by the harness, never written in a
        # context file —
        # one appearing in an AGENTS.md/identity/memory entry is spoofing.
        "harness-marker",
        re.compile(
            r"<\s*/?\s*system-reminder\s*>|\[/?user interjection"
            r"|\[/?agent message",
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


# Invisible / direction-control characters used to hide instructions from the
# human reviewing a file while the model still reads them: zero-width spaces
# and joiners, bidi embedding/override/isolate controls, interior BOMs.
_INVISIBLE_RE = re.compile(
    "[\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]"
)

# How many stripped invisible characters turn from "editor noise" into a
# finding. A stray joiner from a copy-paste is normal; dozens are a payload.
_INVISIBLE_FINDING_THRESHOLD = 8


def strip_invisible(content: str) -> tuple[str, int]:
    """Remove invisible/bidi-control characters; returns (clean, count)."""
    clean, count = _INVISIBLE_RE.subn("", content)
    return clean, count


# Credential shapes that must never persist into prompt-bound stores. Explicit
# vendor prefixes and key blocks only — a generic entropy heuristic would
# false-positive on ordinary prose, and a blocked memory write is friction the
# model retries.
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private-key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Anthropic API key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{10,}")),
    ("OpenAI-style API key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("GitHub token", re.compile(r"\bgh[oprsu]_[A-Za-z0-9]{20,}\b")),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b")),
    (
        "credential assignment",
        re.compile(
            r"\b(api[_-]?key|access[_-]?token|client[_-]?secret|password)\b"
            r"\s*[=:]\s*[\"']?[A-Za-z0-9+/_-]{16,}",
            re.IGNORECASE,
        ),
    ),
)


def looks_like_secret(content: str) -> str | None:
    """The kind of credential ``content`` appears to contain, or ``None``."""
    for kind, pattern in _SECRET_PATTERNS:
        if pattern.search(content):
            return kind
    return None


def screen_findings(content: str) -> list[str]:
    """Finding labels for suspected injection patterns in ``content``."""
    findings = [label for label, pattern in _PATTERNS if pattern.search(content)]
    _, invisible = strip_invisible(content)
    if invisible >= _INVISIBLE_FINDING_THRESHOLD:
        findings.append(f"invisible-text ({invisible} hidden chars)")
    return findings


def screen(content: str, label: str) -> str:
    """Return ``content``, or a blocking placeholder when it fails the screen.

    ``label`` names the source (a path, usually) in the placeholder and the
    log line. A leading UTF-8 BOM is an editor artifact, not a finding.
    """
    if content.startswith("\ufeff"):
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
