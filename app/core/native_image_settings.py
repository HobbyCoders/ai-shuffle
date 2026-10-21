"""Verified subscription-native image defaults, separate from direct tool models."""
import json

from pydantic import BaseModel, ConfigDict, StrictStr

from app.db import database


class NativeImageSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: StrictStr
    size: StrictStr
    quality: StrictStr
    format: StrictStr


_DEFAULTS = {
    "chatgpt": dict(model="default", size="auto", quality="auto", format="png"),
    "grok": dict(model="grok-imagine-image", size="1k", quality="default", format="jpeg"),
}


def capabilities(provider: str) -> dict:
    if provider not in _DEFAULTS:
        raise ValueError("Unknown native image subscription provider.")
    defaults = _DEFAULTS[provider]
    return {"models": [defaults["model"]], "sizes": [defaults["size"]],
            "qualities": [defaults["quality"]], "formats": [defaults["format"]],
            "verified": True,
            "route": "images/generations" if provider == "grok" else "responses",
            "aspect_ratios": ["1:1"] if provider == "grok" else ["auto"],
            "unverified_options_exposed": False,
            "direct_tool_choice": "supergrok-subscription" if provider == "grok" else "chatgpt-subscription"}


def validate(provider: str, value: NativeImageSettings) -> NativeImageSettings:
    caps = capabilities(provider)
    for field, plural in (("model", "models"), ("size", "sizes"), ("quality", "qualities"), ("format", "formats")):
        if getattr(value, field) not in caps[plural]:
            raise ValueError(f"Unsupported native image {field} for {provider}; only verified options are accepted.")
    return value


def load(provider: str) -> NativeImageSettings:
    capabilities(provider)
    raw = database.get_system_setting(f"native_images_{provider}")
    if raw is None:
        return NativeImageSettings(**_DEFAULTS[provider])
    # Fail closed on malformed/obsolete persisted options, not a provider fallback.
    return validate(provider, NativeImageSettings.model_validate_json(raw))


def save(provider: str, value: NativeImageSettings) -> None:
    validate(provider, value)
    database.set_system_setting(f"native_images_{provider}", json.dumps(value.model_dump()))
