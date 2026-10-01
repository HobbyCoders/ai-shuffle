/**
 * Tool registry: name → (1-2 line) description, flat JSON schema, and handler.
 * Descriptions stay terse on purpose; the driving discipline (ref lifecycle,
 * when to snapshot vs screenshot, coordinate space, staleness, console/network
 * usage, batch encoding) lives in the server-level `instructions` string.
 */

import type { Session } from "../session.js";
import type { JsonSchema } from "../schemas.js";
import { TOOL_CATALOG, type BrowserToolName } from "../catalog.js";
import { runNavigate } from "./navigate.js";
import { runSnapshot } from "./snapshot.js";
import { runAct, type ActInput } from "./act.js";
import { runScreenshot, type ScreenshotInput } from "./screenshot.js";
import { runFind } from "./find.js";
import { runReadText } from "./read_text.js";
import { runEval } from "./eval.js";
import { runConsole, type ConsoleInput } from "./console.js";
import { runNetwork, type NetworkInput } from "./network.js";
import { runTabs, type TabsInput } from "./tabs.js";
import { runResize, type ResizeInput } from "./resize.js";
import { runUpload, uploadStatus, type UploadInput } from "./upload.js";
import { runBatch, type BatchInput, type BatchTool } from "./batch.js";
import { jsonResult, type McpToolResult } from "./result.js";
import { runWait, type WaitInput } from "./wait.js";

export interface ToolDefinition {
  name: string;
  description: string;
  inputSchema: JsonSchema;
  run: (session: Session, input: Record<string, unknown>) => Promise<McpToolResult>;
}

const HANDLERS: Record<BrowserToolName, ToolDefinition["run"]> = {
  browser_status: async (session, input) => jsonResult({ ...(session.workspaceStatus?.() ?? { available: true, control: { owner: "agent", viewer_id: null, pending_viewer_id: null }, busy: session.busy, ...(await session.workspaceMetadata()) }), uploads: uploadStatus(input.upload_paths) }),
  browser_wait: (session, input) => runWait(session, input as unknown as WaitInput),
  browser_navigate: (session, input) => runNavigate(session, input as { url: string }),
  browser_snapshot: (session, input) =>
      runSnapshot(
        session,
        input as { filter?: "interactive" | "all"; scope_ref?: string; max_chars?: number },
      ),
  browser_act: (session, input) => runAct(session, input as unknown as ActInput),
  browser_screenshot: (session, input) => runScreenshot(session, input as ScreenshotInput),
  browser_find: (session, input) => runFind(session, input as { query: string }),
  browser_read_text: (session) => runReadText(session),
  browser_eval: (session, input) => runEval(session, input as { code: string }),
  browser_console: (session, input) => runConsole(session, input as unknown as ConsoleInput),
  browser_network: (session, input) => runNetwork(session, input as NetworkInput),
  browser_tabs: (session, input) => runTabs(session, input as unknown as TabsInput),
  browser_resize: (session, input) => runResize(session, input as ResizeInput),
  browser_upload: (session, input) => runUpload(session, input as unknown as UploadInput),
  browser_batch: (session, input) =>
      runBatch(session, input as unknown as BatchInput, (name) => {
        const def = TOOL_MAP.get(name);
        if (!def) return undefined;
        return { inputSchema: def.inputSchema, run: def.run } satisfies BatchTool;
      }),
};

export const TOOL_DEFINITIONS: ToolDefinition[] = TOOL_CATALOG.map(tool => ({
  ...tool, run: HANDLERS[tool.name],
}));

export const TOOL_MAP: Map<string, ToolDefinition> = new Map(
  TOOL_DEFINITIONS.map((t) => [t.name, t]),
);
