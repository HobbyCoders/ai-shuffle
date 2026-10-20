"""Bounded lexical relevance for memory, never a source of authority.

No model call, embedding service or persistent query log. Stop words keep short
continuations from retrieving spurious matches; whole words avoid matching
substrings in unrelated prose. Explicit Recall also preserves short acronyms.
"""
from __future__ import annotations

import re

_STOP = frozenset("""
a an and are as at be been but by can could did do does for from get gets give
go had has have how i if in into is it its just like make me more my need now
of on or our please resume continue next okay ok proceed run say should so some
that the their them then there these they thing things this those to us use
user want was we were what when where which who why will with would you your
yes no thanks thank ahead again sure done fine
""".split())


def query_terms(text: str) -> frozenset[str]:
    """At most 64 distinct content words from a bounded user request."""
    words = re.findall(r"[^\W_]+", text[:4000].casefold())
    return frozenset(
        list(dict.fromkeys(w for w in words if len(w) >= 3 and w not in _STOP))[:64]
    )


_NO_SPACES = re.compile(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af\u0e00-\u0eff\u1000-\u109f\u1780-\u17ff]")
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])|(?<=[A-Z])(?=[A-Z][a-z])")


def _recall_text(text: str) -> str:
    # Keep negation intact: don't -> dont, never don (Donation).
    text = re.sub(r"(\w+)n['’]t\b", r"\1nt", text, flags=re.I)
    return re.sub(r"['’](?:s|d|m|re|ve|ll)\b", "", text, flags=re.I)


def recall_terms(text: str) -> frozenset[str]:
    """Bounded explicit keywords, including acronyms, C++/C#, Go and Make."""
    text = _recall_text(text[:4000]).casefold()
    addresses = re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", text)
    text = re.sub(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", " ", text)
    words = list(dict.fromkeys(addresses + re.findall(
        r"c\+\+|c#|[\u0e00-\u0eff\u1000-\u109f\u1780-\u17ff]+|[^\W_]+", text)))
    # Drop lone fragments in sentences, but retain a one-keyword name.
    words = [w for w in words if len(w) > 1 or len(words) == 1]
    filtered = [w for w in words if w not in (_STOP - {"go", "make"})]
    return frozenset((filtered or words)[:64])


def recall_relevance(text: str, terms: frozenset[str]) -> int:
    """Exact short names, long prefixes, CamelCase parts and non-space scripts.

    Keep whole names as well as their parts, so FactStore/store, GitHub/hub
    and GamingPC/PC work without matching UI inside build. Substring search
    is limited to words with non-space scripts, including mixed Latin text.
    """
    text = _recall_text(text)
    folded = text.casefold()
    parts = _CAMEL_BOUNDARY.sub(" ", text).casefold()
    words = set(re.findall(r"[^\W_]+", folded + " " + parts))
    unsegmented = [word for word in words if _NO_SPACES.search(word)]
    score = 0
    for term in terms:
        if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", term):
            matched = bool(re.search(r"(?<![\d.])" + re.escape(term) + r"(?!\d|\.\d)", folded))
        elif term in {"c++", "c#"}:
            matched = bool(re.search(r"(?<!\w)" + re.escape(term) + r"(?![a-z_])", folded))
        elif _NO_SPACES.search(term):
            matched = term in folded
        elif len(term) <= 2:
            # A mixed Japanese/Latin word can contain UI, but not via build.
            matched = term in words or any(re.search(
                r"(?<![a-z0-9_])" + re.escape(term) + r"(?![a-z0-9_])", word
            ) for word in unsegmented)
        else:
            matched = any(word.startswith(term) for word in words) or any(term in word for word in unsegmented)
        score += matched
    return score



def relevance(text: str, terms: frozenset[str]) -> int:
    """Number of distinct query words in a fact or stable fact key."""
    if not terms:
        return 0
    return len(terms.intersection(re.findall(r"[^\W_]+", text.casefold())))
