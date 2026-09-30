import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { CallToolRequestSchema, ListToolsRequestSchema, type CallToolResult } from "@modelcontextprotocol/sdk/types.js";
import { TOOL_DEFINITIONS } from "./tools/index.js";
import { SERVER_INSTRUCTIONS } from "./instructions.js";
import { errorResult } from "./tools/result.js";

const MAX_RESPONSE = 16 * 1024 * 1024;

export async function forwardTool(name: string, args: Record<string, unknown>, env = process.env, transport: typeof fetch = fetch): Promise<CallToolResult> {
  try {
    if (!env.AIS_SESSION_ID || !env.AIS_PANEL_TOKEN || !env.AIS_PANEL_REGISTER_URL) throw new Error();
    const url = new URL(env.AIS_PANEL_REGISTER_URL);
    url.pathname = url.pathname.replace(/\/register\/?$/, "/rpc");
    url.search = ""; url.hash = "";
    const body = JSON.stringify({ session_id: env.AIS_SESSION_ID, token: env.AIS_PANEL_TOKEN, tool: name, arguments: args });
    if (Buffer.byteLength(body) > 262144) return errorResult("Browser payload too large");
    // Node fetch has no browser Origin; satisfy the worker's same-origin guard.
    // The per-session body token still authenticates this internal request.
    const response = await transport(url, { method: "POST", headers: { "content-type": "application/json", origin: url.origin }, body, signal: AbortSignal.timeout(90000), redirect: "error" });
    if (!response.ok || !response.body) throw new Error();
    const chunks: Uint8Array[] = []; let length = 0;
    const reader = response.body.getReader();
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        length += value.length;
        if (length > MAX_RESPONSE) { await reader.cancel(); throw new Error(); }
        chunks.push(value);
      }
    } finally { reader.releaseLock(); }
    return JSON.parse(Buffer.concat(chunks).toString("utf8")) as CallToolResult;
  } catch {
    return errorResult("Browser runtime unavailable; outcome unknown, not replayed");
  }
}

export async function startProxy(): Promise<void> {
  const server = new Server({ name: "browser", version: "0.1.0" }, { capabilities: { tools: {} }, instructions: SERVER_INSTRUCTIONS });
  server.setRequestHandler(ListToolsRequestSchema, async () => ({ tools: TOOL_DEFINITIONS.map(({ name, description, inputSchema }) => ({ name, description, inputSchema })) }));
  server.setRequestHandler(CallToolRequestSchema, request => forwardTool(request.params.name, request.params.arguments ?? {}));
  // EOF owns only this transport, never the app-owned runtime or its tabs.
  process.stdin.on("end", () => process.exit(0));
  process.stdin.on("close", () => process.exit(0));
  await server.connect(new StdioServerTransport());
}
