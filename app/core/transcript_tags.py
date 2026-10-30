"""Literal, first-close tag extraction for non-XML transcript envelopes."""


def first_tagged_text(text: str, tag: str) -> str | None:
    """Return the first pair's raw body, or None (including an unclosed tag).

    Tags are fixed internal names, not user patterns. A later opener cannot
    complete if there is no close after the first one, so never retry a regex
    from every nested opener in a malformed model/user transcript.
    """
    opening, closing = f"<{tag}>", f"</{tag}>"
    start = text.find(opening)
    if start == -1:
        return None
    start += len(opening)
    end = text.find(closing, start)
    return text[start:end] if end != -1 else None
