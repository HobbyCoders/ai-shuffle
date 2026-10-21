"""Account-scoped Native generation defaults and explicitly verified capabilities."""
from pydantic import BaseModel, ConfigDict, StrictBool, StrictInt, StrictStr
from app.db import database


class NativeImageSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: StrictStr
    size: StrictStr
    quality: StrictStr | None = None
    format: StrictStr


class NativeVideoSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: StrictStr
    duration: StrictInt
    resolution: StrictStr
    aspect_ratio: StrictStr
    generate_audio: StrictBool = False


class NativeGenerationSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    image: NativeImageSettings | None
    video: NativeVideoSettings | None


_IMAGE_DEFAULTS = {
    "chatgpt": dict(model="default", size="auto", quality="auto", format="png"),
    "grok": dict(model="grok-imagine-image", size="1k", quality=None, format="jpeg"),
}
_VIDEO_DEFAULT = dict(model="grok-imagine-video-1.5-lite", duration=1, resolution="480p", aspect_ratio="16:9", generate_audio=False)


def provider_kind(row):
    from app.core.provider.fluid_credentials import _is_codex_subscription, _is_grok_subscription, _is_xai_api
    if _is_grok_subscription(row):
        return "grok"
    if _is_codex_subscription(row):
        return "chatgpt"
    if _is_xai_api(row):
        return "xai"
    if row.get("provider_type") == "openai":
        return "openai"
    raise ValueError("This provider has no Native generation capability table.")


def _options(verified, locked=()):
    return ([{"value": v, "verified": True, "selectable": True} for v in verified] +
            [{"value": v, "verified": False, "selectable": False, "reason": "not yet verified"}
             for v in locked if v not in verified])


def capabilities(kind):
    if kind not in ("chatgpt", "grok", "openai", "xai"):
        raise ValueError("Unknown Native generation provider.")
    defaults = _IMAGE_DEFAULTS.get(kind)
    xai = kind in ("grok", "xai")
    image_candidates = dict(model=["grok-imagine-image", "grok-imagine-image-2.0"] if xai else ["default"],
                            size=["1k", "2k"] if xai else ["auto"],
                            quality=[] if xai else ["auto"], format=["jpeg", "png"] if xai else ["png"])
    image = {"available": defaults is not None, "quality_visible": not xai,
             "fields": {k: _options([defaults[k]] if defaults and defaults[k] is not None else [], values)
                        for k, values in image_candidates.items()},
             "verified_combination": defaults, "aspect_ratios": ["1:1"] if xai else ["auto"],
             "route": "images/generations" if xai else "responses"}
    video_fields = dict(model=["grok-imagine-video-1.5-lite"], duration=list(range(1, 16)),
                        resolution=["480p", "720p", "1080p"],
                        aspect_ratio=["1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3", "21:9", "5:2"],
                        generate_audio=[False, True])
    video = {"available": kind == "grok", "fields": {}, "verified_combination": None,
             "modes": ["text-to-video"] if kind == "grok" else []}
    if kind in ("grok", "xai"):
        video["fields"] = {k: _options([_VIDEO_DEFAULT[k]] if kind == "grok" else [], values)
                           for k, values in video_fields.items()}
    if kind == "grok":
        video["verified_combination"] = dict(_VIDEO_DEFAULT)
        video["billing_note"] = "The provider reports a USD cost; this is not free or unlimited."
    else:
        video["reason"] = "unavailable on ChatGPT plan" if kind == "chatgpt" else "not yet verified"
    if not defaults:
        image["reason"] = "not yet verified"
    return {"image": image, "video": video}


def validate(kind, value):
    """Image settings: only the verified combination, never arbitrary options."""
    if kind not in _IMAGE_DEFAULTS or value.model_dump() != _IMAGE_DEFAULTS[kind]:
        raise ValueError("Unsupported or not yet verified Native generation image options.")
    return value


def validate_generation(kind, value):
    if value.image is not None:
        validate(kind, value.image)
    if value.video is not None and (kind != "grok" or value.video.model_dump() != _VIDEO_DEFAULT):
        raise ValueError("Unsupported or not yet verified Native generation video options.")
    return value


def _account(provider_id):
    row = database.get_model_provider(provider_id, include_token=False)
    if row is None:
        raise ValueError("Unknown Native generation account.")
    return row, provider_kind(row)


def load(provider_id):
    _, kind = _account(provider_id)
    raw = database.get_system_setting(f"native_generation:{provider_id}")
    if raw is not None:
        return validate_generation(kind, NativeGenerationSettings.model_validate_json(raw))
    image = _IMAGE_DEFAULTS.get(kind)
    # Read compatibility with the unshipped family-level native_images keys.
    # Reading never copies credentials, writes settings or changes another account.
    legacy = database.get_system_setting(f"native_images_{kind}") if image else None
    if legacy:
        old = NativeImageSettings.model_validate_json(legacy)
        if kind == "grok" and old.quality == "default":
            old.quality = None
        image = validate(kind, old).model_dump()
    return NativeGenerationSettings(image=image, video=_VIDEO_DEFAULT if kind == "grok" else None)


def save(provider_id, value):
    _, kind = _account(provider_id)
    validate_generation(kind, value)
    database.set_system_setting(f"native_generation:{provider_id}", value.model_dump_json())


def account_view(provider_id):
    row, kind = _account(provider_id)
    # Return an allowlisted projection, never the provider row or token metadata.
    return {"provider_id": provider_id, "name": row.get("name", ""), "provider_kind": kind,
            "settings": load(provider_id).model_dump(), "capabilities": capabilities(kind)}


def list_accounts():
    result = []
    for row in database.get_all_model_providers(include_token=False):
        try:
            provider_kind(row)
        except ValueError:
            continue
        result.append(account_view(str(row["id"])))
    return {"accounts": result}
