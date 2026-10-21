import { describe, it, expect, vi } from "vitest";
import { normalizeKey, normalizeModifiers, invalidInputRemedy } from "../src/tools/act.js";
import { Session } from "../src/session.js";
import { QUIET_CAP_MS } from "../src/dommutation.js";
import { actSchema, validateInput } from "../src/schemas.js";

describe("action input diagnostics", () => {
  it("exposes multi-select and flat drag paths without violating the schema floor", () => {
    expect(validateInput(actSchema, { kind: "select", ref: "e1", values: ["Ham", "Olives"] }).ok).toBe(true);
    expect(validateInput(actSchema, { kind: "drag", ref: "e1", to_ref: "e2", path: [10, 20, 30, 40], hold_ms: 50, drop_delay_ms: 100 }).ok).toBe(true);
    expect(validateInput(actSchema, { kind: "drag", ref: "e1", path: [[10, 20]] }).ok).toBe(false);
    expect(validateInput(actSchema, { kind: "select", ref: "e1", values: [12] }).ok).toBe(false);
    expect(validateInput(actSchema, { kind: "drag", ref: "e1", hold_ms: 5001 }).ok).toBe(false);
  });
  it.each([["Return", "Enter"], ["Esc", "Escape"], ["Ctrl+Return", "Control+Enter"], ["Cmd+Option+Del", "Meta+Alt+Delete"]])("normalizes %s", (input, expected) => {
    expect(normalizeKey(input)).toBe(expected);
  });
  it("normalizes and validates pointer modifiers", () => {
    expect(normalizeModifiers(["Ctrl", "Cmd", "Option", "Shift"])).toEqual(["Control", "Meta", "Alt", "Shift"]);
    expect(() => normalizeModifiers(["Something"])).toThrow(/invalid_input: unknown modifier/);
  });
  it.each([
    ['locator.press: Unknown key: "Unknown"\nwaiting for aria-ref=e1', /Playwright key/],
    ['locator.fill: Malformed value\nwaiting for aria-ref=e1', /YYYY-MM-DD/],
    ['locator.selectOption: Element is not a <select> element\naria-ref=e1', /combobox\/listbox/],
  ])("classifies input errors before ref call logs", (message, remedy) => {
    expect(invalidInputRemedy(message as string)).toMatch(/^invalid_input:/);
    expect(invalidInputRemedy(message as string)).toMatch(remedy as RegExp);
  });
  it("does not reclassify an overlay timeout", () => {
    expect(invalidInputRemedy("Timeout 2000ms exceeded; aria-ref=e1 intercepts pointer events")).toBeUndefined();
  });
});

describe("settle timeout", () => {
  it("allows the page cap plus margin and does not invent a navigation", async () => {
    vi.useFakeTimers();
    try {
      const notifyNavigation = vi.fn();
      const fake = {
        requirePage: () => ({ url: () => "https://example.test/", evaluate: () => new Promise(() => {}) }),
        disposeWatch: vi.fn(async () => {}), notifyNavigation,
      };
      const pending = Session.prototype.settle.call(fake as unknown as Session, "https://example.test/");
      await vi.advanceTimersByTimeAsync(QUIET_CAP_MS + 1000);
      expect(await pending).toMatchObject({ urlChanged: false, settleCapped: true });
      expect(notifyNavigation).not.toHaveBeenCalled();
    } finally { vi.useRealTimers(); }
  });
});
