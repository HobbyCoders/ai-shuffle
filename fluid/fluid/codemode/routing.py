"""Shared positive-clause screen for model authority and memory questions."""
from __future__ import annotations
import re

# Sentence breaks: ; newline and dashes anywhere; . ! ? only before a space or
# the end, so "GPT 6.1 Sol" stays whole. Within a sentence, , and : (again only
# before a space, so "prov:lane:astra" stays whole) separate list parts.
_SENTENCE_BREAK = re.compile(r"[;\n—–]|[.!?](?=\s|$)")
_PART_BREAK = re.compile(r"[,:](?=\s|$)")
# A negation rejects its suffix; only an instruction may retain its positive
# prefix ("use Opus not Sol"), never a narrative ("Opus not working").
_NEGATION = re.compile(
    r"\b(?:never|not|no|dont|don't|do\s+not|avoid|stop\s+using|\w+n't)\b"
    r"|\b(?:is|are|was|were)\s+(?:\w+\s+)?(?:poor|bad|crap)\b", re.I)
# A negation carries into the following parts of its sentence ("NEVER use
# Fable, Mythos or Grok"; "Models to avoid: Fable, Mythos") until a part names
# a fresh instruction to use something: a part that starts with an imperative
# ("use Opus ..."). Only that ends it, not "anything using Grok", so the rule errs
# toward caution: an ambiguous start falls back to the owner's prompt.
_AFFIRM = re.compile(
    r"\s*(?:(?:and|or|but|then|now|please|we)\s+)*"
    r"(?:(?:can|could)\s+(?:you|we)\s+)?"
    r"(?:use|run|route|send|prefer|pick|choose|try|start|switch|default"
    r"|go\s+(?:with|to)|move\s+to)\b", re.I)
# "Use Opus instead of Sol": the words after the cue are the rejected ones.
_CONTRAST = re.compile(r"\b(?:instead\s+of|rather\s+than)\b", re.I)


def is_instruction(text: str) -> bool:
    """A directive starts its own clause, not a narrative mentioning a verb."""
    return bool(_AFFIRM.match(text))


def affirmed_clauses(text: str) -> list[str]:
    """The parts of ``text`` that may name a model to use.

    Sentences split on ``.``/``!``/``?``/``;``/dashes/newlines, then into parts
    on ``,``/``:``. A negation (never, not, no, don't, do not, avoid, stop
    using, "X is poor|bad|crap") drops its suffix and later list parts until a
    fresh instruction. Its prefix survives only if that part starts with an
    instruction. "instead of"/"rather than" likewise drops the rejected suffix.
    """
    clauses = []
    for sentence in _SENTENCE_BREAK.split(str(text or "").replace("’", "'")):
        negated = False
        for part in _PART_BREAK.split(sentence):
            if negated and is_instruction(part) and not _NEGATION.search(part):
                negated = False
            if negated:
                continue
            contrast = _CONTRAST.search(part)
            if contrast:
                part, negated = part[:contrast.start()], True
            negative = _NEGATION.search(part)
            if negative:
                prefix = part[:negative.start()]
                negated = True
                if not is_instruction(prefix):
                    continue
                part = prefix
            if part.strip():
                clauses.append(part)
    return clauses
