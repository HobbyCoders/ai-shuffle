"""Conservative supersession evidence, never last-write-wins or fuzzy deletion."""
from __future__ import annotations
import re
from fluid.codemode.relevance import query_terms

_REPLACEMENT = re.compile(r"\b(?:replac(?:es|ing)|supersedes)\s+([^;,.()\n]+)", re.I)
_EXCEPTION = re.compile(r"\b(?:for this (?:task|session)|one[- ]off|temporar\w*|today only)\b", re.I)
_UNCERTAIN = re.compile(r"\b(?:might|may|could|possibly|proposed|proposal|hypothetical)\b|\b(?:not|never|without)\s+(?:\w+\s+){0,2}(?:replac|supersed)", re.I)


def has_replacement_signal(text: str) -> bool:
    return bool(_REPLACEMENT.search(text)) and not (_EXCEPTION.search(text) or _UNCERTAIN.search(text))
_GENERIC = frozenset("owner user rule latest prefers preference replacing replaces supersedes fact current new old use uses says model".split())


def subject_terms(text: str) -> set[str]:
    # Singular/plural variants are subject evidence, not a semantic assertion.
    return {w[:-1] if len(w) > 4 and w.endswith("s") else w
            for w in query_terms(text) if w not in _GENERIC}


def replacement_evidence(old: dict, new: dict, *, proposed: bool = False) -> bool:
    """Only clear replacement text about the same subject, in compatible scopes.

    A global correction can replace a project rule, but a project exception
    cannot supersede a global rule or another project's convention. Tool-only
    observations and checkout-specific receipts cannot displace shared facts.
    """
    if old is new or old.get("id") == new.get("id") or old.get("superseded_by"):
        return False
    if new.get("provenance") == "tool" or old.get("worker") != new.get("worker"):
        return False
    old_scope, new_scope = old.get("scope", "global"), new.get("scope", "global")
    if old_scope != new_scope and not (new_scope == "global" and old_scope.startswith("project:")):
        return False
    if new.get("updated_at", 0) <= old.get("updated_at", 0):
        return False
    if (_EXCEPTION.search(old["text"]) or _EXCEPTION.search(new["text"])
            or _UNCERTAIN.search(new["text"]) or (not proposed and not has_replacement_signal(new["text"]))):
        return False
    # Do not use keys alone: alias keys are common, while similar keys can
    # describe different subjects. At least two content terms must agree.
    shared = subject_terms(old["text"]) & subject_terms(new["text"])
    if proposed:
        return len(shared) >= 3
    old_text = " ".join(old["text"].casefold().split())
    for match in _REPLACEMENT.finditer(new["text"]):
        phrase = match.group(1).strip().casefold()
        # Entity names may have punctuation (gpt-6-astra); whole-phrase
        # boundaries prevent alpha from superseding alphabet by accident.
        if (len(shared - subject_terms(phrase)) >= 2 and len(phrase) >= 3
                and re.search(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", old_text)):
            return True
    return False
