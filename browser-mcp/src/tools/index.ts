/**
 * Tool registry: name → (1-2 line) description, flat JSON schema, and handler.
 * Descriptions stay terse on purpose; the driving discipline (ref lifecycle,
 * when to snapshot vs screenshot, coordinate space, staleness) lives in the
 * server-level `instructions` string, not here.
 */

import type { Session } from "../session.js";
import {
  navigateSchema,
  snapshotSchema,
  actSchema,
  screenshotSchema,
  type JsonSchema,
} from "../schemas.js";
import { runNavigate } from "./navigate.js";
import { runSnapshot } from "./snapshot.js";
import { runAct, type ActInput } from "./act.js";
import { runScreenshot, type ScreenshotInput } from "./screenshot.js";
import type { McpToolResult } from "./result.js";

export interface ToolDefinition {
  name: string;
  description: string;
  inputSchema: JsonSchema;
  run: (session: Session, input: Record<string, unknown>) => Promise<McpToolResult>;
}

export const TOOL_DEFINITIONS: ToolDefinition[] = [
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
];

export const TOOL_MAP: Map<string, ToolDefinition> = new Map(
  TOOL_DEFINITIONS.map((t) => [t.name, t]),
);
