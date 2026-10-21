import { observationRoots, includeObservationRoot, observationTask, boundedObservation } from "./observation_scope.js";
import type { Frame } from "playwright-core";
import type { Session } from "../session.js";
import { raceWithTimeout } from "../session.js";
import { jsonResult, errorResult, type McpToolResult } from "./result.js";

export interface WaitInput {
  condition: "url" | "text" | "element";
  url?: string; text?: string; ref?: string; selector?: string;
  state?: "visible" | "hidden" | "attached" | "detached";
  match?: "contains" | "exact"; timeout_ms?: number; scope_ref?: string;
}
export async function runWait(session: Session, input: WaitInput): Promise<McpToolResult> {
  const timeout = input.timeout_ms ?? 5000;
  if (!Number.isInteger(timeout) || timeout < 0 || timeout > 15000) return errorResult("timeout_ms must be an integer from 0 to 15000");
  const targets = [input.url, input.text, input.ref, input.selector].filter(v => v !== undefined);
  if (targets.length !== 1 || !targets[0] || targets[0].length > 8192) return errorResult("provide exactly one nonempty url, text, ref, or selector");
  if ((input.condition === "url" && !input.url) || (input.condition === "text" && !input.text) ||
      (input.condition === "element" && !input.ref && !input.selector) || !["url", "text", "element"].includes(input.condition)) return errorResult("condition does not match wait target");
  if (input.scope_ref && (input.condition === "url" || input.ref)) return errorResult("scope_ref applies only to text/selector waits");
  if (input.condition === "url" && input.state !== undefined) return errorResult("state applies only to text/element waits");
  if (input.condition === "element" && input.match !== undefined) return errorResult("match applies only to url/text waits");
  const page = await session.ensurePage();
  const state = input.state ?? "visible";
  const started = Date.now();
  const deadline = started + timeout;
  const negative = state === "hidden" || state === "detached";
  // Retain unresolved checks across polls: a timeout means unknown, not absent.
  const pending = new Map<Frame, Promise<boolean>>();
  const key = `wait:${JSON.stringify(input)}`;
  const selectorError = (err: unknown) => input.selector && /while parsing|InvalidSelector|not a valid (?:selector|XPath)|Unexpected token|Unknown engine|Malformed selector|SyntaxError/i.test(String(err));
  while (true) {
    session.agentAdmission?.(); // a pending takeover interrupts a long wait, not the queue
    const check = async () => {
      if (input.condition === "url") return input.match === "exact" ? page.url() === input.url : page.url().includes(input.url!);
      const present = async (locator: import("playwright-core").Locator) =>
        state === "attached" || state === "detached" ? await locator.count() > 0
          : await locator.filter({ visible: true }).count() > 0;
      if (input.ref) {
        const any = await present(await session.resolveRef(input.ref));
        return negative ? !any : any;
      }
      const roots = await observationRoots(session, input.scope_ref);
      const inspect = async (root: typeof roots[number]): Promise<boolean | undefined> => {
        let task = pending.get(root.frame);
        if (!task) {
          task = observationTask(session, root, key, async () => {
            if (root.primary && root.frame.isDetached()) throw new Error("primary document detached");
            if (!await includeObservationRoot(root)) return false;
            const scope = root.primary && root.scope ? root.scope : root.frame;
            const locator = input.selector ? scope.locator(input.selector)
              : scope.getByText(input.text!, { exact: input.match === "exact" });
            return present(locator);
          });
          if (task) pending.set(root.frame, task);
        }
        try {
          const result = await boundedObservation(task, Math.min(root.primary ? 15000 : 500, deadline - Date.now()));
          pending.delete(root.frame);
          return result;
        } catch (err) {
          if (selectorError(err)) throw new Error(`invalid_input: ${String(err)}`);
          if (!(err instanceof Error) || err.message !== "frame observation timed out") pending.delete(root.frame);
          // Detached child documents can no longer contain the target. Every
          // other error/timeout is unknown; retry failures, retain pending work.
          return !root.primary && root.frame.isDetached() ? false : undefined;
        }
      };
      const primary = await inspect(roots[0]);
      if (primary === true) return !negative;
      let any = false;
      let unknown = primary === undefined;
      let next = 1;
      await Promise.all(Array.from({ length: Math.min(6, roots.length - 1) }, async () => {
        while (next < roots.length) {
          if (any || Date.now() >= deadline) { unknown = true; break; }
          const result = await inspect(roots[next++]);
          any ||= result === true;
          unknown ||= result === undefined;
        }
      }));
      return negative ? !any && !unknown : any;
    };
    let matched = false;
    try { matched = await raceWithTimeout(check(), Math.max(1, deadline - Date.now()), "browser_wait timed out"); }
    catch (err) {
      if (err instanceof Error && err.message.startsWith("invalid_input:")) return errorResult(err.message);
      if (selectorError(err)) return errorResult(`invalid_input: ${String(err)}`);
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
