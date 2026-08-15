"""McpManager — owns MCP client connections and maps results to ToolOutcomes.

The registry talks to the manager; the manager owns one ``McpClient`` per
server. Kept in its own module (rather than folded into the client) so the
delegated engine's bundled server and future multi-server routing share the
same connection pool.
"""

from __future__ import annotations

from typing import Any

from fluid.tools.mcp.client import McpClient, ToolDescriptor


class McpManager:
    def __init__(self) -> None:
        self._clients: dict[str, McpClient] = {}

    async def connect(self, name: str, config: dict[str, Any]) -> list[ToolDescriptor]:
        client = McpClient(name, config)
        await client.start()
        self._clients[name] = client
        return await client.list_tools()

    async def refresh(self, name: str) -> list[ToolDescriptor]:
        return await self._clients[name].list_tools()

    def client(self, name: str) -> McpClient | None:
        return self._clients.get(name)

    async def dispatch(
        self,
        server: str,
        tool: str,
        input: dict[str, Any],
        *,
        input_responses: Any = None,
        request_state: str | None = None,
    ) -> "Any":
        # Imported here to avoid a cycle (registry imports the manager).
        from fluid.tools.registry import InputRequired, ToolOutcome

        client = self._clients.get(server)
        if client is None:
            return ToolOutcome(text=f"MCP server not connected: {server}", is_error=True)
        raw = await client.call_tool(
            tool, input, input_responses=input_responses, request_state=request_state
        )
        if raw.get("kind") == "input_required":
            marker = InputRequired(
                server=server, tool=tool,
                request_state=raw.get("request_state"),
                requests=raw.get("requests", {}),
            )
            # TODO(MRTR): the engine turns this marker into a question/permission
            # round-trip and retries ``dispatch(..., input_responses=...,
            # request_state=marker.request_state)``. The retry seam is wired
            # (kwargs plumb through to ``call_tool``); the engine-side collection
            # of ``inputResponses`` lands with AskUserQuestion in a later slice.
            return ToolOutcome(
                text=f"[input_required] server {server} needs input before completing {tool}",
                input_required=marker,
            )
        return ToolOutcome(text=raw.get("text", ""), is_error=raw.get("is_error", False))

    async def aclose(self) -> None:
        for client in self._clients.values():
            await client.aclose()
        self._clients.clear()
