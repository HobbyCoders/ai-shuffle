/**
 * Round-9 audit, item 8a: the browser tool contract stated up front, and
 * browser_act refreshing a stale screenshot instead of only erroring.
 *
 * Twelve contract errors across one audit set: stale_ref after navigation,
 * stale_screenshot after a prior act, "exactly one of ref or coordinate" on
 * scroll, condition="selector" on browser_wait. None of those rules were in
 * the tool descriptions the model reads first.
 */
import { describe, it, expect } from "vitest";
import { TOOL_MAP } from "../src/tools/index.js";
import { actSchema, waitSchema } from "../src/schemas.js";
import { SERVER_INSTRUCTIONS } from "../src/instructions.js";
import { runAct } from "../src/tools/act.js";
import type { Session } from "../src/session.js";
import type { McpToolResult } from "../src/tools/result.js";

function desc(name: string): string {
  const def = TOOL_MAP.get(name);
  if (!def) throw new Error(`missing tool ${name}`);
  return def.description;
}

describe("tool descriptions state the contract", () => {
  it("browser_snapshot: refs expire on navigation and major DOM change; scope after the first snapshot", () => {
    const d = desc("browser_snapshot");
    expect(d).toMatch(/Refs expire on every navigation/);
    expect(d).toMatch(/major DOM change/);
    expect(d).toMatch(/filter=interactive or scope_ref/);
  });
  it("browser_act: exactly one target (scroll included), screenshot freshness, auto-refresh", () => {
    const d = desc("browser_act");
    expect(d).toMatch(/exactly one of ref or coordinate — scroll included/);
    expect(d).toMatch(/taken since the previous action or navigation/);
    expect(d).toMatch(/re-captures the viewport/);
    expect(actSchema.properties.ref.description).toMatch(/refs expire on navigation/);
    expect(actSchema.properties.direction.description).toMatch(/exactly one of ref or coordinate/);
  });
  it("browser_wait: the condition enum is the whole list", () => {
    const d = desc("browser_wait");
    expect(d).toMatch(/exactly one of url \| text \| element/);
    expect(d).toMatch(/no selector condition/);
    expect(waitSchema.properties.condition.enum).toEqual(["url", "text", "element"]);
    expect(waitSchema.properties.condition.description).toMatch(/no selector condition/);
  });
  it("browser_screenshot: invalidated by any action; layout only", () => {
    expect(desc("browser_screenshot")).toMatch(/Any action, navigation, tab or viewport change invalidates it/);
  });
  it("explains viewer handoff and unsupported capture without granting permissions", () => {
    expect(SERVER_INSTRUCTIONS).toContain("choose Return control in the Browser panel and wait for Agent control");
    expect(SERVER_INSTRUCTIONS).toContain("A chat reply alone does not release browser ownership");
    expect(SERVER_INSTRUCTIONS).toContain("Microphone capture is unsupported");
    expect(SERVER_INSTRUCTIONS).toContain("normal browser and approve microphone access there");
    expect(SERVER_INSTRUCTIONS).toContain("browser-host worker, not necessarily the user's device");
    expect(SERVER_INSTRUCTIONS).toContain("Do not auto-grant permissions");
  });
  it("server instructions open with the two rules", () => {
    const head = SERVER_INSTRUCTIONS.slice(0, 1400);
    expect(head).toMatch(/TWO RULES BEFORE ANYTHING ELSE/);
    expect(head).toMatch(/Refs \(e12\) die on every navigation/);
    expect(head).toMatch(/Coordinates need a browser_screenshot taken AFTER/);
    expect(head).toMatch(/scroll takes exactly one of ref or coordinate/);
    expect(head).toMatch(/scope_ref=eN/);
  });
});

function texts(res: McpToolResult): string[] {
  return res.content.filter((c) => c.type === "text").map((c) => (c as { text: string }).text);
}

/** A session whose stale screenshot can or cannot be re-validated. */
function staleSession(revalidates: boolean, calls: string[]): Session {
  return {
    ensurePage: async () => ({ url: () => "http://app.test/", mouse: { click: async () => { calls.push("click"); } } }),
    bringToFront: async () => {},
    hasValidScreenshot: () => false,
    tryRevalidateViewportScreenshot: async () => { calls.push("revalidate"); return revalidates; },
    toViewportPoint: async (c: [number, number]) => c,
    invalidateScreenshot: () => {},
    beginWatch: async () => {},
    disposeWatch: async () => {},
    settle: async () => ({ urlChanged: false, domMutated: "none" }),
    resolveRef: () => { throw new Error("no refs here"); },
  } as unknown as Session;
}

describe("browser_act on a stale screenshot", () => {
  it("re-validates an unchanged viewport and proceeds, saying so", async () => {
    const calls: string[] = [];
    const res = await runAct(staleSession(true, calls), { kind: "click", coordinate: [10, 10] });
    expect(res.isError).toBeFalsy();
    expect(calls).toEqual(["revalidate", "click"]);
    const body = JSON.parse(texts(res)[0]) as Record<string, unknown>;
    expect(body.ok).toBe(true);
    expect(body.screenshot_refreshed).toBe(true);
  });
  it("otherwise answers stale_screenshot with a fresh capture attached instead of a bare error", async () => {
    const calls: string[] = [];
    const session = staleSession(false, calls);
    // The fresh capture comes from runScreenshot; stub the page it needs.
    (session as unknown as { ensurePage: () => Promise<unknown> }).ensurePage = async () => ({
      url: () => "http://app.test/",
      evaluate: async () => ({ scrollW: 800, scrollH: 600, innerW: 800, innerH: 600, scrollX: 0, scrollY: 0 }),
      screenshot: async () => Buffer.from("jpeg-bytes"),
    });
    (session as unknown as { setLastScreenshot: (m: unknown) => void }).setLastScreenshot = () => {};
    const res = await runAct(session, { kind: "click", coordinate: [10, 10] });
    expect(res.isError).toBe(true);
    expect(calls).toEqual(["revalidate"]);
    expect(texts(res)[0]).toMatch(/^stale_screenshot:/);
    expect(texts(res)[0]).toMatch(/fresh viewport screenshot is attached/);
    expect(res.content.some((c) => c.type === "image")).toBe(true);
  });
});
