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
    words = list(dict.fromkeys(re.findall(r"[^\W_]+", text[:4000].casefold())))
    # A literal one-keyword search may intentionally name Go or Make. Do not
    # erase it merely because it is also an English stop word.
    return frozenset((words if len(words) == 1 else [w for w in words if w not in _STOP])[:64])


def relevance(text: str, terms: frozenset[str]) -> int:
    """Number of distinct query words in a fact or stable fact key."""
    if not terms:
        return 0
    return len(terms.intersection(re.findall(r"[^\W_]+", text.casefold())))
