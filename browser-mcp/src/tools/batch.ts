/**
 * browser_batch: run several other tools in one call, sequentially. It stops at
 * the first failed step (an error, or a body that says ok:false), and after a
 * step that changed the page it runs on only through observation steps, since
 * later actions were planned against a page that no longer exists. The result
 * says which steps ran, how the failed one ended (outcome) and how many did
 * not run. Atomic w.r.t. other sessions' calls because the dispatcher
 * runs it inside a single runExclusive turn; the inner tools are therefore
 * invoked directly (NOT re-entering runExclusive, which would deadlock).
 *
 * Encoding: to keep the schema floor intact (array-of-primitives only), each
 * action is a JSON STRING of `{"tool":"browser_...","input":{...}}`. We parse
 * and validate each server-side with the same validator the dispatcher uses. A
 * batch may not contain another batch, and is capped at {@link BATCH_MAX_ACTIONS}.
 */

import type { Session } from "../session.js";
import { jsonResult, errorResult, type McpToolResult, type McpContent } from "./result.js";
import { normalizeOptionalInput, validateInput, type JsonSchema } from "../schemas.js";

export const BATCH_MAX_ACTIONS = 20;
/** Chars of each item's result text surfaced in the batch summary. */
const SUMMARY_MAX_CHARS = 400;

export interface BatchInput {
  actions: string[];
}

export interface ParsedAction {
  tool: string;
  input: Record<string, unknown>;
}

export interface BatchItemResult {
  tool: string;
  ok: boolean;
  summary?: string;
  error?: string;
  outcome?: string;
}

/**
 * Tools that only look. After a step that changed the page (navigated, opened
 * a tab or a dialog), the rest of the batch was planned against a page that is
 * gone, so the batch runs on only while the next steps are observations.
 */
const OBSERVE_TOOLS: ReadonlySet<string> = new Set([
  "browser_status", "browser_snapshot", "browser_screenshot", "browser_find",
  "browser_read_text", "browser_console", "browser_network", "browser_wait",
]);

function isObservation(action: ParsedAction): boolean {
  if (OBSERVE_TOOLS.has(action.tool)) return true;
  return action.tool === "browser_tabs" && action.input.op === "list";
}

/** Did this step leave the page different from what later steps were planned against? */
function changedPage(tool: string, input: Record<string, unknown>, body: Record<string, unknown> | undefined): boolean {
  if (tool === "browser_navigate" || tool === "browser_resize") return true;
  if (tool === "browser_tabs" && input.op !== "list") return true;
  if (!body) return false;
  return body.url_changed === true || body.paused_by_dialog === true || Array.isArray(body.events) && body.events.some(
    (e) => typeof e === "object" && e !== null && ((e as Record<string, unknown>).type === "new_tab" || (e as Record<string, unknown>).handled === "pending"),
  );
}

function parseBody(raw: string): Record<string, unknown> | undefined {
  try {
    const parsed: unknown = JSON.parse(raw);
    return typeof parsed === "object" && parsed !== null && !Array.isArray(parsed) ? parsed as Record<string, unknown> : undefined;
  } catch {
    return undefined;
  }
}

/** A tool the batch can dispatch to (subset of the full ToolDefinition). */
export interface BatchTool {
  inputSchema: JsonSchema;
  run: (session: Session, input: Record<string, unknown>) => Promise<McpToolResult>;
}

/**
 * Parse one action string into { tool, input }. Enforces: valid JSON, object
 * shape, string `tool`, object `input` (defaulting to {}), and the no-nested-
 * batch rule. Does NOT validate against the tool's schema (the caller does that
 * once it has resolved the tool).
 */
export function parseBatchAction(raw: unknown): { action?: ParsedAction; error?: string } {
  if (typeof raw !== "string") return { error: "each action must be a JSON string" };
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return { error: `action is not valid JSON: ${raw.slice(0, 80)}` };
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    return { error: "each action must decode to an object" };
  }
  const obj = parsed as Record<string, unknown>;
  if (typeof obj.tool !== "string") {
    return { error: "action.tool must be a string" };
  }
  if (obj.tool === "browser_batch") {
    return { error: "browser_batch cannot be nested" };
  }
  let input: Record<string, unknown> = {};
  if (obj.input !== undefined) {
    if (typeof obj.input !== "object" || obj.input === null || Array.isArray(obj.input)) {
      return { error: "action.input must be an object" };
    }
    input = obj.input as Record<string, unknown>;
  }
  return { action: { tool: obj.tool, input } };
}

