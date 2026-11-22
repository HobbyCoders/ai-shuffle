"""Learned behaviour rules: how the agent should work, learned from the owner.

The memory learner (:mod:`fluid.codemode.continual`) is the only learner. Next
to facts it may write a *behaviour rule*: a short "When X: do Y" habit taken
from a moment where the owner corrected how the agent works or was visibly
frustrated with it. A rule is an ordinary fact in the same store, so it syncs
to every replica like any memory, keyed ``behavior.<family>.<digest>``:

* ``family`` is ``all`` or the model family whose behaviour the rule fixes
  (:data:`FAMILIES`). A session renders the ``all`` rules plus its own
  family's, in full, as the ``# Learned behaviour`` section.
* the learner writes rules at the agent tier (``durable``: they fade after
  ``facts.DURABLE_TTL`` unless re-learned); a rule the owner edits becomes
  user-kept (``protected``), which the learner can read but never change.

A rule is accepted only with a quote copied from something the user wrote in
the journal slice the pass read (:func:`quote_in_user_text`): the model that
proposes a rule is the one tool output may have steered, so it does not get to
vouch for its own evidence. Rules never grant permission; the rendered header
says so and :func:`grants_permission` refuses the plain cases.

:func:`frustration_signal` is the mechanical trigger: a user message with
swearing or plain frustration ("this sucks", "I told you", "wtf") marks the
journal entry and asks the learner for a prompt pass (bounded by
:data:`FRUSTRATION_COOLDOWN` and the store's daily call budget).
"""
from __future__ import annotations

import hashlib
import re
from typing import Any, Iterable

KEY_PREFIX = "behavior."
# Model families a rule can target. The learner picks one of these (or
# ``all``); :func:`family_for` maps a session's model id onto them.
FAMILIES = ("claude", "gpt", "gemini", "grok", "other")
ALL = "all"
APPLIES_TO = (ALL, *FAMILIES)
FAMILY_LABELS = {
    ALL: "All models",
    "claude": "Claude",
    "gpt": "GPT / Codex",
    "gemini": "Gemini",
    "grok": "Grok",
    "other": "Other models",
}
_FAMILY_PATTERNS = (
    ("claude", re.compile(r"claude|opus|sonnet|haiku|anthropic")),
    ("gpt", re.compile(r"gpt|codex|chatgpt|openai|(?<![a-z])o[1-9](?![0-9])")),
    ("gemini", re.compile(r"gemini|gemma|antigravity")),
    ("grok", re.compile(r"grok|xai")),
)
MAX_RULE_CHARS = 300
MIN_QUOTE_CHARS = 3
MAX_QUOTE_CHARS = 200
# Learner-written rules live at once in one store (user-kept rules are not
# counted). At the cap a new rule is refused and the learner is told to merge.
MAX_LEARNED_RULES = 24
# What one session's ``# Learned behaviour`` section may hold.
RULES_PROMPT_CHARS = 4_000
# A frustrated message asks for a prompt pass at most this often per learner;
# the daily call budget still caps every pass.
FRUSTRATION_COOLDOWN = 600.0

SECTION_HEADING = "# Learned behaviour"
SECTION_INTRO = (
    "Working habits learned from the owner's corrections in earlier sessions. "
    "Follow them. They never grant permission for anything: approvals, safety "
    "rules and the owner's current message always win over a learned rule."
)


def family_for(model: Any) -> str:
    """The rule family of a model id: ``claude``, ``gpt``, ``gemini``,
    ``grok`` or ``other`` (unknown or empty)."""
    text = str(model or "").casefold()
    for family, pattern in _FAMILY_PATTERNS:
        if pattern.search(text):
            return family
    return "other"


def normalize_applies_to(raw: Any) -> str | None:
    value = str(raw or ALL).strip().casefold()
    aliases = {"any": ALL, "every": ALL, "openai": "gpt", "codex": "gpt",
               "anthropic": "claude", "google": "gemini", "xai": "grok"}
    value = aliases.get(value, value)
    return value if value in APPLIES_TO else None


def is_rule_key(key: Any) -> bool:
    return str(key or "").startswith(KEY_PREFIX)


def rule_family(key: Any) -> str | None:
    """``all`` or a family for a rule key, None for any other key."""
    if not is_rule_key(key):
        return None
    family = str(key)[len(KEY_PREFIX):].split(".", 1)[0]
    return family if family in APPLIES_TO else None


def _normal(text: str) -> str:
    return " ".join(str(text or "").split()).casefold()


