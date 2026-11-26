"""Whether a fact Remember is asked to save reads as task progress.

Memory v3 §5.1: task state ("continuing the scroll fix in PR #1824") belongs
in the working record or session notes, not in durable memory, where it goes
stale the day the work lands. The signal is deliberately narrow so a lasting
rule that merely cites a pull request still saves: progress wording must be
ABOUT a PR, issue or branch reference, not merely near one. Deterministic; no
model.

"About" means one of:

* the reference is the subject of a status, joined by a be-verb, "remains",
  "still", or a colon or dash ("Issue #88 is in progress", "PR #12 still
  failing", "PR #1824: next step is ..."); nearness alone is not enough;
* a continuation names it ("Continuing PR #1824", "continuing the scroll fix
  in PR #1824", "left off on feat/x", "WIP: PR 77"), or a status is "on/for"
  it ("still failing on PR #12");
* the fact is anchored to the work: its when-line names a reference ("When
  working on PR #1824"), or a clause opens with "on branch <name>". Then any
  progress wording in the fact counts.

A generic condition in a rule ("never merge while a migration is in progress;
see issue #88") is none of these, so it saves.
"""
from __future__ import annotations

import re

# A pull request, issue or branch the work is happening on.
_REF = (
    r"(?:(?<![\w&#/])#\d{1,6}\b"                             # #1824 (not &#123; or C#1)
    r"|\b(?:PR|MR|pull\s+request|issue)[\s-]*#?\d{1,6}\b"    # PR 1824, issue #12
    # An issue or PR link, with or without its scheme; then a GitLab one.
    r"|(?:\bhttps?://)?(?:www\.)?\bgithub\.com/[\w.-]+/[\w.-]+/(?:pull|issues)/\d+"
    r"|(?:\bhttps?://[\w.-]+(?:/[\w.-]+)*)?/-/(?:merge_requests|issues)/\d+"
    # A branch with a conventional work prefix. Not docs/, test/, ci/ or
    # build/ on their own: those are as often folders as branches.
    r"|(?<![\w/.-])(?:feat|feature|fix|bugfix|hotfix|chore|refactor|wip)/[A-Za-z0-9][\w.-]*"
    # Any branch named as one: "branch docs/update-readme", "branch fix-login",
    # a quoted "branch `main`", and "on branch main" with any single name.
    r"|\bbranch\s+[`'\"]?[A-Za-z0-9][\w.-]*[/_-][\w./-]*"
    r"|\bbranch\s+[`'\"][A-Za-z0-9][\w./-]*"
    r"|\bon\s+branch\s+[A-Za-z0-9][\w./-]*)"
)
_REFERENCE = re.compile(_REF, re.IGNORECASE)

# Where a piece of work stands.
_STATUS = (
    r"(?:\bin[\s-]progress\b"
    r"|\b(?-i:WIP)\b"
    r"|\bnext\s+steps?\b"
    r"|\bstill\s+(?:needed|needs|need\s+to|to\s+do|missing|open|pending|failing|unfinished)\b"
    r"|\bremaining\s+(?:work|steps|tasks|items|fixes)\b"
    r"|\bhalf[\s-]done\b"
    r"|\bnot\s+(?:yet\s+)?(?:done|finished|merged|pushed)(?:\s+yet)?\b"
    r"|\bwaiting\s+(?:on|for)\s+(?:CI|review|the\s+review|checks)\b)"
)
# Picking a piece of work back up.
_CONTINUE = (r"(?:\bcontinu(?:e|es|ing)|\bresum(?:e|es|ing)|\bpick(?:s|ing|ed)?\s+up"
             r"|\bleft\s+off)")
# Tolerated between a phrase and the reference it is about, within a clause.
_OPEN = r"[\s(\[`'\"]+"

# What makes the reference the subject of the status that follows it: a
# be-verb, "remains", or "still" opening the status. Proximity alone is not.
_SUBJECT = (r"(?:\s+(?:is|are|was|were|remains?)\s+(?:(?:now|currently|still)\s+)?"
            r"|\s+(?=still\b))")

_ABOUT = [re.compile(p, re.IGNORECASE) for p in (
    # "Issue #88 is in progress", "pull/301 is not yet merged", "PR 12 still failing".
    _REF + _SUBJECT + r"(?P<p>" + _STATUS + ")",
    # "still failing on PR #12", "remaining work for #301".
    r"(?P<p>" + _STATUS + r")\s+(?:on|for|in|of|with)\s+(?:the\s+)?" + _REF,
    # "Continuing PR #1824", "left off on feat/x", "resuming with #12".
    r"(?P<p>" + _CONTINUE + r")(?:\s+(?:on|with|from|in|at))?" + _OPEN + _REF,
    # "Continuing the scroll fix in PR #1824", "left off after the store in #12".
    r"(?P<p>" + _CONTINUE + r"\s+(?:the|this|my|our|work|on|after|at)\b)"
    r"(?:\s+[^\s,]+){0,8}?" + _OPEN + _REF,
)]
# These read across the colon or dash the clause split cuts at, so they see
# whole fields: "WIP: PR 77", "WIP on feat/x", and a reference opening a
# clause as the subject of a status after a colon or dash ("PR #1824: next
# step is the scroll test").
_ACROSS = [re.compile(p, re.IGNORECASE) for p in (
    r"(?P<p>(?-i:WIP))\s*:?\s+(?:on\s+)?" + _REF,
    r"(?:^|[;!?]|[.:](?=\s))\s*" + _REF + r"\s*(?::|\s[–—-])\s*(?P<p>" + _STATUS + ")",
)]

# The fact itself is about the work: "On branch docs/x; next step is ...".
# The lookahead keeps "on branch <name>" (part of the reference) and
# "on <reference>" both anchored to a clause that opens with "on".
_ON_WORK = re.compile(
    r"^(?:(?:I'm|I\s+am|we're|we\s+are|currently|now|still)\s+)?(?=on\b)(?:on\s+)?" + _REF,
    re.IGNORECASE)

# Any progress wording, counted once the fact is anchored to a reference.
_PROGRESS = re.compile(
    r"\bcontinu(?:e|es|ing)\s+(?:the|this|my|our|with|work)\b"
    r"|\bresum(?:e|es|ing)\s+(?:the|this|my|our|work)\b"
    r"|\bpick(?:ing|ed)?\s+up\s+where\b"
    r"|\bleft\s+off\b"
    r"|\bwork\s+in\s+progress\b"
    r"|" + _STATUS,
    re.IGNORECASE,
)

# A clause ends at ; ! ? or at . or : followed by a space (not inside a URL).
_CLAUSE = re.compile(r"[;!?]|[.:](?=\s|$)|\s[–—-]\s")


def _clauses(text: str) -> list[str]:
    return [part.strip() for part in _CLAUSE.split(text) if part.strip()]


def task_progress(text: object, when: object = None) -> str | None:
    """The progress phrase if ``text``/``when`` read as task progress, else None."""
    fields = [part for part in (text, when) if isinstance(part, str) and part]
    if not fields or not any(_REFERENCE.search(part) for part in fields):
        return None
    for part in fields:
        match = next((m for p in _ACROSS if (m := p.search(part))), None) or next(
            (m for clause in _clauses(part) for p in _ABOUT if (m := p.search(clause))), None)
        if match:
            return match.group("p")
    anchored = (isinstance(when, str) and _REFERENCE.search(when)) or any(
        _ON_WORK.search(clause) for part in fields for clause in _clauses(part))
    if anchored:
        match = _PROGRESS.search(" ".join(fields))
        if match:
            return match.group(0)
    return None
