"""Execution capabilities derived from the same route selection as the adapter."""

from collections.abc import Mapping
from typing import Any


def supports_auto_permission(model: str, provider: Mapping[str, Any] | None = None) -> bool:
    from app.core.provider.harness_adapter import _decide_provider_kind
    from app.core.provider.types import SessionOptions

    options = SessionOptions(model=model)
    if provider:
        options.extra.update(
            provider_id=provider['id'],
            provider_type=provider.get('provider_type'),
            provider_auth_mode=provider.get('auth_mode'),
            provider_dialect=provider.get('api_dialect'),
        )
    return _decide_provider_kind(options) == 'delegated'