function firstText(result: McpToolResult): string {
  const t = result.content.find((c: McpContent) => c.type === "text");
  return t && t.type === "text" ? t.text : "";
}

/**
 * Execute a batch. `lookup` resolves a tool name to its schema + handler (wired
 * by tools/index.ts to avoid an import cycle). Stops at the first failing item;
 * every attempted item appears in `results`.
 */
export async function runBatch(
  session: Session,
  input: BatchInput,
  lookup: (name: string) => BatchTool | undefined,
): Promise<McpToolResult> {
  const actions = input.actions;
  if (actions.length === 0) {
    return errorResult("`actions` must not be empty");
  }
  if (actions.length > BATCH_MAX_ACTIONS) {
    return errorResult(`too many actions: ${actions.length} (max ${BATCH_MAX_ACTIONS})`);
  }

  const results: BatchItemResult[] = [];
  // Image blocks (screenshots) from sub-results, re-emitted on the batch result
  // so a screenshot taken inside a batch reaches the model.
  const images: McpContent[] = [];
  let allOk = true;
  let stopped: string | undefined;
  let changedAt = -1;

  for (const [index, raw] of actions.entries()) {
    const { action, error } = parseBatchAction(raw);
    if (error || !action) {
      results.push({ tool: "?", ok: false, error: `invalid_input: ${error ?? "parse error"}`, outcome: "not_executed" });
      allOk = false;
      break;
    }
    if (changedAt >= 0 && !isObservation(action)) {
      stopped = `step ${changedAt + 1} changed the page (navigation, new tab or dialog), so step ${index + 1} ` +
        `(${action.tool}) and later steps were not run. Observe the new page, then continue.`;
      break;
    }
    const tool = lookup(action.tool);
    if (!tool) {
      results.push({ tool: action.tool, ok: false, error: `invalid_input: unknown tool: ${action.tool}`, outcome: "not_executed" });
      allOk = false;
      break;
    }
    const normalizedInput = normalizeOptionalInput(tool.inputSchema, action.input);
    const valid = validateInput(tool.inputSchema, normalizedInput);
    if (!valid.ok) {
      results.push({ tool: action.tool, ok: false, error: `invalid_input: ${valid.error ?? "invalid input"}`, outcome: "not_executed" });
      allOk = false;
      break;
    }

    let res: McpToolResult;
    try {
      session.agentAdmission?.();
      // Direct call: the batch itself already holds the runExclusive turn.
      res = await tool.run(session, normalizedInput);
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      results.push({ tool: action.tool, ok: false, error: message, ...(message.startsWith("human_control") ? { outcome: "not_executed" } : {}) });
      allOk = false;
      break;
    }

    const first = firstText(res);
    const body = parseBody(first);
    const summary = first.slice(0, SUMMARY_MAX_CHARS);
    // A tool can answer without isError and still say it did not get there
    // (browser_wait timing out reports ok:false): that also ends the batch.
    if (res.isError || body?.ok === false) {
      const outcome = typeof body?.outcome === "string" ? body.outcome : undefined;
      const err = typeof body?.error === "string" ? body.error.slice(0, SUMMARY_MAX_CHARS) : summary || "tool error";
      results.push({ tool: action.tool, ok: false, error: err, ...(outcome ? { outcome } : {}) });
      for (const c of res.content) if (c.type === "image") images.push(c);
      allOk = false;
      break;
    }
    results.push({ tool: action.tool, ok: true, summary });
    for (const c of res.content) {
      if (c.type === "image") images.push(c);
    }
    if (body?.paused_by_dialog === true && index < actions.length - 1) {
      // Every later step would only be refused with dialog_open.
      stopped = `step ${index + 1} opened a dialog that waits for an answer, so later steps were not run. ` +
        "Answer it with browser_act kind=accept_dialog or dismiss_dialog.";
      break;
    }
    if (changedAt < 0 && changedPage(action.tool, normalizedInput, body)) changedAt = index;
  }

  const notRun = actions.length - results.length;
  const out = jsonResult({
    ok: allOk,
    count: results.length,
    not_run: notRun,
    ...(stopped ? { stopped } : {}),
    results,
  });
  out.content.push(...images);
  return out;
}