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
import { runAct, isDialogAnswer, type ActInput } from "./act.js";
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
import { jsonResult, errorResult, type McpToolResult } from "./result.js";
import { runWait, type WaitInput } from "./wait.js";

export interface ToolDefinition {
  name: string;
  description: string;
  inputSchema: JsonSchema;
  run: (session: Session, input: Record<string, unknown>) => Promise<McpToolResult>;
}

const HANDLERS: Record<BrowserToolName, ToolDefinition["run"]> = {
  browser_status: async (session, input) => jsonResult({
    ...(session.workspaceStatus?.() ?? { available: true, control: { owner: "agent", viewer_id: null, pending_viewer_id: null }, busy: session.busy, ...(await session.workspaceMetadata()) }),
    ...runtimeStatus(session),
    uploads: uploadStatus(input.upload_paths),
  }),
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
  browser_read_text: (session, input) => runReadText(session, input as { offset?: number }),
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

/**
 * What browser_status adds about this runtime: whether a viewer can share the
 * browser, whether browser_find has its model, and anything waiting on the
 * agent (a dialog) or produced for it (downloads). Only true things.
 */
function runtimeStatus(session: Session): Record<string, unknown> {
  const dialog = session.describeDialog();
  const downloads = session.downloads();
  return {
    shared_panel: session.workspaceStatus !== undefined,
    semantic_find: Boolean(process.env.ANTHROPIC_API_KEY || process.env.ANTHROPIC_AUTH_TOKEN),
    ...(dialog ? { pending_dialog: dialog } : {}),
    ...(downloads.length ? { downloads } : {}),
  };
}

/**
 * Every tool but browser_status runs behind the dialog gate. While a
 * confirm/prompt/beforeunload dialog waits for an answer the page is blocked,
 * so other page work would only hang: it is refused up front (nothing runs)
 * until browser_act answers the dialog. A dialog that opens DURING a call
 * pauses that call; the caller hears about the dialog immediately and the
 * answer returns the paused call's own result.
 */
function withDialogGate(name: BrowserToolName, run: ToolDefinition["run"]): ToolDefinition["run"] {
  if (name === "browser_status") return run;
  return async (session, input) => {
    if (name === "browser_act" && isDialogAnswer(input)) return run(session, input);
    const pending = session.pendingDialog;
    if (pending && !(name === "browser_tabs" && input.op === "list")) {
      return errorResult(
        `dialog_open: a ${pending.type} dialog is waiting for an answer. Call browser_act kind=accept_dialog ` +
          "(value = text for a prompt) or kind=dismiss_dialog first.",
        { outcome: "not_executed", dialog: session.describeDialog(pending) },
      );
    }
    // A batch's steps each pass through this gate themselves.
    if (name === "browser_batch") return run(session, input);
    const raced = await session.raceDialog(run(session, input));
    if (!raced.paused) return raced.value;
    return jsonResult({
      ok: true,
      paused_by_dialog: true,
      dialog: session.describeDialog(raced.dialog),
      note: `${name} ran and the page opened this dialog. Answer it with browser_act kind=accept_dialog or dismiss_dialog; that answer returns the rest of this call's result.`,
    });
  };
}

export const TOOL_DEFINITIONS: ToolDefinition[] = TOOL_CATALOG.map(tool => ({
  ...tool, run: withDialogGate(tool.name, HANDLERS[tool.name]),
}));

export const TOOL_MAP: Map<string, ToolDefinition> = new Map(
  TOOL_DEFINITIONS.map((t) => [t.name, t]),
);
