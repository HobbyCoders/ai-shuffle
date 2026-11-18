"""What a generation call does to its inputs, and which models can do it.

One table maps each operation to its output kind, the tools that perform it
and the fal catalog categories (plus an endpoint-ID hint where fal files
several operations under one category). The model picker, the source catalog
and the tools all read it from here, so the card never offers an
image-to-video model to a text-to-video call.

Remembered choices stay per kind and belong to the kind's text operation:
the four text tools (generateImage, generateVideo, generateAudio, textTo3D)
are the ones that use them.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Operation:
    id: str
    kind: str
    label: str
    categories: tuple[str, ...]
    tools: tuple[str, ...] = ()
    # fal files reference and extend endpoints under broad categories;
    # these words in the endpoint ID pick them out ...
    id_hints: tuple[str, ...] = ()
    # ... and keep them out of the single-input operation sharing the category.
    exclude_hints: tuple[str, ...] = ()
    # Subscription accounts generate from a prompt only. A text operation
    # takes every prompt-only (``text-to-*``) category of its kind, as fal
    # adds them (text-to-music, text-to-speech, ...); ``categories`` names
    # the known ones.
    native: bool = False


OPERATIONS: dict[str, Operation] = {op.id: op for op in (
    Operation("text-to-image", "image", "Text to image", ("text-to-image",), ("generateImage",), native=True),
    Operation("edit-image", "image", "Edit image", ("image-to-image",), ("editImage",), exclude_hints=("reference",)),
    Operation("reference-to-image", "image", "Image from references", ("image-to-image",), ("generateWithReference",)),
    Operation("text-to-video", "video", "Text to video", ("text-to-video",), ("generateVideo",), native=True),
    Operation("image-to-video", "video", "Image to video", ("image-to-video",), ("imageToVideo",),
              exclude_hints=("reference",)),
    Operation("reference-to-video", "video", "Video from references", ("image-to-video", "video-to-video"),
              id_hints=("reference",)),
    Operation("extend-video", "video", "Extend video", ("video-to-video",), ("extendVideo",), id_hints=("extend",)),
    Operation("text-to-audio", "audio", "Text to audio", ("text-to-audio", "text-to-speech"), ("generateAudio",),
              native=True),
    Operation("audio-to-audio", "audio", "Transform audio", ("audio-to-audio",), ("audioToAudio",)),
    Operation("text-to-3d", "3d", "Text to 3D", ("text-to-3d",), ("textTo3D",), native=True),
    Operation("image-to-3d", "3d", "Image to 3D", ("image-to-3d",), ("imageTo3D",)),
)}

TEXT_OPERATION = {op.kind: op.id for op in OPERATIONS.values() if op.native}


def resolve(kind: str, operation: str | None = None) -> Operation:
    """The named operation for ``kind``, or the kind's text operation.

    Raises ValueError for an unknown operation or one of another kind.
    """
    op = OPERATIONS.get(operation or TEXT_OPERATION.get(kind, ""))
    if op is None or op.kind != kind:
        raise ValueError("Unknown generation operation for this kind.")
    return op


def for_tool(tool_name: str) -> Operation | None:
    """The operation a generation tool performs (``mcp__ai-tools__`` optional)."""
    name = tool_name.rsplit("__", 1)[-1]
    return next((op for op in OPERATIONS.values() if name in op.tools), None)


def fits(op: Operation, category: str | None, endpoint_id: str) -> bool:
    """Whether a fal endpoint performs ``op``. Callers have already kept only
    models of ``op.kind``; an uncategorized endpoint fits nothing."""
    if not category:
        return False
    if op.native:
        if not category.startswith("text-to-"):
            return False
    elif category not in op.categories:
        return False
    endpoint = endpoint_id.casefold()
    if any(hint in endpoint for hint in op.exclude_hints):
        return False
    return not op.id_hints or any(hint in endpoint for hint in op.id_hints)


_MEDIA_NOUN = {"image_url": "an image", "image_urls": "an image", "video_url": "a video", "audio_url": "an audio file"}


@dataclass(frozen=True)
class ToolRequest:
    """The fal request a media tool really builds for a picked endpoint."""
    endpoint: str
    # Input fields the tool fills itself. Its values win over model options
    # (``applyExtraParams`` in tools/providers/shared/fal-helpers.ts).
    sends: tuple[str, ...]
    # The one of ``sends`` that carries the tool's media input.
    media: str | None = None


def _image_route(model: str, declared: set[str], *, edit: bool) -> ToolRequest | str:
    # tools/providers/image/fal-ai.ts: knownEditRoute + catalogEditRoute +
    # buildEditInput (editImage), referenceRoute with one reference
    # (generateWithReference). Kontext always gets one ``image_url`` (with two
    # or more references it is sent to fal-ai/flux-pro/kontext/multi instead).
    if "kontext" in model:
        return ToolRequest(model, ("prompt", "image_url"), "image_url")
    if model in ("fal-ai/nano-banana-2", "fal-ai/nano-banana-2/edit"):
        return ToolRequest("fal-ai/nano-banana-2/edit", ("prompt", "image_urls"), "image_urls")
    if "nano-banana" in model:
        return ToolRequest("fal-ai/nano-banana-pro/edit", ("prompt", "image_urls"), "image_urls")
    # editImageFieldFromOpenapi: a declared list field wins over a single one.
    for field in ("image_urls", "image_url"):
        if field in declared:
            return ToolRequest(model, ("prompt", field), field)
    return "This model does not take a source image." if edit else "This model does not take a reference image."


def tool_request(op: Operation, model: str, declared: set[str]) -> ToolRequest | str:
    """The request ``op``'s tool builds for fal ``model``, or why it cannot.

    Mirrors the Node builders exactly, id-based routing included;
    tools/tests/fixtures/generation-tool-requests.json pins both sides.
    Text operations run through the resolver: the prompt plus the options.
    """
    if op.id in ("edit-image", "reference-to-image"):
        return _image_route(model, declared, edit=op.id == "edit-image")
    if op.id == "image-to-video":
        # tools/providers/video/fal-ai.ts imageToVideo: a text-to-video ID is
        # swapped for its image-to-video twin; the tool fills these fields.
        endpoint = model if "image-to-video" in model else model.replace("text-to-video", "image-to-video")
        return ToolRequest(endpoint, ("prompt", "image_url", "aspect_ratio", "duration"), "image_url")
    if op.id == "extend-video":
        # tools/providers/video/fal-ai.ts extend (prompt only when given).
        return ToolRequest(model, ("video_url",), "video_url")
    if op.id == "audio-to-audio":
        # tools/audio-generation/audioToAudio.ts -> executeFalModel.
        return ToolRequest(model, ("audio_url",), "audio_url")
    if op.id == "image-to-3d":
        # tools/providers/model3d/fal-ai.ts imageTo3D: text-to-3d IDs swapped.
        endpoint = model.replace("text-to-3d", "image-to-3d") if "text-to-3d" in model else model
        return ToolRequest(endpoint, ("image_url",), "image_url")
    return ToolRequest(model, ())


def _file_input(name: str) -> bool:
    """A field that takes a file (an image, video or audio URL)."""
    return name.endswith("_url") or name.endswith("_urls")


def _media_fits(schema, name: str, value) -> bool:
    """Whether the tool's media value satisfies that field's own declaration."""
    prop = ((schema.input_schema or {}).get("properties") or {}).get(name)
    if isinstance(prop, dict):
        from jsonschema import Draft7Validator
        try:
            Draft7Validator({"type": "object", "properties": {name: prop},
                             "definitions": schema.input_schema.get("definitions", {}),
                             "components": schema.input_schema.get("components", {})}).validate({name: value})
        except Exception:  # noqa: BLE001 - any schema mismatch means it does not fit
            return False
        return True
    field = next((f for f in schema.fields if f.name == name), None)
    return field is None or (field.type == "array") == isinstance(value, list)


def input_problem(op: Operation, schema, options: dict | None = None) -> str | None:
    """Why a fal model's declared inputs do not fit ``op``'s tool, or None.

    ``schema`` is the model's ``ModelSchema``; ``options`` the pick's saved
    options, if any. The request is the one the Node tool really builds
    (``tool_request``): the picked endpoint must be the one it calls, its
    media field must be one the model declares and fits, and every other
    required field needs the tool, a default or a saved option to fill it. A
    required file input only ever comes from the tool's own call, never a
    saved option.
    """
    tool = op.tools[0] if op.tools else "this tool"
    names = {f.name: f for f in schema.fields}
    declared = set(names) | set(((schema.input_schema or {}).get("properties") or {}))
    route = tool_request(op, schema.endpoint_id, declared)
    if isinstance(route, str):
        return f"{route[:-1]}, which {tool} sends."
    if route.endpoint != schema.endpoint_id:
        return f"{tool} runs {route.endpoint} for this model, not the model picked."
    if route.media:
        if route.media not in declared:
            other = next((n for n in ("image_urls", "image_url") if n in declared and n != route.media), None)
            where = f" (it takes {other})" if other else ""
            noun = _MEDIA_NOUN[route.media].split(" ", 1)[1]
            return f"{tool} sends its {noun} in {route.media}, which this model does not take{where}."
        sample = "https://example.invalid/input"
        if not _media_fits(schema, route.media, [sample] if route.media.endswith("_urls") else sample):
            return f"This model's {route.media} does not accept the one input {tool} sends."
    if schema.input_schema is not None:
        required = set(schema.input_schema.get("required") or ())
    else:
        required = {f.name for f in schema.fields if f.required}
    saved = {k for k in (options or {}) if not _file_input(k)}
    missing = sorted(name for name in required
                     if name not in route.sends and name != schema.primary_text_field
                     and (_file_input(name) or (name not in saved
                                                and (name not in names or names[name].default is None))))
    if missing:
        return f"This model needs {', '.join(missing)}, which {tool} does not send."
    return None


def schema_problem(op: Operation, schema: dict | None, options: dict | None = None) -> str | None:
    """``input_problem`` for a candidate's serialised schema (None: no schema to check)."""
    if not isinstance(schema, dict) or not schema.get("fields"):
        return None
    from app.services.fal_schema import ModelSchema, ParamField
    try:
        parsed = ModelSchema(schema.get("endpoint_id") or "", fields=[ParamField(**f) for f in schema["fields"]],
                             primary_text_field=schema.get("primary_text_field"),
                             input_schema=schema.get("input_schema"))
    except (TypeError, ValueError):
        return None
    return input_problem(op, parsed, options)
