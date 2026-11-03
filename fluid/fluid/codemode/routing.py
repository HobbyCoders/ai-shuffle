"""Shared positive-clause screen for model authority and memory questions."""
from __future__ import annotations
import re

# Sentence breaks: ; newline and dashes anywhere; . ! ? only before a space or
# the end, so "GPT 6.1 Sol" stays whole. Within a sentence, , and : (again only
# before a space, so "prov:lane:astra" stays whole) separate list parts.
_SENTENCE_BREAK = re.compile(r"[;\n—–]|[.!?](?=\s|$)")
_PART_BREAK = re.compile(r"[,:](?=\s|$)")
# A part with one of these says what NOT to use: every model in it is left out.
_NEGATION = re.compile(
    r"\b(?:never|not|no|dont|don't|do\s+not|avoid|stop\s+using|\w+n't)\b"
    r"|\b(?:is|are|was|were)\s+(?:\w+\s+)?(?:poor|bad|crap)\b", re.I)
# A negation carries into the following parts of its sentence ("NEVER use
# Fable, Mythos or Grok"; "Models to avoid: Fable, Mythos") until a part names
# a fresh instruction to use something: a part that starts with an imperative
# ("use Opus ..."). Only that ends it, not "anything using Grok", so the rule errs
# toward caution: an ambiguous start falls back to the owner's prompt.
_AFFIRM = re.compile(
    r"^\s*(?:(?:and|or|but|then)\s+)?(?:use|run|route|send|prefer|pick|default"
    r"|go\s+to|switch\s+to|move\s+to)\b", re.I)
# "Use Opus instead of Sol": the words after the cue are the rejected ones.
_CONTRAST = re.compile(r"\b(?:instead\s+of|rather\s+than)\b", re.I)


def affirmed_clauses(text: str) -> list[str]:
    """The parts of ``text`` that may name a model to use.

    Sentences split on ``.``/``!``/``?``/``;``/dashes/newlines, then into parts
    on ``,``/``:``. A part with a negation (never, not, no, don't, do not,
    avoid, stop using, "X is poor|bad|crap") is dropped, and so is every later
    part of that sentence until one starts with an instruction to use something
    (use, run, route, send, prefer, pick, default, go to, switch to, move to). "instead of"/"rather than"
    drops the words after it, and the rest of the sentence's list.
    """
    clauses = []
    for sentence in _SENTENCE_BREAK.split(str(text or "").replace("’", "'")):
        negated = False
        for part in _PART_BREAK.split(sentence):
            if negated and _AFFIRM.search(part) and not _NEGATION.search(part):
                negated = False
            if negated:
                continue
            contrast = _CONTRAST.search(part)
            if contrast:
                part, negated = part[:contrast.start()], True
            if _NEGATION.search(part):
                negated = True
                continue
            if part.strip():
                clauses.append(part)
    return clauses
