"""Account-scoped Native generation defaults and explicitly verified capabilities."""
import json

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


def _validated_stored_groups(kind, data, defaults):
    # Once JSON parses, preserve each explicit null and valid peer group. An
    # invalid/missing video group is disabled, never silently enabled for billing.
    if not isinstance(data, dict):
        data = {}
    stale = set(data) != {"image", "video"}
    groups = {}
    for name, model in (("image", NativeImageSettings), ("video", NativeVideoSettings)):
        try:
            raw = data[name]
            value = model.model_validate(raw) if raw is not None else None
            if value is not None:
                if name == "image":
                    validate(kind, value)
                else:
                    validate_generation(kind, NativeGenerationSettings(image=None, video=value))
        except (KeyError, ValueError, TypeError):
            value = defaults.image if name == "image" else None
            stale = True
        groups[name] = value
    return NativeGenerationSettings(**groups), stale


def _load_with_status(provider_id):
    _, kind = _account(provider_id)
    defaults = NativeGenerationSettings(image=_IMAGE_DEFAULTS.get(kind), video=_VIDEO_DEFAULT if kind == "grok" else None)
    raw = database.get_system_setting(f"native_generation:{provider_id}")
    if raw is not None:
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            # Unknown owner choices must not enable billed video.
            return NativeGenerationSettings(image=defaults.image, video=None), True
        return _validated_stored_groups(kind, data, defaults)
    # Read compatibility only; never rewrite an account or echo stored values.
    legacy = database.get_system_setting(f"native_images_{kind}") if defaults.image else None
    if legacy is None:
        return defaults, False
    try:
        data = json.loads(legacy)
    except (ValueError, TypeError):
        return NativeGenerationSettings(image=defaults.image, video=None), True
    try:
        old = NativeImageSettings.model_validate(data)
        if kind == "grok" and old.quality == "default":
            old.quality = None
        defaults.image = validate(kind, old)
        return defaults, False
    except (ValueError, TypeError):
        return NativeGenerationSettings(image=defaults.image, video=None), True


def load(provider_id):
    return _load_with_status(provider_id)[0]


def save(provider_id, value):
    _, kind = _account(provider_id)
    validate_generation(kind, value)
    database.set_system_setting(f"native_generation:{provider_id}", value.model_dump_json())


def account_view(provider_id):
    row, kind = _account(provider_id)
    # Return an allowlisted projection, never the provider row or token metadata.
    value, stale = _load_with_status(provider_id)
    result = {"provider_id": provider_id, "name": row.get("name", ""), "provider_kind": kind,
              "settings": value.model_dump(), "capabilities": capabilities(kind), "stale": stale}
    if stale:
        result["reason"] = "Stored Native generation settings are invalid or no longer verified; disabled groups preserved and invalid groups reset safely."
    return result


def list_accounts():
    result = []
    for row in database.get_all_model_providers(include_token=False):
        try:
            provider_kind(row)
        except ValueError:
            continue
        result.append(account_view(str(row["id"])))
    return {"accounts": result}
