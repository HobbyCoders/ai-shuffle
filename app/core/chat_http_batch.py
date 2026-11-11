"""The batch form of ChatHttpRequest: one call, up to eight requests.

The tool handler, the owner gate (``background_permissions``) and saved
permission rules (``permission_handler``) all read a call through
:func:`items`, so they never disagree about which requests a call sends.
No network or grant logic lives here; each item still goes through the
broker on its own.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from app.core.chat_files import AccessDenied

LIMIT = 8
FIELDS = ("grant_id", "method", "path", "body", "content_type")
SHAPE = (f"requests must be a list of 1 to {LIMIT} request objects, "
         "each with grant_id, method and path.")


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _filler(item: Any) -> bool:
    """A plain-JSON provider's placeholder item: an object with only blank values."""
    return isinstance(item, dict) and all(_blank(value) for value in item.values())


def items(arguments: Mapping[str, Any]) -> list[dict] | None:
    """The requests of a batched call, or None for the single-request form.

    Raises AccessDenied for a malformed batch. Providers that fill every
    field send blank values for the form they do not use: a blank
    ``requests`` (null, "", [] or only blank objects) beside top-level
    fields is the single form, and blank top-level fields beside a batch
    are ignored. A JSON-encoded ``requests`` string is read as its list.
    """
    raw = arguments.get("requests")
    if isinstance(raw, str):
        if not raw.strip():
            raw = None
        else:
            try:
                raw = json.loads(raw)
            except ValueError:
                raise AccessDenied(SHAPE) from None
    if raw is None:
        return None
    single = any(not _blank(arguments.get(name)) for name in FIELDS)
    if isinstance(raw, list) and single and all(_filler(item) for item in raw):
        return None
    if not isinstance(raw, list) or not 1 <= len(raw) <= LIMIT or not all(isinstance(i, dict) for i in raw):
        raise AccessDenied(SHAPE)
    beside = [name for name in FIELDS if not _blank(arguments.get(name))]
    if beside:
        raise AccessDenied(f"Put {', '.join(beside)} inside each requests item, not beside requests.")
    return raw
