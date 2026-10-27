"""The first words of a call: when the voice greets, and whom it greets.

The greeting is part of the voice prompt's playbook (``LIVE_INSTRUCTIONS``);
the engines only need a trigger to speak first. The trigger text is fixed here,
never user text. The caller's first name rides it as a bounded data line, and
only for the owner: a key's caller is never named, and a username is never
spoken as if it were a name.
"""
from __future__ import annotations

import logging
import re

from app.core.voice_prompts import (
    CALLER_NAME_NOTE,
    GREETING_NEW_CALL,
    GREETING_RETURNING_CALL,
)

logger = logging.getLogger(__name__)

# A first name, said aloud: letters with an inner apostrophe or hyphen
# ("O'Neil", "Anne-Marie"). Digits, underscores, @ and markup never pass.
FIRST_NAME_MAX_CHARS = 24
_FIRST_NAME = re.compile(r"[^\W\d_](?:[^\W\d_]|['’-](?=[^\W\d_]))*")
# Account and admin usernames that read like words, not names.
_NOT_NAMES = frozenset({
    "admin", "administrator", "owner", "user", "root", "guest", "test", "default",
    "me", "you", "system", "assistant", "operator", "anonymous", "unknown",
})


def spoken_first_name(value: object) -> str | None:
    """The first name to say, or None when ``value`` is not a person's name.

    "Sam Rivers" gives "Sam". A username such as "Quickkill0", "admin" or
    "sam_r", and anything lower-case or all-caps, gives None: a greeting
    with a wrong name is worse than one with none.
    """
    if not isinstance(value, str):
        return None
    words = value.split()
    if not words or len(value) > 80:
        return None
    first = words[0]
    if not _FIRST_NAME.fullmatch(first) or not 2 <= len(first) <= FIRST_NAME_MAX_CHARS:
        return None
    # Every word must read as a name, so "Sam_R 2" or "Sam <x>" is refused whole.
    if not all(_FIRST_NAME.fullmatch(word.strip(".")) for word in words):
        return None
    if not first[0].isupper() or (len(first) > 3 and first.isupper()):
        return None
    if first.lower() in _NOT_NAMES:
        return None
    return first


def owner_first_name() -> str | None:
    """The owner's first name from the About-you introduction. Synchronous.

    Only the stored introduction is read (detected name, or the owner's
    correction), never the network; None when it is unknown or not a name.
    """
    try:
        from app.core.owner_introduction import effective
        from app.db import owner_state
        account = owner_state.get_account() or {}
        return spoken_first_name(effective(account.get("introduction")).get("preferred_name"))
    except Exception:
        logger.debug("Voice greeting: owner name unavailable", exc_info=True)
        return None


def caller_first_name(session_id: str, api_user_id: str | None) -> str | None:
    """The name to greet with: the owner's, never a key's caller. Synchronous.

    ``api_user_id`` is the key that opened the call; a session a key owns
    (directly or through its parent) is never greeted by the owner's name.
    """
    if api_user_id:
        return None
    try:
        from app.core import api_user_scope
        from app.db import database
        if api_user_scope.session_api_user_id(database.get_session(session_id)):
            return None
    except Exception:
        logger.debug("Voice greeting: session owner unavailable", exc_info=True)
        return None
    return owner_first_name()


def greeting_note(name: str | None, returning: bool) -> str:
    """The fixed trigger that makes the voice speak first on a fresh call.

    ``returning``: the call joins a conversation that already has history.
    """
    text = GREETING_RETURNING_CALL if returning else GREETING_NEW_CALL
    clean = spoken_first_name(name)
    if clean:
        text += "\n" + CALLER_NAME_NOTE + clean
    return text
