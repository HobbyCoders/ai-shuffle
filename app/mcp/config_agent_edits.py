"""
Fine-grained edit operations shared by the profile and subagent config tools.

``updateProfile`` and ``updateSubagent`` use one convention:

- ``prompt_edits``: an ordered list of ``{old, new}`` (exact replace; ``old``
  must occur exactly once) and ``{append}`` (adds a paragraph at the end)
  items, applied to the agent's own prompt text.
- ``add`` / ``remove``: objects keyed by a list field name. ``add`` appends
  entries (deduplicated, order kept); ``remove`` drops them. For the
  ``provider_models`` map, ``add`` is ``{provider_key: model}`` and ``remove``
  is a list of provider keys.

Every helper here is pure: the tools apply them inside their existing single-hop
read-modify-write closure, so a failed edit writes nothing.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

PROMPT_EDITS_SCHEMA: dict[str, Any] = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "old": {"type": "string"},
            "new": {"type": "string"},
            "append": {"type": "string"},
        },
    },
    "description": (
        "Patch the prompt instead of resending it; preferred for long prompts. "
        "In order: {old, new} replaces old, which must occur exactly once; "
        "{append} adds a paragraph at the end. Any failure saves nothing."
    ),
}

_EXCERPT_CONTEXT = 60
_EXCERPT_LIMIT = 300


def _excerpt(text: str, start: int, end: int) -> str:
    """The changed span with a little context, capped so results stay small."""
    snippet = text[max(0, start - _EXCERPT_CONTEXT):end + _EXCERPT_CONTEXT]
    if len(snippet) > _EXCERPT_LIMIT:
        half = _EXCERPT_LIMIT // 2
        snippet = snippet[:half] + " … " + snippet[-half:]
    return snippet


def apply_prompt_edits(
    text: str, edits: Any
) -> tuple[str | None, str | None, list[str]]:
    """Apply ``prompt_edits`` to ``text``.

    Returns ``(new_text, error, excerpts)``. On any invalid or non-matching
    item ``new_text`` is None and ``error`` names the item (1-based) and why.
    """
    if not isinstance(edits, list) or not edits:
        return None, "prompt_edits must be a non-empty list.", []
    text = text or ""
    excerpts: list[str] = []
    for index, item in enumerate(edits, start=1):
        label = f"prompt_edits[{index}]"
        if not isinstance(item, dict):
            return None, f"{label} must be an object.", []
        has_append = "append" in item
        has_replace = "old" in item or "new" in item
        if has_append == has_replace:
            return None, f"{label} must be either {{old, new}} or {{append}}.", []
        if has_append:
            addition = item["append"]
            if not isinstance(addition, str) or not addition.strip():
                return None, f"{label}.append must be non-empty text.", []
            base = text.rstrip("\n")
            text = f"{base}\n\n{addition}" if base.strip() else addition
            excerpts.append(_excerpt(text, len(text) - len(addition), len(text)))
            continue
        old, new = item.get("old"), item.get("new")
        if not isinstance(old, str) or not old:
            return None, f"{label}.old must be non-empty text.", []
        if not isinstance(new, str):
            return None, f"{label}.new must be text (\"\" deletes old).", []
        count = text.count(old)
        if count == 0:
            return None, f"{label}.old was not found in the prompt; nothing was saved.", []
        if count > 1:
            return None, (
                f"{label}.old matches {count} times; include more surrounding "
                "text so it matches exactly once. Nothing was saved."
            ), []
        start = text.index(old)
        text = text[:start] + new + text[start + len(old):]
        excerpts.append(_excerpt(text, start, start + len(new)))
    return text, None, excerpts


def normalized_system_prompt(value: Any) -> dict[str, Any]:
    """A stored ``system_prompt`` as a dict: legacy text is custom, and a
    missing prompt runs on the security text alone, i.e. an empty custom
    prompt; a dict without a type is a preset."""
    if isinstance(value, str):
        return {"type": "custom", "content": value}
    if not isinstance(value, dict):
        return {"type": "custom", "content": ""}
    return value


def patch_system_prompt(
    stored: Any, edits: Any
) -> tuple[dict[str, Any] | None, str | None, dict[str, Any]]:
    """Apply ``prompt_edits`` to a stored ``system_prompt`` without changing its kind.

    A custom prompt patches its content; a preset prompt patches the agent's
    own text appended to the preset. Returns ``(new_prompt, error, details)``.
    """
    current = normalized_system_prompt(stored)
    custom = current.get("type") == "custom"
    key = "content" if custom else "append"
    text, error, excerpts = apply_prompt_edits(current.get(key) or "", edits)
    if error:
        return None, error, {}
    patched = dict(current) if custom else {"preset": "claude_code", **current, "type": "preset"}
    patched[key] = text
    return patched, None, {
        "edited": "custom prompt" if custom else "text appended to the preset",
        "length": len(text),
        "changes": excerpts,
    }


def nullable(schema: dict[str, Any]) -> dict[str, Any]:
    """``schema`` that also accepts null (the update tools' "clear" value)."""
    result = {**schema, "type": [schema["type"], "null"]}
    if "enum" in schema:
        result["enum"] = [*schema["enum"], None]
    return result


def list_ops_schema(list_fields: Iterable[str], map_field: str = "provider_models") -> dict[str, Any]:
    """The ``add`` and ``remove`` properties for the given list fields plus one map."""
    list_fields = tuple(list_fields)
    strings = {"type": "array", "items": {"type": "string"}}
    add_props: dict[str, Any] = {name: strings for name in list_fields}
    remove_props: dict[str, Any] = dict(add_props)
    add_props[map_field] = {"type": "object", "additionalProperties": {"type": "string"}}
    remove_props[map_field] = strings
    return {
        "add": {
            "type": "object",
            "properties": add_props,
            "description": (
                f"Add entries by field without resending, e.g. {{\"{list_fields[0]}\": "
                f"[\"Read\"], \"{map_field}\": {{\"__native__\": \"sonnet\"}}}}; deduplicated."
            ),
        },
        "remove": {
            "type": "object",
            "properties": remove_props,
            "description": f"Remove entries by field ({map_field}: keys), after add.",
        },
    }


def _clean_strings(values: Any, label: str) -> tuple[list[str] | None, str | None]:
    if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
        return None, f"{label} must be a list of strings."
    return [v.strip() for v in values if v.strip()], None


def apply_list_ops(
    current: Any, add: Any, remove: Any, label: str
) -> tuple[list[str] | None, str | None, list[str]]:
    """Add then remove entries; returns ``(new_list, error, unmatched_removals)``."""
    result: list[str] = []
    for value in current if isinstance(current, list) else []:
        if isinstance(value, str) and value not in result:
            result.append(value)
    unmatched: list[str] = []
    if add is not None:
        additions, error = _clean_strings(add, f"add.{label}")
        if error:
            return None, error, []
        for value in additions:
            if value not in result:
                result.append(value)
    if remove is not None:
        removals, error = _clean_strings(remove, f"remove.{label}")
        if error:
            return None, error, []
        for value in removals:
            if value in result:
                result.remove(value)
            else:
                unmatched.append(value)
    return result, None, unmatched


def apply_map_ops(
    current: Any, add: Any, remove: Any, label: str
) -> tuple[dict[str, str] | None, str | None, list[str]]:
    """Set then remove map keys; returns ``(new_map, error, unmatched_removals)``."""
    result: dict[str, str] = dict(current) if isinstance(current, dict) else {}
    unmatched: list[str] = []
    if add is not None:
        if not isinstance(add, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in add.items()
        ):
            return None, f"add.{label} must map keys to strings.", []
        for key, value in add.items():
            if key.strip() and value.strip():
                result[key.strip()] = value.strip()
            else:
                return None, f"add.{label} needs a non-empty key and value.", []
    if remove is not None:
        keys, error = _clean_strings(remove, f"remove.{label}")
        if error:
            return None, error, []
        for key in keys:
            if result.pop(key, None) is None:
                unmatched.append(key)
    return result, None, unmatched


def pruned_provider_models(value: Any) -> Any:
    """Drop blank entries (the editor treats them as "unavailable").

    Non-string keys or values are left in place so the ProfileConfig check
    rejects them instead of this silently clearing them.
    """
    if not isinstance(value, dict):
        return value
    result = {}
    for key, model in value.items():
        if isinstance(key, str) and isinstance(model, str):
            if key.strip() and model.strip():
                result[key.strip()] = model.strip()
        else:
            result[key] = model
    return result


def list_ops_for(args: dict[str, Any], field: str) -> tuple[Any, Any]:
    """The ``add`` / ``remove`` values this call names for ``field``."""
    add = args.get("add") if isinstance(args.get("add"), dict) else {}
    remove = args.get("remove") if isinstance(args.get("remove"), dict) else {}
    return add.get(field), remove.get(field)


def unknown_op_fields(args: dict[str, Any], allowed: Iterable[str]) -> str | None:
    """Why ``add`` / ``remove`` is malformed or names an unsupported field."""
    allowed = tuple(allowed)
    for op in ("add", "remove"):
        if op not in args or args[op] is None:
            continue
        if not isinstance(args[op], dict):
            return f"{op} must be an object keyed by field name."
        unknown = sorted(set(args[op]) - set(allowed))
        if unknown:
            return (
                f"{op} does not support {', '.join(unknown)}; use one of: "
                + ", ".join(allowed) + "."
            )
    return None


def validate_profile_fields(config: dict[str, Any], keys: Iterable[str]) -> tuple[str | None, dict[str, Any]]:
    """Check ``config[keys]`` with the REST editor's ``ProfileConfig`` model.

    Only the keys this call touched are checked, so legacy values elsewhere in
    a stored config never block an unrelated edit. Returns ``(error, normalized)``
    where ``normalized`` holds the model's dump of each checked key.
    """
    from pydantic import ValidationError

    from app.core.models import ProfileConfig

    subset = {key: config[key] for key in keys if key in config}
    if not subset:
        return None, {}
    try:
        validated = ProfileConfig.model_validate(subset)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors()
        )
        return f"Invalid agent settings, nothing was saved: {problems}", {}
    return None, validated.model_dump(include=set(subset), exclude_none=True)
