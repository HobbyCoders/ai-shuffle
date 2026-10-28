"""
fal.ai Model Schema Service

Fetches and normalizes the OpenAPI 3.0 *input* schema for any fal.ai model
endpoint, so the UI can render model-specific parameter controls dynamically
(instead of hardcoding per-model payload shapes).

This is the foundation for the schema-driven "pick any fal model" system:

    GET https://api.fal.ai/v1/models?endpoint_id=<id>&expand=openapi-3.0

returns an OpenAPI 3.0 document for the model. We resolve the submit
endpoint's request-body schema, flatten it into a list of normalized
parameter fields, and tag the "primary" text/prompt field plus any
file/url inputs (reference image, source audio, etc.).

Schemas are cached in memory with a TTL — they change rarely and a network
round-trip per model selection would make the picker feel sluggish.
"""

import logging
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

FAL_MODELS_API = "https://api.fal.ai/v1/models"
DEFAULT_SCHEMA_TTL = 24 * 3600  # schemas change rarely; cache for a day

# fal's documented ImageSize presets (not arbitrary model resolution labels):
# https://fal.ai/learn/tools/how-to-use-gpt-image-2
_IMAGE_SIZE_PRESETS = {
    "square_hd": (1024, 1024),
    "square": (512, 512),
    "portrait_4_3": (768, 1024),
    "portrait_16_9": (576, 1024),
    "landscape_4_3": (1024, 768),
    "landscape_16_9": (1024, 576),
}

# Candidate names for the model's primary text/prompt input, in priority order.
_PRIMARY_TEXT_FIELDS = (
    "prompt",
    "text",
    "gen_text",
    "script",
    "input_text",
    "text_input",
    "input",
    "query",
)

# Property names / formats that indicate a file-or-URL input the user should
# upload rather than type (reference image, source audio, etc.).
_FILE_NAME_HINTS = (
    "image_url",
    "audio_url",
    "video_url",
    "reference_audio_url",
    "ref_audio_url",
    "image",
    "audio",
    "video",
    "file_url",
    "source_url",
    "mask_url",
)


