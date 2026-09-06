#!/usr/bin/env node
/**
 * browser-mcp — STDIO MCP server for token-conservative browser control.
 *
 * One process per session. Connects to the supervised Chromium over CDP, drives
 * a single Page, and exposes the core four tools. The low-level Server is used
 * (not the high-level helper) so we emit our exact flat JSON schemas — no
 * anyOf/oneOf/$ref can leak in.
 */

import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  ListToolsRequestSchema,
  CallToolRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";

import { BrowserManager, BROWSER_RESTARTING, BrowserRestartingError } from "./browser.js";
import { Session, raceWithTimeout } from "./session.js";
import { SERVER_INSTRUCTIONS } from "./instructions.js";
import { TOOL_DEFINITIONS, TOOL_MAP } from "./tools/index.js";
import { normalizeOptionalInput, validateInput } from "./schemas.js";
import { errorResult } from "./tools/result.js";
import { reportCurrentTarget } from "./panel.js";
import { startWorkspaceBridge } from "./workspace-bridge.js";
import { setWorkspaceRuntimeIdentity } from "./workspace.js";

async function main(): Promise<void> {
  if (process.env.AIS_BROWSER_MODE === "proxy" && process.env.AIS_SESSION_ID && process.env.AIS_PANEL_TOKEN && process.env.AIS_PANEL_REGISTER_URL) {
    const { startProxy } = await import("./proxy.js");
    await startProxy();
    return;
  }
  const manager = new BrowserManager();
  const session = new Session(manager);
  setWorkspaceRuntimeIdentity(session);
  const bridge = startWorkspaceBridge(session);

  // Log the effective SSRF egress policy to stderr (stdout is the MCP channel).
  process.stderr.write(`[browser-mcp] ssrf policy: ${manager.ssrfPolicySummary()}\n`);

  const server = new Server(
    { name: "browser", version: "0.1.0" },
    {
      capabilities: { tools: {} },
      instructions: SERVER_INSTRUCTIONS,
    },
  );

  server.setRequestHandler(ListToolsRequestSchema, async () => ({
    tools: TOOL_DEFINITIONS.map((t) => ({
      name: t.name,
      description: t.description,
      inputSchema: t.inputSchema,
    })),
  }));

  server.setRequestHandler(CallToolRequestSchema, async (request) => {
    const def = TOOL_MAP.get(request.params.name);
    if (!def) return errorResult(`unknown tool: ${request.params.name}`);

    const rawArgs = (request.params.arguments ?? {}) as Record<string, unknown>;
    const args = normalizeOptionalInput(def.inputSchema, rawArgs);
    const valid = validateInput(def.inputSchema, args);
    if (!valid.ok) return errorResult(valid.error ?? "invalid input");

    try {
      // Serialize every tool execution for this session: the low-level Server
      // dispatches handlers without awaiting the previous one, so overlapping
      // calls would otherwise race on shared page/session state.
      if (def.name === "browser_status") return await def.run(session, args);
      return await session.runAgentExclusive(() => def.run(session, args));
    } catch (err) {
      if (err instanceof BrowserRestartingError) {
        return errorResult(BROWSER_RESTARTING);
      }
      const msg = err instanceof Error ? err.message : String(err);
      return errorResult(msg);
    }
  });

  // Cleanup: close the pages this process created (leave the browser to its
  // supervisor). Triggered by SIGTERM/SIGINT and by stdin EOF (client gone).
  let cleaningUp = false;
  const shutdown = async (code = 0): Promise<void> => {
    if (cleaningUp) return;
    cleaningUp = true;
    await bridge?.shutdown();
    // Best-effort: let the panel know no target is being driven anymore.
    reportCurrentTarget(null);
    try {
      await raceWithTimeout(manager.cleanup(), 3000, "browser cleanup timed out").catch(() => {});
    } finally {
      process.exit(code);
    }
  };

  process.on("SIGTERM", () => void shutdown(0));
  process.on("SIGINT", () => void shutdown(0));
  process.stdin.on("end", () => void shutdown(0));
  process.stdin.on("close", () => void shutdown(0));

  const transport = new StdioServerTransport();
  await server.connect(transport);
}

main().catch((err) => {
  process.stderr.write(`[browser-mcp] fatal: ${err instanceof Error ? err.stack : String(err)}\n`);
  process.exit(1);
});
