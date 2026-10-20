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


def recall_terms(text: str) -> frozenset[str]:
    """Bounded distinct Recall keywords, including short names such as uv/UI.

    Unlike automatic task matching, an explicit search may intentionally name
    a two-character tool. Punctuation separates key/path components; stop words
    never earn relevance merely by appearing in ordinary prose.
    """
    text = re.sub(r"['’](?:s|t|d|m|re|ve|ll)\b", "", text[:4000].casefold())
    addresses = re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", text)
    text = re.sub(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", " ", text)
    words = list(dict.fromkeys(addresses + [w for w in re.findall(r"[^\W_]+", text)
                                          if len(w) > 1 or len(text.strip()) == 1]))
    # Go and Make are explicit tool names even in multi-keyword queries. If
    # every word is filtered, retain the literal query rather than returning
    # nothing. Automatic task matching still uses the stricter query_terms.
    filtered = [w for w in words if w not in (_STOP - {"go", "make"})]
    return frozenset((filtered or words)[:64])


def recall_relevance(text: str, terms: frozenset[str]) -> int:
    """Exact short names, long word prefixes, and unsegmented CJK substrings.

    Preserve explicit keyword/stem recall without matching UI inside build or
    splitting IP addresses into common numeric components.
    """
    folded = text.casefold()
    words = re.findall(r"[^\W_]+", folded)
    score = 0
    for term in terms:
        if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", term):
            matched = bool(re.search(r"(?<![\d.])" + re.escape(term) + r"(?!\d|\.\d)", folded))
        elif re.search(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]", term):
            matched = term in folded
        elif len(term) <= 2:
            matched = term in words
        else:
            matched = any(word.startswith(term) for word in words)
        score += matched
    return score


def relevance(text: str, terms: frozenset[str]) -> int:
    """Number of distinct query words in a fact or stable fact key."""
    if not terms:
        return 0
    return len(terms.intersection(re.findall(r"[^\W_]+", text.casefold())))
