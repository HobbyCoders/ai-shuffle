"""Rewind must execute beside the provider transcript, never on a mirror."""
from typing import Optional

from fastapi import HTTPException

from app.db.async_database import run_sync
from app.network import control


async def forward_rewind(session: dict, operation: str, params: Optional[dict] = None):
    owner = await run_sync(control.owner_of, session)
    if owner is None:
        return None
    try:
        result = await control.call(owner, "rewind", {
            "session_id": session["id"], "operation": operation, "params": params or {},
        }, timeout=120.0 if operation == "execute" else 20.0)
    except control.RunnerUnreachable as exc:
        raise HTTPException(503, "The device that owns this session is unreachable. "
                            "Reconnect it to rewind; the mirrored history has not been changed.") from exc
    except control.ControlRefused as exc:
        raise HTTPException(409, "The owning device could not handle rewind. "
                            "Check that it is online and updated. " + str(exc)) from exc
    target_seq = result.pop("_rewind_seq", None)
    if result.get("chat_rewound") and target_seq is not None:
        # The UI reloads immediately on success. Pull signed owner operations
        # before returning so it does not reload the old mirror. Sync is bounded
        # and never replays the mutating RPC if replication is temporarily down.
        try:
            await _sync_rewind(owner, session["id"], int(target_seq))
        except Exception:
            import logging
            logging.getLogger(__name__).warning("Owner rewind succeeded; mirror sync pending", exc_info=True)
            warning = "The owning device rewound successfully, but this view is still syncing. Do not retry the rewind."
            result["warning"] = " ".join(filter(None, [result.get("warning"), warning]))
    return result


async def _sync_rewind(owner: str, session_id: str, target_seq: int) -> None:
    import httpx
    from app.db import session_events
    from app.network import engine, transport

    link = transport._links[owner]  # authenticated by control.call above
    ctx = engine.require_context()
    async with httpx.AsyncClient(follow_redirects=False) as client:
        for _ in range(10):
            if await run_sync(session_events.get_max_seq, session_id) >= target_seq:
                return
            summary = await link.sync_once(client, ctx)
            if not summary.get("received"):
                break
    if await run_sync(session_events.get_max_seq, session_id) < target_seq:
        raise RuntimeError("Rewind event has not reached the mirror yet")


async def handle_rewind(body: dict, from_device: str) -> dict:
    # Device-token authentication is enforced by /peer/control. Check actual
    # ownership again here; a peer must not turn this worker into a relay or
    # cause it to truncate a read-only mirror (even one with a stale local id).
    from app.api.commands import RewindRequestV2, execute_rewind, get_rewind_checkpoints
    from app.network import catalog

    session_id = str(body.get("session_id") or "")
    await run_sync(control._require_owned, session_id)
    operation = body.get("operation")
    if operation == "checkpoints":
        result = await get_rewind_checkpoints(session_id)
    elif operation == "execute":
        params = body.get("params")
        if not isinstance(params, dict):
            raise ValueError("Rewind params must be an object")
        result = await execute_rewind(session_id, RewindRequestV2.model_validate(params))
        if result.chat_rewound:
            # Publish the real owner's rewind through the signed event stream;
            # never manufacture transcript.rewound on the requesting mirror.
            try:
                await run_sync(catalog.hydrate, session_id)
            except Exception:
                import logging
                logging.getLogger(__name__).exception("Owner rewind succeeded; publication pending")
                result.warning = " ".join(filter(None, [result.warning,
                    "Rewind succeeded on the owning device, but publishing its updated history is delayed. Do not retry."]))
    else:
        raise ValueError("Unsupported rewind operation")
    response = result.model_dump()
    if operation == "execute" and result.chat_rewound:
        from app.db import session_events
        response["_rewind_seq"] = await run_sync(session_events.get_max_seq, session_id)
    return response
