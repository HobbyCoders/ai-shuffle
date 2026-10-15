import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { CallToolRequestSchema, ListToolsRequestSchema, type CallToolResult } from "@modelcontextprotocol/sdk/types.js";
import { TOOL_CATALOG } from "./catalog.js";
import { SERVER_INSTRUCTIONS } from "./instructions.js";
import { errorResult } from "./tools/result.js";

const MAX_RESPONSE = 16 * 1024 * 1024;

// Kept in sync with app/core/native_browser_proxy.py. Never echo server text.
const UNAVAILABLE_MESSAGES = new Map<string, string>([
  ["checkout_branch_mismatch", "Browser unavailable: this session's checkout is not on its branch (detached or switched HEAD). Switch the worktree back to the session's branch."],
  ["session_unavailable", "Session unavailable"],
  ["profile_unavailable", "Session profile unavailable"],
  ["project_not_checked_out", "Session project is not checked out on this device"],
  ["runtime_unavailable", "Browser runtime unavailable"],
  ["runtime_owned", "An existing browser runtime already owns this session"],
  ["runtime_closed", "Browser runtime closed"],
  ["runtime_identity_mismatch", "Browser runtime did not report its expected identity"],
  ["initial_tab_failed", "Browser could not open its initial tab"],
  ["runtime_expired", "Browser runtime expired"],
  ["runtime_disconnected", "Browser runtime disconnected"],
  ["runtime_busy", "Browser runtime busy"],
  ["outcome_unknown", "Browser runtime disconnected; outcome unknown"],
  ["start_failed", "Browser runtime could not start"],
  ["stale_build", "The browser tool build is out of date. Rebuild browser-mcp or redeploy the app image, then try again."],
]);

function unavailable(status: number, raw = ""): string {
  if (status === 503) {
    try {
      const body = JSON.parse(raw);
      const message = typeof body?.reason === "string" ? UNAVAILABLE_MESSAGES.get(body.reason) : undefined;
      if (message) return `${message} (HTTP 503); outcome unknown, not replayed`;
    } catch { /* Malformed and arbitrary bodies are never shown. */ }
  }
  return `Browser runtime unavailable (HTTP ${Math.trunc(status)}); outcome unknown, not replayed`;
}

async function unavailableResponse(response: Response): Promise<string> {
  if (response.status !== 503 || !response.body) return unavailable(response.status);
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = []; let length = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      length += value.length;
      if (length > 4096) { await reader.cancel(); return unavailable(response.status); }
      chunks.push(value);
    }
    return unavailable(response.status, Buffer.concat(chunks).toString("utf8"));
  } catch {
    return unavailable(response.status);
  } finally { reader.releaseLock(); }
}

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
    // A bounded 503 may name a known reason; only proxy-owned messages escape.
    if (!response.ok) return errorResult(await unavailableResponse(response));
    if (!response.body) throw new Error();
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
  server.setRequestHandler(ListToolsRequestSchema, async () => ({ tools: TOOL_CATALOG.map(({ name, description, inputSchema }) => ({ name, description, inputSchema })) }));
  server.setRequestHandler(CallToolRequestSchema, request => forwardTool(request.params.name, request.params.arguments ?? {}));
  // EOF owns only this transport, never the app-owned runtime or its tabs.
  process.stdin.on("end", () => process.exit(0));
  process.stdin.on("close", () => process.exit(0));
  await server.connect(new StdioServerTransport());
}
