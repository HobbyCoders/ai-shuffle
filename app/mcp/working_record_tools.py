"""One owner-only tool for durable working items (native and delegated legs)."""
import json
from typing import Any


def build_tools(tool: Any, *, is_admin: bool, session_id: str) -> list[Any]:
    from app.core import working_record
    from app.db import working_record as store
    from app.db.async_database import run_sync

    @tool("WorkingRecord",
          "Read the owner's working record and live session states. Add a goal, decision, "
          "pending item or needs_owner; resolve an item by id when done, superseded, "
          "or a report has been acknowledged. Records survive settled chats and sync "
          "across workers. Never store secrets. list is read-only; add/resolve write.",
          {"type": "object", "properties": {
              "action": {"type": "string", "enum": ["list", "add", "resolve"]},
              "kind": {"type": "string", "enum": ["goal", "decision", "pending", "needs_owner"]},
              "content": {"type": "string", "maxLength": 2000},
              "item_id": {"type": "string"},
              "offset": {"type": "integer", "minimum": 0},
              "include_resolved": {"type": "boolean"}},
           "required": ["action"], "additionalProperties": False})
    async def working_record_tool(args: dict[str, Any]) -> dict[str, Any]:
        try:
            if not is_admin:
                raise PermissionError("WorkingRecord is only available to the owner")
            await run_sync(working_record.require_owner, session_id)
            action = args.get("action")
            if action == "list":
                offset = args.get("offset", 0)
                if not isinstance(offset, int) or offset < 0:
                    raise ValueError("offset must be a non-negative integer")
                result = await run_sync(working_record.snapshot, session_id, offset=offset,
                                        include_resolved=bool(args.get("include_resolved")))
            elif action == "add":
                if args.get("kind") not in ("goal", "decision", "pending", "needs_owner"):
                    raise ValueError("kind must be goal, decision, pending or needs_owner")
                result = {"item_id": await run_sync(store.add, args["kind"], args.get("content"), session_id)}
            elif action == "resolve":
                if not isinstance(args.get("item_id"), str) or not args["item_id"].strip():
                    raise ValueError("item_id is required")
                result = {"resolved": await run_sync(store.resolve, args["item_id"])}
            else:
                raise ValueError("action must be list, add or resolve")
            return {"content": [{"type": "text", "text": json.dumps(result, default=str)}]}
        except (ValueError, PermissionError) as exc:
            return {"content": [{"type": "text", "text": str(exc)}], "isError": True}

    return [working_record_tool]
