"""External stdio MCP fixture: text, reported failure, and raised tool error."""
import json
import sys
from pathlib import Path

text = Path(sys.argv[1]).read_text(encoding="utf-8")
for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    method = request.get("method")
    params = request.get("params", {})
    result = {}
    if method in ("initialize", "server/discover"):
        version = params.get("protocolVersion", "2026-07-28")
        result = {"protocolVersion": version, "supportedVersions": [version],
                  "capabilities": {"tools": {}}, "serverInfo": {"name": "fixture", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": [{"name": "report", "description": "Report fixture data",
                            "inputSchema": {"type": "object", "properties": {"mode": {"type": "string"}}}}],
                  "resultType": "complete", "ttlMs": 60000, "cacheScope": "private"}
    elif method == "tools/call":
        mode = params.get("arguments", {}).get("mode")
        if mode == "exception":
            print(json.dumps({"jsonrpc": "2.0", "id": request["id"],
                              "error": {"code": -32603, "message": text}}), flush=True)
            continue
        result = {"content": [{"type": "text", "text": text}], "isError": mode == "error",
                  "resultType": "complete"}
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
