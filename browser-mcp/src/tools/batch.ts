/**
 * browser_batch — run several other tools in one call, sequentially, stopping at
 * the first error. Atomic w.r.t. other sessions' calls because the dispatcher
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
  // — firstText() alone would silently drop them and the model would never see
  // a screenshot taken inside a batch.
  const images: McpContent[] = [];
  let allOk = true;

  for (const raw of actions) {
    const { action, error } = parseBatchAction(raw);
    if (error || !action) {
      results.push({ tool: "?", ok: false, error: error ?? "parse error" });
      allOk = false;
      break;
    }
    const tool = lookup(action.tool);
    if (!tool) {
      results.push({ tool: action.tool, ok: false, error: `unknown tool: ${action.tool}` });
      allOk = false;
      break;
    }
    const normalizedInput = normalizeOptionalInput(tool.inputSchema, action.input);
    const valid = validateInput(tool.inputSchema, normalizedInput);
    if (!valid.ok) {
      results.push({ tool: action.tool, ok: false, error: valid.error ?? "invalid input" });
      allOk = false;
      break;
    }

    let res: McpToolResult;
    try {
      session.agentAdmission?.();
      // Direct call — the batch itself already holds the runExclusive turn.
      res = await tool.run(session, normalizedInput);
    } catch (err) {
      results.push({
        tool: action.tool,
        ok: false,
        error: err instanceof Error ? err.message : String(err),
      });
      allOk = false;
      break;
    }

    const summary = firstText(res).slice(0, SUMMARY_MAX_CHARS);
    if (res.isError) {
      results.push({ tool: action.tool, ok: false, error: summary || "tool error" });
      allOk = false;
      break;
    }
    results.push({ tool: action.tool, ok: true, summary });
    for (const c of res.content) {
      if (c.type === "image") images.push(c);
    }
  }

  const out = jsonResult({ ok: allOk, count: results.length, results });
  out.content.push(...images);
  return out;
}
