"""Compatibility names for unified media admission, never raw native transports."""
from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from fluid.tools.registry import ToolCtx, ToolOutcome, ToolSpec

from app.core.generation_resolver import Resolver, RunContext, result, with_current_choices
from app.services.generation_sources import Sources


def make_alias(
    kind: str, context: RunContext | None,
) -> tuple[ToolSpec, Callable[[dict[str, Any], ToolCtx], Awaitable[ToolOutcome]]]:
    """Bind the same trusted context and backend as the unified generation tools."""
    from app.core.provider import native_images, native_video

    original = native_images.SPEC if kind == "image" else native_video.SPEC
    properties = {
        **original.input_schema["properties"],
        "source": {"type": "string"},
        "model": {"type": "string"},
        "worker": {"type": "string"},
        "options": {"type": "object"},
        "confirmed": {"type": "boolean"},
        "confirmation_token": {"type": "string"},
    }
    spec = ToolSpec(
        name=original.name,
        description=(
            f"Compatibility alias for generate{kind.title()}. Uses the same saved/job choice, "
            "price, confirmation and daily-cap checks. With no saved choice, interactive calls "
            "offer a choice card; unattended calls skip. Never choose for the user. "
            "Use source/model/worker/options from the choice or approved confirmation retry."
        ),
        input_schema={
            "type": "object", "properties": properties,
            "required": ["prompt"], "additionalProperties": False,
        },
        risk=original.risk,
    )
    # Missing host context must not become an interactive/owner capability.
    context = context if isinstance(context, RunContext) else RunContext()
    resolver = Resolver(Sources(None, context))

    async def generate(args: dict[str, Any], ctx: ToolCtx) -> ToolOutcome:
        request = dict(args)
        options = request.get("options", {})
        if not isinstance(options, dict):
            output = result("refused", error="options must be an object.")
        else:
            options = dict(options)
            conflict = False
            if kind == "image":
                if "image_model" in request:
                    model = request.pop("image_model")
                    conflict = "model" in request and request["model"] != model
                    request["model"] = model
                for key in ("size", "quality", "format"):
                    if key in request:
                        value = request.pop(key)
                        conflict |= key in options and options[key] != value
                        options[key] = value
            request["options"] = options
            if conflict:
                output = result("refused", error="Conflicting native and unified options.")
            else:
                # These grants historically authorize native media, not fal.
                # Keep that scope while sharing every resolver admission rule.
                output = await resolver.generate(kind, request, await with_current_choices(context),
                                                 exclude_sources=("fal",))
        return ToolOutcome(
            text=json.dumps(output), result_fields=output,
            is_error=output["status"] in ("refused", "failed"),
        )

    return spec, generate
