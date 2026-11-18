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
    # The input field(s) the tool fills with its one media input, in its order
    # of preference: it sends the first one the model declares (a list field
    # gets a one-item list). Text operations send only the prompt.
    media: tuple[str, ...] = ()
    # Subscription accounts generate from a prompt only. A text operation
    # takes every prompt-only (``text-to-*``) category of its kind, as fal
    # adds them (text-to-music, text-to-speech, ...); ``categories`` names
    # the known ones.
    native: bool = False


OPERATIONS: dict[str, Operation] = {op.id: op for op in (
    Operation("text-to-image", "image", "Text to image", ("text-to-image",), ("generateImage",), native=True),
    Operation("edit-image", "image", "Edit image", ("image-to-image",), ("editImage",), exclude_hints=("reference",),
              media=("image_urls", "image_url")),
    Operation("reference-to-image", "image", "Image from references", ("image-to-image",), ("generateWithReference",),
              media=("image_urls", "image_url")),
    Operation("text-to-video", "video", "Text to video", ("text-to-video",), ("generateVideo",), native=True),
    Operation("image-to-video", "video", "Image to video", ("image-to-video",), ("imageToVideo",),
              exclude_hints=("reference",), media=("image_url",)),
    Operation("reference-to-video", "video", "Video from references", ("image-to-video", "video-to-video"),
              id_hints=("reference",)),
    Operation("extend-video", "video", "Extend video", ("video-to-video",), ("extendVideo",), id_hints=("extend",),
              media=("video_url",)),
    Operation("text-to-audio", "audio", "Text to audio", ("text-to-audio", "text-to-speech"), ("generateAudio",),
              native=True),
    Operation("audio-to-audio", "audio", "Transform audio", ("audio-to-audio",), ("audioToAudio",), media=("audio_url",)),
    Operation("text-to-3d", "3d", "Text to 3D", ("text-to-3d",), ("textTo3D",), native=True),
    Operation("image-to-3d", "3d", "Image to 3D", ("image-to-3d",), ("imageTo3D",), media=("image_url",)),
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


def input_problem(op: Operation, schema) -> str | None:
    """Why a fal model's declared inputs do not fit ``op``'s tool, or None.

    ``schema`` is the model's ``ModelSchema``. The tool sends the prompt and,
    for a media operation, its one input in the first ``op.media`` field the
    model declares (as the Node tools route it). Every other required field
    needs a default: the run-time check (``ModelSchema.validate_options``)
    refuses the call otherwise, so the picker refuses the model first.
    """
    tool = op.tools[0] if op.tools else "this tool"
    names = {f.name: f for f in schema.fields}
    supplied: dict = {}
    if op.media:
        field = next((names[n] for n in op.media if n in names), None)
        if field is None:
            return f"This model does not take {_MEDIA_NOUN[op.media[0]]}, which {tool} sends."
        sample = "https://example.invalid/input"
        supplied[field.name] = [sample] if field.type == "array" else sample
    if schema.input_schema is not None:
        required = set(schema.input_schema.get("required") or ())
    else:
        required = {f.name for f in schema.fields if f.required}
    missing = sorted(name for name in required
                     if name not in supplied and name != schema.primary_text_field
                     and (name not in names or names[name].default is None))
    if missing:
        return f"This model needs {', '.join(missing)}, which {tool} does not send."
    if schema.primary_text_field:
        try:
            schema.validate_options(supplied, "A prompt.")
        except ValueError:
            return f"This model's declared inputs do not fit {tool}."
    return None


def schema_problem(op: Operation, schema: dict | None) -> str | None:
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
    return input_problem(op, parsed)
