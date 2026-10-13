import type { Session } from "../session.js";
import { raceWithTimeout } from "../session.js";
import { jsonResult, errorResult, type McpToolResult } from "./result.js";

export interface WaitInput {
  condition: "url" | "text" | "element";
  url?: string; text?: string; ref?: string; selector?: string;
  state?: "visible" | "hidden" | "attached" | "detached";
  match?: "contains" | "exact"; timeout_ms?: number;
}
export async function runWait(session: Session, input: WaitInput): Promise<McpToolResult> {
  const timeout = input.timeout_ms ?? 5000;
  if (!Number.isInteger(timeout) || timeout < 0 || timeout > 15000) return errorResult("timeout_ms must be an integer from 0 to 15000");
  const targets = [input.url, input.text, input.ref, input.selector].filter(v => v !== undefined);
  if (targets.length !== 1 || !targets[0] || targets[0].length > 8192) return errorResult("provide exactly one nonempty url, text, ref, or selector");
  if ((input.condition === "url" && !input.url) || (input.condition === "text" && !input.text) ||
      (input.condition === "element" && !input.ref && !input.selector) || !["url", "text", "element"].includes(input.condition)) return errorResult("condition does not match wait target");
  if (input.condition === "url" && input.state !== undefined) return errorResult("state applies only to text/element waits");
  if (input.condition === "element" && input.match !== undefined) return errorResult("match applies only to url/text waits");
  const page = await session.ensurePage();
  const state = input.state ?? "visible";
  const started = Date.now();
  const deadline = started + timeout;
  while (true) {
    session.agentAdmission?.(); // a pending takeover interrupts a long wait, not the queue
    const check = async () => {
      if (input.condition === "url") return input.match === "exact" ? page.url() === input.url : page.url().includes(input.url!);
      const locator = input.ref ? await session.resolveRef(input.ref) : input.selector ? page.locator(input.selector).first() : page.getByText(input.text!, { exact: input.match === "exact" }).first();
      if (state === "attached" || state === "detached") return (await locator.count() > 0) === (state === "attached");
      return (await locator.isVisible()) === (state === "visible");
    };
    let matched = false;
    try { matched = await raceWithTimeout(check(), Math.max(1, deadline - Date.now()), "browser_wait timed out"); }
    catch (err) {
      if (!(err instanceof Error) || err.message !== "browser_wait timed out") throw err;
    }
    const evidence = {
      condition: input.condition,
      target: targets[0],
      state: input.condition === "url" ? undefined : state,
      match: input.condition === "element" ? undefined : input.match ?? "contains",
      tab_id: session.currentTabId() ?? null,
      url: page.url(),
      elapsed_ms: Date.now() - started,
    };
    if (matched) return jsonResult({ ...evidence, ok: true, matched: true });
    if (Date.now() >= deadline) {
      return jsonResult({ ...evidence, ok: false, matched: false, timed_out: true });
    }
    await new Promise(resolve => setTimeout(resolve, Math.min(100, deadline - Date.now())));
  }
}