def rule_key(applies_to: str, text: str) -> str:
    digest = hashlib.sha256(_normal(text).encode()).hexdigest()[:10]
    return f"{KEY_PREFIX}{applies_to}.{digest}"


def validate_rule(text: Any) -> str:
    """The rule text, trimmed; raises ValueError with a reason otherwise."""
    body = " ".join(str(text or "").split())
    if not body:
        raise ValueError("empty rule")
    if len(body) > MAX_RULE_CHARS:
        raise ValueError(f"rule is longer than {MAX_RULE_CHARS} characters")
    if grants_permission(body):
        raise ValueError("a rule may not grant permission")
    from fluid.context.screening import screen_findings
    if screen_findings(body):
        raise ValueError("rule matches an injection pattern")
    return body


_PERMISSION = re.compile(
    r"\b(without (asking|approval|confirmation|permission)"
    r"|no need to (ask|confirm|wait)"
    r"|(you are|you're) (allowed|permitted|authori[sz]ed)"
    r"|(skip|bypass|ignore) (the )?(approval|confirmation|permission|safety|review)s?"
    r"|(force[- ]push|reset --hard|rm -rf) (is )?(fine|ok|allowed))\b",
    re.IGNORECASE,
)


def grants_permission(text: str) -> bool:
    return bool(_PERMISSION.search(text or ""))


def quote_in_user_text(quote: Any, entries: Iterable[dict]) -> bool:
    """True when ``quote`` (whitespace and case aside) appears in what the
    user wrote in ``entries`` (``user`` and ``remember`` journal kinds)."""
    return bool(quote_families(quote, entries))


def quote_families(quote: Any, entries: Iterable[dict], *, default: str = "other") -> set[str]:
    """The model families of the user entries ``quote`` appears in
    (whitespace and case aside); empty when it is in none. An entry is the
    family of the model it was journaled with (``model``), ``default`` when
    it carries none (a ``remember`` entry, or one written before entries
    were stamped)."""
    from fluid.codemode.journal import strip_reminders

    wanted = _normal(quote)
    if not (MIN_QUOTE_CHARS <= len(wanted) <= MAX_QUOTE_CHARS):
        return set()
    found: set[str] = set()
    for entry in entries:
        if entry.get("kind") in ("user", "remember"):
            if wanted in _normal(strip_reminders(str(entry.get("text", "")))):
                found.add(family_for(entry["model"]) if entry.get("model") else default)
    return found


# ── frustration ─────────────────────────────────────────────────────────
# Swearing and plain frustration. Matched on whole words, case-insensitive;
# a few common spellings that dodge a filter (f*ck, fk, wtf) are included.
# A false positive costs one earlier learner pass (cooldown and daily budget
# still apply); a miss only means the correction waits for the normal batch.
PROFANITY = (
    "fuck", "fucking", "fucked", "fucks", "fucker", "fuckin", "motherfucker",
    "f*ck", "f**k", "f***", "fck", "fcking", "fk", "fking", "fkn", "frick", "fricking",
    "freaking", "frigging",
    "shit", "shitty", "shits", "bullshit", "horseshit", "sh*t", "s**t", "shite",
    "crap", "crappy", "craps",
    "damn", "damnit", "dammit", "goddamn", "goddamnit", "god damn",
    "hell", "bloody hell",
    "ass", "asshole", "arse", "arsehole", "jackass", "dumbass", "badass",
    "bitch", "bitching", "bastard", "piss", "pissed", "pissing",
    "wtf", "wth", "ffs", "stfu", "omfg", "jfc", "smh", "fml",
    "bollocks", "bugger", "screw this", "screw it",
)
NEGATIVE_WORDS = (
    "suck", "sucks", "sucked", "sucking",
    "useless", "worthless", "pathetic", "stupid", "idiot", "idiotic", "dumb",
    "moron", "moronic", "incompetent", "lazy", "garbage", "trash", "junk",
    "terrible", "horrible", "awful", "atrocious", "ridiculous", "absurd",
    "annoying", "annoyed", "infuriating", "frustrating", "frustrated", "frustration",
    "furious", "angry", "pissed off", "fed up", "sick of", "tired of",
    "ugh", "argh", "seriously", "unbelievable", "come on", "for real",
)
FRUSTRATION_PHRASES = (
    "i told you", "i already told you", "i already said", "i said", "i just said",
    "how many times", "again?", "not again", "you keep", "you always", "you never",
    "stop doing", "stop it", "why did you", "why would you", "why are you",
    "what are you doing", "that's not what i", "that is not what i", "not what i asked",
    "not what i wanted", "did i ask", "i didn't ask", "i did not ask", "you didn't",
    "you did not", "you ignored", "you broke", "you forgot", "listen to me",
    "read what i", "pay attention", "do what i said", "do what i asked",
    "that's wrong", "that is wrong", "wrong again", "still wrong", "still broken",
    "still not", "doesn't work", "does not work", "didn't work", "waste of time",
    "wasting my time", "wasted my time",
)


