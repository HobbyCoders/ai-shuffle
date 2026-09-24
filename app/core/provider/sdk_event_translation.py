"""Legacy idle-reader seam for provider events.

All live adapters now produce the app-owned event vocabulary themselves. Keep
this function for older callers, without a second set of message classes or a
runtime dependency on a provider client.
"""
from typing import Any


def to_provider_event(message: Any) -> Any:
    return message
