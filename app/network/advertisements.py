"""Owner-configured dial aliases, local to this worker and published in presence."""
from __future__ import annotations

import json

from app.db import database

SETTING = "network_extra_advertised_urls"
# Existing advertisements occupy at most six slots (tunnel, primary, four
# LAN addresses). Stay within previous peers' eight-endpoint wire limit.
MAX_URLS = 2
MAX_URL_LENGTH = 512


def validate_urls(values: object) -> list[str]:
    from app.network.tokens import canonical_endpoint
    from app.network.endpoints import is_valid_endpoint

    if not isinstance(values, list) or len(values) > MAX_URLS:
        raise ValueError(f"Supply at most {MAX_URLS} advertised addresses")
    result = []
    for value in values:
        if not isinstance(value, str) or len(value) > MAX_URL_LENGTH:
            raise ValueError(f"Each advertised address must be at most {MAX_URL_LENGTH} characters")
        # Same ASCII-host rule as the signature verifier; never re-encode IDNA
        # or infer aliases from request headers. Errors contain no input URLs.
        try:
            canonical = canonical_endpoint(value)
        except ValueError:
            raise ValueError("Use an HTTP(S) base URL with an ASCII host and no credentials, query or fragment") from None
        if not is_valid_endpoint(canonical):
            raise ValueError("Use a peer-dialable HTTP(S) address, not a link-local address")
        if canonical not in result:
            result.append(canonical)
    return result


def owner_urls() -> list[str]:
    raw = database.get_system_setting(SETTING)
    if not raw:
        return []
    try:
        return validate_urls(json.loads(raw))
    except (ValueError, TypeError):
        # Corrupt/local legacy values do not expand the trusted allowlist.
        return []


def set_owner_urls(values: list[str]) -> list[str]:
    urls = validate_urls(values)
    database.set_system_setting(SETTING, json.dumps(urls))
    return urls
