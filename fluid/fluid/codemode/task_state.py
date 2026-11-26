"""Whether a fact Remember is asked to save reads as task progress.

Memory v3 §5.1: task state ("continuing the scroll fix in PR #1824") belongs
in the working record or session notes, not in durable memory, where it goes
stale the day the work lands. The signal is deliberately narrow so a lasting
rule that merely cites a pull request still saves: a PR, issue or branch
reference AND progress wording, both required. Deterministic; no model.
"""
from __future__ import annotations

import re

# A pull request, issue or branch the work is happening on.
_REFERENCE = re.compile(
    r"(?<![\w&#/])#\d{1,6}\b"                               # #1824 (not &#123; or C#1)
    r"|\b(?:PR|MR|pull request|issue)[\s-]*#?\d{1,6}\b"       # PR 1824, issue #12
    r"|\bgithub\.com/[\w.-]+/[\w.-]+/(?:pull|issues)/\d+"     # an issue or PR link
    r"|/-/(?:merge_requests|issues)/\d+"                      # a GitLab link
    # A branch with a conventional work prefix. Not docs/, test/, ci/ or
    # build/: those are as often folders as branches.
    r"|(?<![\w/.-])(?:feat|feature|fix|bugfix|hotfix|chore|refactor|wip)/[A-Za-z0-9][\w.-]*",
    re.IGNORECASE,
)

# Wording that describes where a piece of work stands, not how things are.
_PROGRESS = re.compile(
    r"\bcontinu(?:e|es|ing)\s+(?:the|this|my|our|with|work)\b"
    r"|\bresum(?:e|es|ing)\s+(?:the|this|my|our|work)\b"
    r"|\bpick(?:ing|ed)?\s+up\s+where\b"
    r"|\bleft\s+off\b"
    r"|\bin[\s-]progress\b"
    r"|\bwork\s+in\s+progress\b"
    # WIP as a status, not the name of a label or title prefix.
    r"|(?<!titled )(?<!labeled )(?<!labelled )(?<!marked )(?<!as )"
    r"\b(?-i:WIP)\b(?!\s+(?:label|title|prefix|tag|PRs?|pull|branch|commit))"
    r"|\bnext\s+steps?\b"
    r"|\bstill\s+(?:needed|needs|need\s+to|to\s+do|missing|open|pending|failing|unfinished)\b"
    r"|\bremaining\s+(?:work|steps|tasks|items|fixes)\b"
    r"|\bhalf[\s-]done\b"
    r"|\bnot\s+(?:yet\s+)?(?:done|finished|merged|pushed)\s+yet\b"
    r"|\bnot\s+yet\s+(?:done|finished|merged|pushed)\b"
    r"|\bwaiting\s+(?:on|for)\s+(?:CI|review|the\s+review|checks)\b",
    re.IGNORECASE,
)


def task_progress(text: object, when: object = None) -> str | None:
    """The progress phrase if ``text``/``when`` read as task progress, else None.

    Both a work reference and progress wording must appear (in either field).
    """
    joined = " ".join(str(part) for part in (text, when) if isinstance(part, str) and part)
    if not joined or not _REFERENCE.search(joined):
        return None
    match = _PROGRESS.search(joined)
    return match.group(0) if match else None
