"""Conservative cleanup for generated commit and pull-request text.

The model that drafts commit messages and PR descriptions occasionally leaves a
mechanical artifact in its output: a bare ``:wq`` on its own line, a literal
``(no output)`` where a tool result got echoed back, a code fence it opened and
never closed. Once such a line reaches ``git commit -m`` or ``gh pr create`` it
is effectively permanent — a merged commit body cannot be corrected without
rewriting history, and that is exactly how one ended up in this repo.

This module strips only artifacts that cannot plausibly be prose, and leaves
everything else byte-for-byte. It is deliberately NOT a style pass: it does not
reflow, re-wrap, re-capitalize, re-punctuate, translate, or shorten anything,
and it never adds text. Where a judgment call would be required, the text goes
through untouched — a stray artifact is a far smaller cost than a mangled
message.

What it removes:

* a line that is nothing but a vim command (``:w``, ``:wq``, ``:q!``, ``:x``);
* a line that is exactly ``(no output)``;
* a single unclosed code fence at the very end of the text;
* trailing whitespace, and blank lines at the start and end;
* a non-indented line repeated identically on the line right before it.

What it deliberately leaves alone:

* anything inside a fenced code block — ``:wq`` there is likely the subject of
  the text, and identical consecutive lines are ordinary code;
* anything indented, for the same reason;
* prose that merely contains one of these strings ("the command produced no
  output", "then type :wq to save");
* balanced fences anywhere, and unbalanced ones that are not the last line,
  because guessing which fence is the stray one risks eating real content.
"""

import logging
import re
from collections import Counter
from typing import List

logger = logging.getLogger(__name__)

# A line that is nothing but a vim command. Anything else on the line — a word
# before it, an explanation after it — makes it prose about vim rather than a
# leaked keystroke, and it is left alone.
_VIM_ARTIFACT = re.compile(r"^:(?:w|q|x|wq|xa|wqa)!?$")

# The literal placeholder a captured tool result uses for an empty stream. Only
# an exact whole-line match counts; "produced no output" is prose.
_NO_OUTPUT = re.compile(r"^\(no output\)$", re.IGNORECASE)

# Opening or closing fence, in either markdown flavour.
_FENCE = re.compile(r"^\s*(?:```|~~~)")

# A fence carrying no info string — the only kind safe to drop as a stray.
_BARE_FENCE = re.compile(r"^\s*(?:```|~~~)\s*$")


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def sanitize_commit_text(text: str, *, context: str = "commit message") -> str:
    """Strip unambiguous generation artifacts from ``text``.

    ``context`` only labels the debug log line. Returns the text unchanged when
    there is nothing unambiguous to remove, and falls back to the original if
    sanitizing would empty out text that had content — an empty commit message
    fails the commit, which is worse than the artifact.
    """
    if not text or not text.strip():
        return text or ""

    original = text
    lines = _normalize_newlines(text).split("\n")
    kept: List[str] = []
    removed: List[str] = []
    in_fence = False

    for line in lines:
        trimmed = line.rstrip()
        bare = trimmed.strip()

        if _FENCE.match(trimmed):
            in_fence = not in_fence
            kept.append(trimmed)
            continue

        if in_fence:
            # Every line inside a fence is content. Only trailing whitespace
            # goes; a ":wq" here is probably what the text is explaining, and a
            # repeated line is ordinary code.
            kept.append(trimmed)
            continue

        if _VIM_ARTIFACT.match(bare):
            removed.append("vim artifact")
            continue

        if _NO_OUTPUT.match(bare):
            removed.append("(no output) line")
            continue

        # An identical repeat of the line immediately above. Indented lines are
        # skipped: a four-space markdown code block repeats lines legitimately.
        if (
            bare
            and not line[:1].isspace()
            and kept
            and kept[-1] == trimmed
        ):
            removed.append("duplicate line")
            continue

        kept.append(trimmed)

    while kept and not kept[-1].strip():
        kept.pop()

    # A fence the generator opened and never closed. Only a bare fence on the
    # very last line is safe to drop — that is an opener with nothing after it.
    fence_count = sum(1 for line in kept if _FENCE.match(line))
    if fence_count % 2 == 1 and kept and _BARE_FENCE.match(kept[-1]):
        kept.pop()
        removed.append("unbalanced trailing code fence")
        while kept and not kept[-1].strip():
            kept.pop()

    cleaned = "\n".join(kept).strip("\n")

    if not cleaned.strip():
        logger.debug(
            "git_text: sanitizing would have emptied the %s; keeping it as written",
            context,
        )
        return original

    if cleaned != original:
        counts = Counter(removed)
        summary = ", ".join(
            f"{count}x {kind}" for kind, count in sorted(counts.items())
        )
        logger.debug(
            "git_text: sanitized %s (%s)",
            context,
            summary or "trailing whitespace only",
        )

    return cleaned


def sanitize_pr_title(title: str) -> str:
    """Sanitize a PR title and flatten it to the single line ``gh`` expects.

    The title rides in argv, so an embedded newline would either be rejected or
    silently swallow the rest of the title. Interior whitespace runs collapse to
    single spaces; no other rewriting happens.
    """
    if not title or not title.strip():
        return title or ""

    cleaned = sanitize_commit_text(title, context="PR title")
    collapsed = " ".join(cleaned.split())
    return collapsed or title.strip()


def sanitize_pr_body(body: str) -> str:
    """Sanitize a PR body. Multi-line, so this is the commit-text pass."""
    return sanitize_commit_text(body, context="PR body")