@dataclass
class ParamField:
    """A single normalized input parameter for a model."""

    name: str
    type: str = "string"  # string | integer | number | boolean | enum | array | file
    title: str = ""
    description: str = ""
    required: bool = False
    default: Any = None
    enum: Optional[list] = None
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    format: Optional[str] = None
    is_file: bool = False
    is_primary_text: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ModelSchema:
    """Normalized input schema for a fal.ai model endpoint."""

    endpoint_id: str
    fields: list[ParamField] = field(default_factory=list)
    primary_text_field: Optional[str] = None
    input_schema: Optional[dict] = None

    def validate_options(self, options: dict, prompt: str, *, partial: bool = False) -> dict:
        """Validate tool input against the same declared fields the picker uses.

        Undeclared parameters are refused, not silently dropped. Defaults and
        the primary text field are resolved once, before pricing and execution.
        ``partial`` checks only named saved settings, never request completeness.
        """
        import math

        fields = {f.name: f for f in self.fields}
        if set(options) - fields.keys():
            raise ValueError("Unknown model option; use the model's declared parameters.")
        values = {} if partial else {f.name: f.default for f in self.fields if f.default is not None}
        values.update(options)
        if not self.primary_text_field:
            raise ValueError("This model has no declared text input.")
        if self.primary_text_field in options:
            raise ValueError("Pass the prompt separately, not in options.")
        if not partial:
            values[self.primary_text_field] = prompt
        if self.input_schema is not None:
            # Keep nested object/array constraints and local references from
            # the provider's OpenAPI document. Never resolve external URLs.
            from jsonschema import Draft7Validator
            def local_refs(value):
                if isinstance(value, dict):
                    if "$ref" in value and not str(value["$ref"]).startswith("#/"):
                        raise ValueError("External model schema references are not supported.")
                    for item in value.values():
                        local_refs(item)
                elif isinstance(value, list):
                    for item in value:
                        local_refs(item)
            local_refs(self.input_schema)
            schema = self.input_schema
            if partial:
                # Only property values are saved, not a complete request. Keep
                # reference targets and nested constraints, but omit root-level
                # requirements/combinations involving other per-call inputs.
                schema = {key: schema[key] for key in ("properties", "definitions", "$defs", "components")
                          if key in schema}
            try:
                Draft7Validator(schema).validate(values)
            except Exception:
                # Validation errors include the user's values and schema. Do
                # not return those in a provider/tool error or application log.
                raise ValueError("Model options do not match the declared input schema.") from None
            return values
        for name, f in fields.items():
            if name not in values:
                if f.required and not partial:
                    raise ValueError(f"Missing required model option: {name}.")
                continue
            value = values[name]
            valid = True
            if f.enum is not None:
                valid = any(type(value) is type(item) and value == item for item in f.enum)
            elif f.type in ("string", "file"):
                valid = isinstance(value, str)
            elif f.type == "integer":
                valid = type(value) is int
            elif f.type == "number":
                valid = type(value) in (int, float) and math.isfinite(value)
            elif f.type == "boolean":
                valid = type(value) is bool
            elif f.type == "array":
                valid = isinstance(value, list)
            if valid and type(value) in (int, float):
                valid = (math.isfinite(value) and (f.minimum is None or value >= f.minimum)
                         and (f.maximum is None or value <= f.maximum))
            if not valid:
                raise ValueError(f"Invalid model option: {name}.")
        return values

    def pricing_quantity(self, unit: str, options: dict, prompt: str | None = None):
        """Conservative billable units from this model's declared parameters.

        fal's documented ImageSize presets have fixed pixel dimensions. For an
        unresolved size, only a fully understood enum can bound the largest
        allowed output; arbitrary resolution labels and tokens are not guessed.
        Missing duration units or text lengths leave the price unknown. Whole
        billing units round up so a fractional block cannot under-reserve the cap.
        """
        from decimal import Decimal, ROUND_CEILING
        import re

        fields = {f.name: f for f in self.fields}
        def number(value):
            if type(value) not in (int, float, str):
                raise ValueError("Unknown billable quantity.")
            value = Decimal(str(value))
            if not value.is_finite() or value <= 0:
                raise ValueError("Unknown billable quantity.")
            return value

        count_fields = [name for name in ("num_images", "number_of_images", "num_outputs", "num_videos", "num_samples", "batch_size")
                        if name in fields and name in options]
        if len(count_fields) > 1:
            raise ValueError("Ambiguous output count.")
        count = number(options[count_fields[0]]) if count_fields else Decimal(1)
        if count != count.to_integral_value() or count > 100:
            raise ValueError("Invalid output count.")
        unit = re.sub(r"\s+", " ", unit.strip().casefold())
        if unit.startswith("per "):
            unit = unit[4:]
        unit = {"requests": "request", "inferences": "inference", "images": "image",
                "videos": "video", "audios": "audio", "meshes": "mesh",
                "generations": "generation", "characters": "character",
                "seconds": "second", "minutes": "minute", "megapixels": "megapixel"}.get(unit, unit)
        if unit in ("request", "inference"):
            return Decimal(1), Decimal(1)
        if unit in ("image", "video", "audio", "mesh", "3d", "generation"):
            return Decimal(1), count
        if unit in ("character", "characters", "1k chars", "1000 characters"):
            if prompt is None or not prompt:
                raise ValueError("Text length is needed for pricing.")
            divisor = 1000 if unit in ("1k chars", "1000 characters") else 1
            quantity = (Decimal(len(prompt)) / divisor).to_integral_value(rounding=ROUND_CEILING)
            return quantity, count
        if unit in ("second", "seconds", "minute", "minutes"):
            durations = []
            for name in ("duration", "duration_seconds", "duration_sec", "duration_minutes", "seconds"):
                if name not in fields or name not in options:
                    continue
                field = fields[name]
                value = str(options[name])
                match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)\s*(s|sec|seconds?|m|min|minutes?)?", value)
                if not match:
                    continue
                declaration = " ".join((name, field.title, field.description)).lower()
                suffix = match[2]
                if suffix:
                    in_minutes = suffix.startswith("m")
                elif "millisecond" in declaration:
                    continue
                elif "minute" in declaration:
                    in_minutes = True
                elif "second" in declaration or name in ("duration_sec", "seconds"):
                    in_minutes = False
                else:
                    continue
                durations.append(number(match[1]) * (60 if in_minutes else 1))
            if len(durations) != 1:
                raise ValueError("No unambiguous declared duration in seconds.")
            quantity = durations[0] / (60 if unit.startswith("minute") else 1)
            return quantity.to_integral_value(rounding=ROUND_CEILING), count
        if unit in ("megapixel", "megapixels"):
            dimensions = options.get("image_size") if "image_size" in fields else None
            if isinstance(dimensions, str) and dimensions in _IMAGE_SIZE_PRESETS:
                width, height = _IMAGE_SIZE_PRESETS[dimensions]
                dimensions = {"width": width, "height": height}
            if dimensions is None and {"width", "height"} <= fields.keys():
                dimensions = options
            if not isinstance(dimensions, dict) and "image_size" in fields:
                allowed = fields["image_size"].enum
                if allowed and all(isinstance(size, str) and size in _IMAGE_SIZE_PRESETS for size in allowed):
                    width, height = max((_IMAGE_SIZE_PRESETS[size] for size in allowed), key=lambda size: size[0] * size[1])
                    dimensions = {"width": width, "height": height}
            if not isinstance(dimensions, dict) or any(type(dimensions.get(k)) is not int for k in ("width", "height")):
                raise ValueError("Declared pixel dimensions or a bounded ImageSize preset are required.")
            quantity = number(dimensions["width"]) * number(dimensions["height"]) / 1_000_000
            return quantity.to_integral_value(rounding=ROUND_CEILING), count
        raise ValueError("Unsupported declared pricing unit.")

    def to_dict(self) -> dict:
        return {
            "endpoint_id": self.endpoint_id,
            "primary_text_field": self.primary_text_field,
            "fields": [f.to_dict() for f in self.fields],
            "input_schema": self.input_schema,
        }


