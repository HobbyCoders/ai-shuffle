/**
 * browser_eval — run JavaScript in the page with REPL semantics: top-level
 * await is allowed. A single bare expression has its value returned
 * automatically; multi-statement code must `return` the value explicitly (only
 * then does anything come back). The result is serialized to a JSON-ish string
 * with depth/size/total-work caps; non-serializable values (functions, DOM
 * nodes, symbols, …) are described by type rather than dropped. Thrown errors
 * return { ok:false, error, stack } with only the top stack frame — never the
 * whole page state.
 */

import type { Session } from "../session.js";
import { jsonResult, errorResult, capText, type McpToolResult } from "./result.js";

/** Serialized-value size cap (chars) before a truncation note is appended. */
export const EVAL_MAX_CHARS = 10000;
/**
 * Total-work budget for the page-side describer. The per-level (100) and depth
 * (6) caps still permit ~100^6 nodes on a pathological object before the final
 * capText; these shared running budgets bail the walk early so it cannot pin
 * the (serialized) session for long. Whichever trips first wins.
 */
export const EVAL_NODE_BUDGET = 5000;
export const EVAL_CHAR_BUDGET = 20000;

interface EvalOutcome {
  ok: boolean;
  type?: string;
  value?: string;
  error?: string;
  stack?: string;
}

/**
 * Page-side REPL. Self-contained (injected via page.evaluate). Wraps the code in
 * an async function, preferring expression form (so a bare expression's value is
 * returned) and falling back to statement form when that is a syntax error.
 * Serializes the result with a bounded, cycle-safe describer.
 */
async function evalInPage(code: string): Promise<EvalOutcome> {
  const MAX_DEPTH = 6;
  const MAX_ITEMS = 100;
  // Shared running budgets (mirror EVAL_NODE_BUDGET / EVAL_CHAR_BUDGET — kept
  // literal here because this function is injected into the page verbatim and
  // cannot close over module scope). Whichever trips first ends the walk.
  const NODE_BUDGET = 5000;
  const CHAR_BUDGET = 20000;
  const BUDGET_MARKER = '"…(truncated: output budget exceeded)"';
  let nodesVisited = 0;
  let charsUsed = 0;
  let budgetExceeded = false;

  const seen = new WeakSet<object>();
  /** Account for a leaf's serialized chars as we return it. */
  function emit(s: string): string {
    charsUsed += s.length;
    return s;
  }
  function describe(v: unknown, depth: number): string {
    nodesVisited += 1;
    if (budgetExceeded || nodesVisited > NODE_BUDGET || charsUsed > CHAR_BUDGET) {
      budgetExceeded = true;
      return BUDGET_MARKER;
    }
    if (v === null) return "null";
    const t = typeof v;
    if (t === "undefined") return "undefined";
    if (t === "number" || t === "boolean") return emit(String(v));
    if (t === "bigint") return emit(`${String(v)}n`);
    if (t === "string") return emit(JSON.stringify(v));
    if (t === "symbol") return emit((v as symbol).toString());
    if (t === "function") {
      const name = (v as { name?: string }).name;
      return emit(`[Function: ${name || "anonymous"}]`);
    }
    // objects
    const obj = v as object;
    if (v instanceof Error) {
      return emit(JSON.stringify({ name: v.name, message: v.message }));
    }
    if (typeof Node !== "undefined" && v instanceof Node) {
      const node = v as unknown as { nodeName?: string; id?: string };
      const id = node.id ? `#${node.id}` : "";
      return emit(`[${node.nodeName || "Node"}${id}]`);
    }
    if (seen.has(obj)) return '"[Circular]"';
    if (depth <= 0) return Array.isArray(v) ? '"[Array]"' : '"[Object]"';
    seen.add(obj);
    try {
      if (Array.isArray(v)) {
        const parts = v.slice(0, MAX_ITEMS).map((x) => describe(x, depth - 1));
        if (v.length > MAX_ITEMS) parts.push(`"… ${v.length - MAX_ITEMS} more"`);
        return `[${parts.join(",")}]`;
      }
      const keys = Object.keys(obj).slice(0, MAX_ITEMS);
      const parts = keys.map(
        (k) => `${JSON.stringify(k)}:${describe((obj as Record<string, unknown>)[k], depth - 1)}`,
      );
      if (Object.keys(obj).length > MAX_ITEMS) parts.push('"…":"more"');
      return `{${parts.join(",")}}`;
    } finally {
      seen.delete(obj);
    }
  }

  const AsyncFunction = Object.getPrototypeOf(async function () {
    /* noop */
  }).constructor as new (body: string) => () => Promise<unknown>;

  let fn: () => Promise<unknown>;
  try {
    // Expression form: `return ( <code> )` returns a bare expression's value.
    fn = new AsyncFunction(`return (\n${code}\n);`);
  } catch {
    // Multi-statement code (or a statement that is a syntax error as an
    // expression): run as a function body. Value comes from an explicit return.
    try {
      fn = new AsyncFunction(code);
    } catch (compileErr) {
      const e = compileErr as Error;
      return { ok: false, error: e.message, stack: "" };
    }
  }

  try {
    const result = await fn();
    return { ok: true, type: result === null ? "null" : typeof result, value: describe(result, MAX_DEPTH) };
  } catch (runErr) {
    const e = runErr as Error;
    const stackTop = typeof e.stack === "string" ? e.stack.split("\n").slice(0, 2).join("\n") : "";
    return { ok: false, error: e && e.message ? e.message : String(e), stack: stackTop };
  }
}

export async function runEval(session: Session, input: { code: string }): Promise<McpToolResult> {
  await session.ensurePage();

  let outcome: EvalOutcome;
  try {
    outcome = await session.evaluate(evalInPage, input.code);
  } catch (err) {
    // Evaluation could not even complete (page closed, navigation, CSP, …).
    const e = err as Error;
    const stackTop = typeof e.stack === "string" ? e.stack.split("\n").slice(0, 2).join("\n") : "";
    return errorResult(e && e.message ? e.message : String(err), { stack: stackTop });
  }

  if (!outcome.ok) {
    return errorResult(outcome.error ?? "eval failed", { stack: outcome.stack ?? "" });
  }

  const { text, truncated, fullChars } = capText(outcome.value ?? "undefined", EVAL_MAX_CHARS);
  const body: Record<string, unknown> = {
    ok: true,
    type: outcome.type,
    value: text,
    truncated,
  };
  if (truncated) body.full_chars = fullChars;
  return jsonResult(body);
}
