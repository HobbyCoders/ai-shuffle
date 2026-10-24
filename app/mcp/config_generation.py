"""Conversational controls for the owner's remembered and job generation picks."""

from typing import Any

from app.mcp.config_helpers import _error, _tool_result


def _create_generation_tools(is_admin: bool, *, writable: bool = False) -> list[Any]:
    from app.db.async_database import run_sync
    from app.mcp.inproc import tool

    @tool(
        "generationPreferences",
        "Read, remember or forget generation picks for image/video/audio/3d. "
        "Use searchModels IDs, never guess. Optional job_id scopes a pick to an existing "
        "automation (pin account and worker). Writes need the user's acknowledgment "
        "and confirm=true. Saves do not generate, verify availability or approve spending; "
        "the next turn rechecks choices, price and caps.",
        {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["get", "remember", "forget"]},
                "kind": {"type": "string", "enum": ["image", "video", "audio", "3d"]},
                "job_id": {"type": "string", "description": "Existing automation ID."},
                "choice": {
                    "type": "object",
                    "properties": {
                        key: {"type": "string"} for key in ("source", "account", "worker", "model")
                    }
                    | {"options": {"type": "object"}},
                    "required": ["model"],
                    "additionalProperties": False,
                },
                "confirm": {"type": "boolean"},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
    )
    async def generation_preferences(args: dict[str, Any]) -> dict:
        if not is_admin:
            return _error("App configuration tools require admin access.")
        action, kind = args.get("action"), args.get("kind")
        if action not in ("get", "remember", "forget"):
            return _error("Unknown generation preference action.")
        if (kind is not None or action != "get") and kind not in ("image", "video", "audio", "3d"):
            return _error("Choose image, video, audio or 3d.")
        if action != "get" and not writable:
            return _error("Generation picks can only be changed in an interactive owner chat.")
        if action != "get" and args.get("confirm") is not True:
            return _error(
                "Explain the change and get the user's acknowledgment, then set confirm=true."
            )

        def apply() -> dict:
            from app.core import generation_choices as choices
            from app.core.preference_identity import principal_preference_identity

            # Same authenticated owner namespace as the Settings API and run context.
            # Never accept an identity/account principal supplied in tool arguments.
            identity = principal_preference_identity({"kind": "admin"})
            job_id = args.get("job_id")
            if job_id is not None:
                job_id = choices.validate_job_id(job_id)
            if action == "get":
                stored = (
                    choices.load_job(identity, job_id)
                    if job_id
                    else choices.load(identity, run_migration=False)["remembered"]
                )
                if kind is not None:
                    stored = {kind: stored[kind]} if kind in stored else {}
                return {"choices": stored, "job_id": job_id}
            if action == "remember":
                value = choices.clean(args.get("choice"))
                if value is None:
                    raise ValueError("A remembered pick needs a choice.")
                if job_id:
                    choice = choices.save_job(identity, job_id, kind, value)
                else:
                    choice = choices.save(identity, kind, value, remember=True)
                return {
                    "kind": kind,
                    "choice": choice,
                    "job_id": job_id,
                    "message": "Saved for subsequent turns. Availability and price are "
                    "not verified by saving; execution rechecks every guard.",
                }
            if job_id:
                choices.save_job(identity, job_id, kind, None)
            else:
                choices.forget(identity, kind)
            return {"kind": kind, "choice": None, "job_id": job_id}

        try:
            return _tool_result({"success": True, **await run_sync(apply)})
        except (ValueError, TypeError):
            return _error(
                "Invalid choice or job: use an existing automation ID and a model "
                "with source/account; job choices also need account and worker."
            )
        except Exception:  # noqa: BLE001 - never expose identity/database internals
            # Identity/database errors can include private infrastructure details.
            return _error(
                "Generation preferences could not be accessed. Nothing was generated. "
                "Read the choices again before retrying a change."
            )

    return [generation_preferences]