def _resolve_ref(ref: str, root: dict) -> dict:
    """Resolve a local JSON-schema ``$ref`` (e.g. '#/components/schemas/Foo')."""
    if not ref.startswith("#/"):
        return {}
    node: Any = root
    for part in ref[2:].split("/"):
        if not isinstance(node, dict):
            return {}
        node = node.get(part)
        if node is None:
            return {}
    return node if isinstance(node, dict) else {}


def _deref(schema: dict, root: dict, _depth: int = 0) -> dict:
    """Follow a ``$ref`` once and merge single-branch allOf, defensively bounded."""
    if not isinstance(schema, dict) or _depth > 5:
        return schema if isinstance(schema, dict) else {}
    if "$ref" in schema:
        resolved = _resolve_ref(schema["$ref"], root)
        # Preserve sibling keys (title/description/default) declared alongside $ref.
        merged = {**resolved, **{k: v for k, v in schema.items() if k != "$ref"}}
        return _deref(merged, root, _depth + 1)
    # fal frequently wraps enums as ``allOf: [{$ref: Enum}]``.
    all_of = schema.get("allOf")
    if isinstance(all_of, list) and len(all_of) == 1:
        merged = {**_deref(all_of[0], root, _depth + 1), **{k: v for k, v in schema.items() if k != "allOf"}}
        return merged
    return schema


def _find_input_schema(openapi: dict) -> Optional[dict]:
    """Locate the request-body input schema in a fal OpenAPI document.

    Strategy: scan POST paths for a JSON request body and use the first that
    resolves to a non-empty object schema. Falls back to a component schema
    whose name ends with "Input".
    """
    root = openapi

    # 1) Prefer a POST request body (the model's submit endpoint).
    paths = openapi.get("paths", {})
    if isinstance(paths, dict):
        for methods in paths.values():
            if not isinstance(methods, dict):
                continue
            post = methods.get("post")
            if not isinstance(post, dict):
                continue
            body = post.get("requestBody", {})
            content = body.get("content", {}) if isinstance(body, dict) else {}
            json_body = content.get("application/json", {}) if isinstance(content, dict) else {}
            raw = json_body.get("schema")
            if not raw:
                continue
            resolved = _deref(raw, root)
            if resolved.get("properties"):
                return resolved

    # 2) Fall back to a components schema named "...Input".
    schemas = openapi.get("components", {}).get("schemas", {})
    if isinstance(schemas, dict):
        for name, schema in schemas.items():
            if name.endswith("Input") and isinstance(schema, dict) and schema.get("properties"):
                return _deref(schema, root)
        # Last resort: the first object schema with properties.
        for schema in schemas.values():
            if isinstance(schema, dict) and schema.get("properties"):
                return _deref(schema, root)

    return None


def _is_file_field(name: str, prop: dict) -> bool:
    """Heuristic: does this property expect an uploaded file / URL?"""
    # Media-related names also occur on switches (generate_audio), counts
    # and arrays. Only strings can be a single file/URL input.
    if prop.get("type") not in (None, "string"):
        return False
    fmt = prop.get("format")
    lname = name.lower()
    if any(hint in lname for hint in _FILE_NAME_HINTS):
        # Plain "audio"/"image" could be a non-file enum (e.g. output format);
        # only treat as a file when it's a string/uri, not an enum.
        if prop.get("enum"):
            return False
        return True
    if fmt == "uri":
        return True
    return False


