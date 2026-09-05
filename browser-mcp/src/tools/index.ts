/**
 * Tool registry: name → (1-2 line) description, flat JSON schema, and handler.
 * Descriptions stay terse on purpose; the driving discipline (ref lifecycle,
 * when to snapshot vs screenshot, coordinate space, staleness, console/network
 * usage, batch encoding) lives in the server-level `instructions` string.
 */

import type { Session } from "../session.js";
import {
  statusSchema,
  waitSchema,
  navigateSchema,
  snapshotSchema,
  actSchema,
  screenshotSchema,
  findSchema,
  readTextSchema,
  evalSchema,
  consoleSchema,
  networkSchema,
  tabsSchema,
  resizeSchema,
  uploadSchema,
  batchSchema,
  type JsonSchema,
} from "../schemas.js";
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
import { runUpload, type UploadInput } from "./upload.js";
import { runBatch, type BatchInput, type BatchTool } from "./batch.js";
import { jsonResult, type McpToolResult } from "./result.js";
import { runWait, type WaitInput } from "./wait.js";

export interface ToolDefinition {
  name: string;
  description: string;
  inputSchema: JsonSchema;
  run: (session: Session, input: Record<string, unknown>) => Promise<McpToolResult>;
}

export const TOOL_DEFINITIONS: ToolDefinition[] = [
  { name: "browser_status", description: "Current shared browser tabs, viewport, busy state and agent/human control. Available during human control; never creates a page.", inputSchema: statusSchema,
    run: async session => jsonResult(session.workspaceStatus?.() ?? { available: true, control: { owner: "agent", viewer_id: null, pending_viewer_id: null }, busy: session.busy, ...(await session.workspaceMetadata()) }) },
  { name: "browser_wait", description: "Bounded wait for URL, text, or ref/selector element state (visible, hidden, attached, detached). Default 5s, maximum 15s; interrupted by human takeover.", inputSchema: waitSchema,
    run: (session, input) => runWait(session, input as unknown as WaitInput) },
  {
    name: "browser_navigate",
    description:
      "Open a URL (bare domains get https://) or go back/forward. Acquires the page on first use and returns the landing page's interactive snapshot.",
    inputSchema: navigateSchema,
    run: (session, input) => runNavigate(session, input as { url: string }),
  },
  {
    name: "browser_snapshot",
    description:
      "Return the page's accessibility tree as refs you can act on. Use filter=interactive to shrink it, scope_ref to zoom into a subtree.",
    inputSchema: snapshotSchema,
    run: (session, input) =>
      runSnapshot(
        session,
        input as { filter?: "interactive" | "all"; scope_ref?: string; max_chars?: number },
      ),
  },
  {
    name: "browser_act",
    description:
      "Perform one action (click/type/fill/key/hover/scroll/select/drag/dblclick) on a ref or coordinate. Returns a terse change summary.",
    inputSchema: actSchema,
    run: (session, input) => runAct(session, input as unknown as ActInput),
  },
  {
    name: "browser_screenshot",
    description:
      "Capture the viewport (or a region / full page). Downscaled JPEG; the returned pixel dimensions define the coordinate space for browser_act.",
    inputSchema: screenshotSchema,
    run: (session, input) => runScreenshot(session, input as ScreenshotInput),
  },
  {
    name: "browser_find",
    description:
      "Find elements by a plain-language query without pulling the whole tree into context. Returns up to 20 act-able refs; falls back to text matching if the model sub-call is unavailable.",
    inputSchema: findSchema,
    run: (session, input) => runFind(session, input as { query: string }),
  },
  {
    name: "browser_read_text",
    description:
      "Reader-mode text of the current page: main article/content region (falls back to body). Returns title, url, and text (capped ~30k chars).",
    inputSchema: readTextSchema,
    run: (session) => runReadText(session),
  },
  {
    name: "browser_eval",
    description:
      "Run JavaScript in the page (top-level await allowed; last expression is returned). Result is JSON-ish, size-capped; errors return message + top stack frame.",
    inputSchema: evalSchema,
    run: (session, input) => runEval(session, input as { code: string }),
  },
  {
    name: "browser_console",
    description:
      "Read buffered console messages. `pattern` is REQUIRED (substring/regex). only_errors, limit (default 20, newest last), clear.",
    inputSchema: consoleSchema,
    run: (session, input) => runConsole(session, input as unknown as ConsoleInput),
  },
  {
    name: "browser_network",
    description:
      "Two-phase: without request_id, list recorded requests (filter by url_pattern); with request_id, return that response's text body (capped ~20k).",
    inputSchema: networkSchema,
    run: (session, input) => runNetwork(session, input as NetworkInput),
  },
  {
    name: "browser_tabs",
    description:
      "Manage tabs: op=list|create|close|select. Every other tool acts on the current tab; select moves that pointer. close rejects the last tab.",
    inputSchema: tabsSchema,
    run: (session, input) => runTabs(session, input as unknown as TabsInput),
  },
  {
    name: "browser_resize",
    description:
      "Set the viewport: preset=mobile|tablet|desktop, or explicit width+height (overrides preset).",
    inputSchema: resizeSchema,
    run: (session, input) => runResize(session, input as ResizeInput),
  },
  {
    name: "browser_upload",
    description:
      "Set files on a file input (by ref). paths must be under BROWSER_UPLOAD_DIR; 10MB/file cap.",
    inputSchema: uploadSchema,
    run: (session, input) => runUpload(session, input as unknown as UploadInput),
  },
  {
    name: "browser_batch",
    description:
      'Run several tools in order, stop on first error. actions is an array of JSON strings {"tool":"...","input":{...}}. No nested batch; max 20.',
    inputSchema: batchSchema,
    run: (session, input) =>
      runBatch(session, input as unknown as BatchInput, (name) => {
        const def = TOOL_MAP.get(name);
        if (!def) return undefined;
        return { inputSchema: def.inputSchema, run: def.run } satisfies BatchTool;
      }),
  },
];

export const TOOL_MAP: Map<string, ToolDefinition> = new Map(
  TOOL_DEFINITIONS.map((t) => [t.name, t]),
);
