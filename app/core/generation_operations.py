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
    # these words in the endpoint ID pick them out.
    id_hints: tuple[str, ...] = ()
    # Subscription accounts generate from a prompt only.
    native: bool = False


OPERATIONS: dict[str, Operation] = {op.id: op for op in (
    Operation("text-to-image", "image", "Text to image", ("text-to-image",), ("generateImage",), native=True),
    Operation("edit-image", "image", "Edit image", ("image-to-image",), ("editImage",)),
    Operation("reference-to-image", "image", "Image from references", ("image-to-image",), ("generateWithReference",)),
    Operation("text-to-video", "video", "Text to video", ("text-to-video",), ("generateVideo",), native=True),
    Operation("image-to-video", "video", "Image to video", ("image-to-video",), ("imageToVideo",)),
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


def for_endpoint(category: str | None, endpoint_id: str, kind: str) -> Operation | None:
    """The most specific operation a fal endpoint performs, if any of ``kind``."""
    matches = [op for op in OPERATIONS.values() if op.kind == kind and fits(op, category, endpoint_id)]
    # An endpoint naming a hint ("extend", "reference") is that operation.
    return next((op for op in matches if op.id_hints), matches[0] if matches else None)


def fits(op: Operation, category: str | None, endpoint_id: str) -> bool:
    if category not in op.categories:
        return False
    return not op.id_hints or any(hint in endpoint_id.casefold() for hint in op.id_hints)
