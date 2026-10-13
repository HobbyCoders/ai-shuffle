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

    def to_dict(self) -> dict:
        return {
            "endpoint_id": self.endpoint_id,
            "primary_text_field": self.primary_text_field,
            "fields": [f.to_dict() for f in self.fields],
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

    if isinstance(enum, list) and enum:
        ptype = "enum"
    elif _is_file_field(name, prop):
        ptype = "file"
    elif json_type == "integer":
        ptype = "integer"
    elif json_type == "number":
        ptype = "number"
    elif json_type == "boolean":
        ptype = "boolean"
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

    return ModelSchema(endpoint_id=endpoint_id, fields=fields, primary_text_field=primary)


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
