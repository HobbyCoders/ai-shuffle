import { describe, it, expect } from "vitest";
import { validateTarget, scrollDelta, type ActInput } from "../src/tools/act.js";
import { normalizeUrl } from "../src/tools/navigate.js";
import { runScreenshot, scaleFor, MAX_EDGE } from "../src/tools/screenshot.js";
import type { Session } from "../src/session.js";
import type { McpToolResult } from "../src/tools/result.js";

function firstText(res: McpToolResult): string {
  const t = res.content.find((c) => c.type === "text");
  return t && t.type === "text" ? t.text : "";
}
// A session whose ensurePage would throw — proves the check ran BEFORE it.
const noPageSession = {
  ensurePage: () => Promise.reject(new Error("should not reach ensurePage")),
} as unknown as Session;

describe("act.validateTarget", () => {
  const base = { kind: "click" } as ActInput;
  it("requires exactly one of ref or coordinate", () => {
    expect(validateTarget({ ...base })).toMatch(/exactly one/);
    expect(validateTarget({ ...base, ref: "e1", coordinate: [1, 2] })).toMatch(/exactly one/);
    expect(validateTarget({ ...base, ref: "e1" })).toBeNull();
    expect(validateTarget({ ...base, coordinate: [1, 2] })).toBeNull();
  });
  it("rejects malformed coordinates", () => {
    expect(validateTarget({ ...base, coordinate: [1] })).toMatch(/\[x, y\]/);
  });
});

describe("act.scrollDelta", () => {
  it("maps directions to wheel deltas", () => {
    expect(scrollDelta("down", 400)).toEqual([0, 400]);
    expect(scrollDelta("up", 400)).toEqual([0, -400]);
    expect(scrollDelta("right", 250)).toEqual([250, 0]);
    expect(scrollDelta("left", 250)).toEqual([-250, 0]);
  });
});

describe("navigate.normalizeUrl", () => {
  it("prepends https:// for bare domains", () => {
    expect(normalizeUrl("example.com")).toBe("https://example.com");
    expect(normalizeUrl("example.com/path?q=1")).toBe("https://example.com/path?q=1");
  });
  it("leaves explicit schemes untouched", () => {
    expect(normalizeUrl("http://foo.test")).toBe("http://foo.test");
    expect(normalizeUrl("https://foo.test")).toBe("https://foo.test");
    expect(normalizeUrl("about:blank")).toBe("about:blank");
    expect(normalizeUrl("data:text/html,hi")).toBe("data:text/html,hi");
  });
});

describe("screenshot.scaleFor", () => {
  it("returns 1 when within the max edge", () => {
    expect(scaleFor(800, 600)).toBe(1);
    expect(scaleFor(MAX_EDGE, 100)).toBe(1);
  });
  it("downscales by the longest edge", () => {
    expect(scaleFor(2560, 1440)).toBeCloseTo(MAX_EDGE / 2560);
    expect(scaleFor(1000, 4000)).toBeCloseTo(MAX_EDGE / 4000);
  });
});

describe("screenshot region validation (m6)", () => {
  it("rejects a region that is not [x,y,w,h]", async () => {
    const res = await runScreenshot(noPageSession, { region: [0, 0, 100] });
    expect(res.isError).toBe(true);
    expect(firstText(res)).toContain("[x, y, width, height]");
  });
  it("rejects region + full_page together", async () => {
    const res = await runScreenshot(noPageSession, { region: [0, 0, 100, 100], full_page: true });
    expect(res.isError).toBe(true);
    expect(firstText(res)).toContain("not both");
  });
  it("rejects non-positive width/height", async () => {
    const zero = await runScreenshot(noPageSession, { region: [0, 0, 0, 100] });
    expect(zero.isError).toBe(true);
    const neg = await runScreenshot(noPageSession, { region: [0, 0, 100, -5] });
    expect(neg.isError).toBe(true);
    expect(firstText(neg)).toContain("positive");
  });
});
