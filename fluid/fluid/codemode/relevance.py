"""Bounded lexical relevance for memory, never a source of authority.

No model call, embedding service or persistent query log. Stop words keep short
continuations from retrieving spurious matches; whole words avoid matching
substrings in unrelated prose. Explicit Recall also preserves short acronyms.
"""
from __future__ import annotations

from collections.abc import Callable
from functools import lru_cache
import re
import unicodedata

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


_RECALL_WORDS = re.compile(r"c\+\+|c#|[^\W_]+")
_RECALL_PARTS = re.compile(r"c\+\+|c#|[^\W_]+|[^\w\s]")


def _recall_words(text: str) -> tuple[str, ...]:
    """Shared Unicode words: marks extend a word in every script, never split it."""
    text = unicodedata.normalize("NFC", text.casefold())
    if text.isascii():
        return tuple(_RECALL_WORDS.findall(text))
    words: list[str] = []
    current = ""
    end = -1
    for match in _RECALL_PARTS.finditer(text):
        if match.start() != end and current:
            words.append(current)
            current = ""
        part = match.group()
        if part in {"c++", "c#"}:
            if current:
                words.append(current)
                current = ""
            words.append(part)
        elif part[0].isalnum():
            current += part
        elif (unicodedata.category(part[0]).startswith("M") or part in "\u200c\u200d") and current:
            current += part
        elif current:
            words.append(current)
            current = ""
        end = match.end()
    if current:
        words.append(current)
    return tuple(words)


def _recall_text(text: str) -> str:
    # Keep negation intact: don't -> dont, never don (Donation).
    text = re.sub(r"(\w+)n['’]t\b", r"\1nt", text, flags=re.I)
    return re.sub(r"['’](?:s|d|m|re|ve|ll)\b", "", text, flags=re.I)


def recall_terms(text: str) -> frozenset[str]:
    """Bounded explicit keywords, including acronyms, C++/C#, Go and Make."""
    text = _recall_text(text[:4000]).casefold()
    addresses = re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", text)
    text = re.sub(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", " ", text)
    words = list(dict.fromkeys(addresses + list(_recall_words(text))))
    # Drop lone fragments in sentences, but retain a one-keyword name.
    words = [w for w in words if len(w) > 1 or len(words) == 1]
    filtered = [w for w in words if w not in (_STOP - {"go", "make"})]
    return frozenset((filtered or words)[:64])


@lru_cache(maxsize=4096)
def _recall_index(text: str) -> tuple[str, frozenset[str], tuple[str, ...]]:
    """Bounded immutable index; a rewrite's full text is a different key."""
    text = _recall_text(text)
    words = frozenset(_recall_words(text)) | frozenset(_recall_words(_CAMEL_BOUNDARY.sub(" ", text)))
    return (unicodedata.normalize("NFC", text.casefold()), words,
            tuple(word for word in words if _NO_SPACES.search(word)))


def recall_scorer(terms: frozenset[str]) -> Callable[[str], int]:
    """Prepare exact short names, long prefixes and non-space script matching.

    Keep whole names and CamelCase parts; UI must not match inside build.
    Query classification stays local: no query cache or persisted history.
    """
    prepared = []
    for term in terms:
        if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", term):
            prepared.append((term, "pattern", re.compile(r"(?<![\d.])" + re.escape(term) + r"(?!\d|\.\d)")))
        elif term in {"c++", "c#"}:
            prepared.append((term, "pattern", re.compile(r"(?<!\w)" + re.escape(term) + r"(?![a-z_])")))
        elif _NO_SPACES.search(term):
            prepared.append((term, "unsegmented", None))
        elif len(term) <= 2:
            prepared.append((term, "short", re.compile(r"(?<![a-z0-9_])" + re.escape(term) + r"(?![a-z0-9_])")))
        else:
            prepared.append((term, "prefix", None))
    query = tuple(prepared)

    def score(text: str) -> int:
        folded, words, unsegmented = _recall_index(text)
        total = 0
        for term, kind, pattern in query:
            if kind == "pattern":
                matched = bool(pattern.search(folded))
            elif kind == "unsegmented":
                matched = term in folded
            elif kind == "short":
                matched = term in words or any(pattern.search(word) for word in unsegmented)
            else:
                matched = any(word.startswith(term) for word in words) or any(term in word for word in unsegmented)
            total += bool(matched)
        return total

    return score


def recall_relevance(text: str, terms: frozenset[str]) -> int:
    """Convenience scorer; retrieval prepares once per query with recall_scorer."""
    return recall_scorer(terms)(text)



def relevance(text: str, terms: frozenset[str]) -> int:
    """Number of distinct query words in a fact or stable fact key."""
    if not terms:
        return 0
    return len(terms.intersection(re.findall(r"[^\W_]+", text.casefold())))
