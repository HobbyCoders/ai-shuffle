"""A tiny in-repo stdio MCP server (hand-rolled JSON-RPC) for offline tests.

Deliberately SDK-free: it speaks newline-delimited JSON-RPC over stdio
directly, so the fixture works regardless of which ``mcp`` SDK generation is
installed on the client side. It answers BOTH the modern ``server/discover``
handshake (2026-07-28) and the legacy ``initialize`` (echoing the client's
requested protocolVersion so any client generation validates). Results carry
the fields the 2026-07-28 wire requires (``resultType``, and ``ttlMs`` /
``cacheScope`` on ``tools/list``); older clients ignore them.

Behavior knobs:
  * ``FLUID_FAKE_TTL`` — advertise a ``tools/list`` freshness hint (``ttlMs``)
    so the client's TTL cache can be exercised.
  * the ``need_input`` tool — returns an MRTR ``input_required`` result
    (an elicitation round-trip) to test that path.
"""

import json
import os
import sys

TOOLS = [
    {
        "name": "get_weather",
        "description": "Look up the current weather for a city.",
        "inputSchema": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "write_note",
        "description": "Persist a note to the server's scratch store.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
        },
    },
    {
        "name": "need_input",
        "description": "A tool that requires elicited input before finishing.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def _tools_list_result() -> dict:
    # The 2026-07-28 wire requires resultType, ttlMs and cacheScope on every
    # tools/list result; older clients ignore them. ttlMs=0 means "immediately
    # stale", which is the no-cache-hint case the legacy tests exercise.
    ttl = os.environ.get("FLUID_FAKE_TTL")
    return {
        "tools": TOOLS,
        "resultType": "complete",
        "ttlMs": int(ttl) if ttl else 0,
        "cacheScope": "private",
    }


def _call_tool_result(params: dict) -> dict:
    name = params.get("name")
    args = params.get("arguments") or {}
    if name == "get_weather":
        city = args.get("city", "?")
        return {
            "content": [{"type": "text", "text": f"{city}: sunny, 21C"}],
            "isError": False,
            "resultType": "complete",
        }
    if name == "write_note":
        return {
            "content": [{"type": "text", "text": "note saved"}],
            "isError": False,
            "resultType": "complete",
        }
    if name == "need_input":
        return {
            "content": [],
            "isError": False,
            "resultType": "input_required",
            "requestState": "rs-1",
            "inputRequests": {
                "confirm": {
                    "method": "elicitation/create",
                    "params": {
                        "mode": "form",
                        "message": "Confirm before continuing?",
                        "requestedSchema": {
                            "type": "object",
                            "properties": {"ok": {"type": "boolean"}},
                        },
                    },
                }
            },
        }
    return {
        "content": [{"type": "text", "text": f"unknown tool {name}"}],
        "isError": True,
        "resultType": "complete",
    }


def _handle(msg: dict) -> dict | None:
    """Response for a request; None for notifications."""
    method = msg.get("method", "")
    msg_id = msg.get("id")
    if msg_id is None:
        return None  # notification (e.g. notifications/initialized)
    params = msg.get("params") or {}
    if method in ("initialize", "server/discover"):
        # discover pins the modern version; initialize echoes the client's so
        # every SDK generation's version check passes.
        version = (
            "2026-07-28"
            if method == "server/discover"
            else params.get("protocolVersion") or "2026-07-28"
        )
        result = {
            "protocolVersion": version,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "fakesrv", "version": "0.1.0"},
        }
        if method == "server/discover":
            # DiscoverResult requires supportedVersions; without it a 2.0
            # client rejects the reply and falls back to initialize().
            result["supportedVersions"] = [version]
    elif method == "tools/list":
        result = _tools_list_result()
    elif method == "tools/call":
        result = _call_tool_result(params)
    elif method == "ping":
        result = {}
    else:
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": -32601, "message": f"method not found: {method}"},
        }
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = _handle(msg)
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
