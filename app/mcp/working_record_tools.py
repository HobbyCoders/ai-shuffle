"""One owner-only tool for durable working items (native and delegated legs)."""
from typing import Any


def build_tools(tool: Any, *, is_admin: bool, session_id: str) -> list[Any]:
    from app.core import working_record
    from app.db import working_record as store
    from app.db.async_database import run_sync

    @tool("WorkingRecord",
          "Read the owner's working record and live session states. list returns short previews, "
          "newest first, a page at a time (limit; offset=next_offset continues); get returns one "
          "item_id in full. Add a goal, decision, pending item or needs_owner; resolve an item by "
          "id when done, superseded, or a report has been acknowledged. Records survive settled "
          "chats and sync across workers. Never store secrets. list/get read; add/resolve write.",
          {"type": "object", "properties": {
              "action": {"type": "string", "enum": ["list", "get", "add", "resolve"]},
              "kind": {"type": "string", "enum": ["goal", "decision", "pending", "needs_owner"]},
              "content": {"type": "string", "maxLength": 2000},
              "item_id": {"type": "string"},
              "offset": {"type": "integer", "minimum": 0},
              "limit": {"type": "integer", "minimum": 1, "maximum": working_record.LIST_MAX},
              "include_resolved": {"type": "boolean"}},
           "required": ["action"], "additionalProperties": False})
    async def working_record_tool(args: dict[str, Any]) -> dict[str, Any]:
        try:
            if not is_admin:
                raise PermissionError("WorkingRecord is only available to the owner")
            await run_sync(working_record.require_owner, session_id)
            action = args.get("action")
            if action == "list":
                result = await run_sync(working_record.snapshot, session_id, offset=args.get("offset", 0),
                                        limit=args.get("limit", working_record.LIST_DEFAULT),
                                        include_resolved=bool(args.get("include_resolved")))
            elif action == "get":
                if not isinstance(args.get("item_id"), str) or not args["item_id"].strip():
                    raise ValueError("item_id is required")
                result = await run_sync(working_record.detail, session_id, args["item_id"].strip())
            elif action == "add":
                if args.get("kind") not in ("goal", "decision", "pending", "needs_owner"):
                    raise ValueError("kind must be goal, decision, pending or needs_owner")
                result = {"item_id": await run_sync(store.add, args["kind"], args.get("content"), session_id)}
            elif action == "resolve":
                if not isinstance(args.get("item_id"), str) or not args["item_id"].strip():
                    raise ValueError("item_id is required")
                result = {"resolved": await run_sync(store.resolve, args["item_id"])}
            else:
                raise ValueError("action must be list, get, add or resolve")
            return {"content": [{"type": "text", "text": working_record.dumps(result)}]}
        except (ValueError, PermissionError) as exc:
            return {"content": [{"type": "text", "text": str(exc)}], "isError": True}

    return [working_record_tool]