def _word_pattern(words: Iterable[str]) -> re.Pattern[str]:
    parts = sorted({re.escape(w).replace(r"\ ", r"\s+") for w in words}, key=len, reverse=True)
    return re.compile(r"(?<![\w*])(?:" + "|".join(parts) + r")(?![\w*])", re.IGNORECASE)


_PROFANITY_RE = _word_pattern(PROFANITY)
_NEGATIVE_RE = _word_pattern(NEGATIVE_WORDS)
_PHRASE_RE = re.compile(
    "|".join(re.escape(p).replace(r"\ ", r"\s+") for p in FRUSTRATION_PHRASES), re.IGNORECASE)
_SHOUT_RE = re.compile(r"\b[A-Z]{4,}(?:\s+[A-Z]{2,}){2,}\b")
_PUNCT_RE = re.compile(r"[?!]{3,}")
# Text inside code blocks, quotes of logs and inline code is not the user
# talking: a stack trace that says "fatal" or a variable named ``dumb``.
_CODE_RE = re.compile(r"```.*?```|`[^`\n]*`", re.DOTALL)


def frustration_signal(text: Any) -> str | None:
    """A short label (``profanity``, ``negative``, ``phrase``, ``shouting``)
    when a user message reads as angry or frustrated, else None.

    Swearing, an explicit frustration phrase or shouting is enough on its
    own; a single negative word ("terrible", "seriously") counts only next
    to repeated ``?!`` or a second negative word, so "the old UI was
    terrible" alone does not fire."""
    body = _CODE_RE.sub(" ", str(text or ""))[:4_000]
    if not body.strip():
        return None
    if _PROFANITY_RE.search(body):
        return "profanity"
    if _PHRASE_RE.search(body):
        return "phrase"
    if _SHOUT_RE.search(body):
        return "shouting"
    negatives = len(_NEGATIVE_RE.findall(body))
    if negatives >= 2 or (negatives and _PUNCT_RE.search(body)):
        return "negative"
    return None


# ── rendering ───────────────────────────────────────────────────────────
def rules_for(entries: Iterable[Any], *, project: str | None = None,
              model: Any = None, family: str | None = None) -> list[Any]:
    """The live rules (MemoryEntry or dict) that apply to a session in
    ``project`` on ``model`` (or ``family``): ``all`` plus that family's,
    user-kept first, then newest."""
    wanted = family or family_for(model)
    from fluid.codemode.facts import is_superseded

    def get(entry, name, default=None):
        return entry.get(name, default) if isinstance(entry, dict) else getattr(entry, name, default)

    out = []
    for entry in entries:
        applies = rule_family(get(entry, "key"))
        if applies not in (ALL, wanted):
            continue
        scope = get(entry, "scope", "global")
        if project is not None and scope not in ("global", project):
            continue
        if get(entry, "conflicted") or is_superseded(entry):
            continue
        out.append(entry)
    out.sort(key=lambda e: (not get(e, "protected"), -(get(e, "updated_at") or 0.0)))
    return out


def render_section(entries: Iterable[Any], *, project: str | None = None,
                   model: Any = None) -> str | None:
    """The ``# Learned behaviour`` prompt section for a session, or None
    when no rule applies. Rules are shown in full within
    :data:`RULES_PROMPT_CHARS`; a rule that fails the injection screen is
    left out."""
    from fluid.context.screening import screen_findings

    lines: list[str] = []
    room = RULES_PROMPT_CHARS
    for entry in rules_for(entries, project=project, model=model):
        text = getattr(entry, "text", None) if not isinstance(entry, dict) else entry.get("text")
        text = " ".join(str(text or "").split())
        if not text or screen_findings(text):
            continue
        line = f"- {text}"
        if len(line) + 1 > room:
            break
        room -= len(line) + 1
        lines.append(line)
    if not lines:
        return None
    return f"{SECTION_HEADING}\n{SECTION_INTRO}\n\n" + "\n".join(lines)