def _normalize_field(name: str, prop: dict, required: bool, root: dict) -> ParamField:
    prop = _deref(prop, root)
    json_type = prop.get("type")
    enum = prop.get("enum")

    if json_type == "boolean":
        ptype = "boolean"
    elif isinstance(enum, list) and enum:
        ptype = "enum"
    elif _is_file_field(name, prop):
        ptype = "file"
    elif json_type == "integer":
        ptype = "integer"
    elif json_type == "number":
        ptype = "number"
    elif json_type == "array":
        ptype = "array"
    else:
        ptype = "string"

    return ParamField(
        name=name,
        type=ptype,
        title=prop.get("title") or name.replace("_", " ").title(),
        description=prop.get("description", "") or "",
        required=required,
        default=prop.get("default"),
        enum=list(enum) if isinstance(enum, list) else None,
        minimum=prop.get("minimum"),
        maximum=prop.get("maximum"),
        format=prop.get("format"),
        is_file=ptype == "file",
    )


def _pick_primary_text(fields: list[ParamField]) -> Optional[str]:
    """Choose the field the user's main prompt text should populate."""
    by_name = {f.name: f for f in fields}
    for candidate in _PRIMARY_TEXT_FIELDS:
        f = by_name.get(candidate)
        if f and f.type in ("string",) and not f.is_file:
            return f.name
    # Otherwise: first required free-text string with no enum/format.
    for f in fields:
        if f.required and f.type == "string" and not f.enum and not f.is_file:
            return f.name
    return None


def normalize_openapi(endpoint_id: str, openapi: dict) -> Optional[ModelSchema]:
    """Convert a fal OpenAPI document into a normalized ModelSchema."""
    input_schema = _find_input_schema(openapi)
    if not input_schema:
        return None

    properties = input_schema.get("properties", {})
    required_names = set(input_schema.get("required", []) or [])
    if not isinstance(properties, dict):
        return None

    fields: list[ParamField] = []
    for name, prop in properties.items():
        if not isinstance(prop, dict):
            continue
        fields.append(_normalize_field(name, prop, name in required_names, openapi))

    primary = _pick_primary_text(fields)
    if primary:
        for f in fields:
            if f.name == primary:
                f.is_primary_text = True
                break

    return ModelSchema(endpoint_id=endpoint_id, fields=fields, primary_text_field=primary,
                       input_schema={**input_schema, "components": openapi.get("components", {})})


def _extract_openapi(model_data: dict) -> Optional[dict]:
    """Pull the OpenAPI document out of a fal models-API entry (shape-tolerant)."""
    for key in ("openapi", "openapi_3_0", "openapi-3.0", "openapi3"):
        doc = model_data.get(key)
        if isinstance(doc, dict) and doc:
            return doc
    metadata = model_data.get("metadata", {})
    if isinstance(metadata, dict):
        for key in ("openapi", "openapi_3_0", "openapi-3.0"):
            doc = metadata.get(key)
            if isinstance(doc, dict) and doc:
                return doc
    return None


class FalSchemaService:
    """Fetches and caches normalized input schemas for fal.ai models."""

    def __init__(self, cache_ttl: int = DEFAULT_SCHEMA_TTL):
        self._cache: dict[str, tuple[float, Optional[ModelSchema]]] = {}
        self._cache_ttl = cache_ttl

    def _cached(self, endpoint_id: str) -> Optional[tuple[float, Optional[ModelSchema]]]:
        entry = self._cache.get(endpoint_id)
        if not entry:
            return None
        ts, _ = entry
        if time.time() - ts > self._cache_ttl:
            return None
        return entry

    async def get_schema(
        self, endpoint_id: str, api_key: Optional[str] = None, force: bool = False
    ) -> Optional[ModelSchema]:
        """Return the normalized input schema for a model, or None if unavailable.

        Results (including a None "no schema" result) are cached so repeated
        selections don't re-hit the network.
        """
        endpoint_id = (endpoint_id or "").strip()
        if not endpoint_id:
            return None

        if not force:
            cached = self._cached(endpoint_id)
            if cached is not None:
                return cached[1]

        schema = await self._fetch(endpoint_id, api_key)
        self._cache[endpoint_id] = (time.time(), schema)
        return schema

    async def _fetch(self, endpoint_id: str, api_key: Optional[str]) -> Optional[ModelSchema]:
        headers = {}
        if api_key:
            headers["Authorization"] = f"Key {api_key}"
        params = {"endpoint_id": endpoint_id, "expand": "openapi-3.0"}

        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.get(FAL_MODELS_API, params=params, headers=headers)
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            logger.warning(f"Failed to fetch schema for {endpoint_id}: {e}")
            return None

        models = data.get("models") or []
        if not models:
            logger.info(f"No model entry returned for schema of {endpoint_id}")
            return None

        openapi = _extract_openapi(models[0])
        if not openapi:
            logger.info(f"No OpenAPI document in fal response for {endpoint_id}")
            return None

        try:
            return normalize_openapi(endpoint_id, openapi)
        except Exception as e:
            logger.warning(f"Failed to normalize schema for {endpoint_id}: {e}")
            return None


# Singleton instance
schema_service = FalSchemaService()
