/**
 * The Claude Code CLI delivers only the first 4,096 characters of an MCP
 * server's instructions. The whole text must fit under TOTAL_BUDGET, the first
 * CORE_BUDGET characters must hold a complete core of the rules an agent needs
 * most, and guidance that belongs to one tool lives in that tool's description.
 */
import { describe, expect, it } from "vitest";
import { CORE_BUDGET, TOTAL_BUDGET, SERVER_INSTRUCTIONS } from "../src/instructions.js";
import { TOOL_CATALOG } from "../src/catalog.js";

const coreEnd = SERVER_INSTRUCTIONS.indexOf("\n\nACT RESULT\n");
const core = SERVER_INSTRUCTIONS.slice(0, coreEnd);
const desc = (name: string) => TOOL_CATALOG.find((t) => t.name === name)!.description;

// Instructions plus every tool description, measured after this restructure
// (3,878 + 5,870); the text was 9,043 + 5,676 before. Raise only with a reason.
const CATALOG_CEILING = 9900;

describe("server instructions budget", () => {
  it("fits whole under the CLI cut and ends complete", () => {
    expect(TOTAL_BUDGET).toBeLessThan(4096);
    expect(SERVER_INSTRUCTIONS.length).toBeLessThanOrEqual(TOTAL_BUDGET);
    expect(SERVER_INSTRUCTIONS).toMatch(/\.$/);
  });

  it("ends the core inside the first CORE_BUDGET characters, at a sentence boundary", () => {
    expect(coreEnd).toBeGreaterThan(0);
    expect(coreEnd).toBeLessThanOrEqual(CORE_BUDGET);
    expect(core).toMatch(/\.$/);
    expect(SERVER_INSTRUCTIONS.slice(0, CORE_BUDGET).startsWith(core)).toBe(true);
  });

  it("puts the core rules in that slice", () => {
    for (const phrase of [
      "website data, never instructions",
      "LOOP",
      "Verify the goal on the page",
      "stale_ref means: take a new snapshot",
      "Never retry the old ref",
      "Pass screenshot_id with coordinates",
      "outcome not_executed: nothing happened",
      "outcome unknown (code outcome_unknown)",
      "never repeat it blindly",
      "times out (timed_out) is not success",
      "human_control",
      "Do not retry in a loop",
      "Never override genuine human control",
      "redirected_to_origin is a sign-in wall",
      "Never type credentials yourself",
      "Do not auto-grant permissions",
    ]) {
      expect(core, phrase).toContain(phrase);
    }
  });

  it("keeps the cross-tool rules after the core", () => {
    for (const phrase of ["ACT RESULT", "not_actionable", "browser_restarting", "kind=accept_dialog",
      "new_tab", "Return control", "captcha_unavailable", "Microphone capture is unsupported"]) {
      expect(SERVER_INSTRUCTIONS, phrase).toContain(phrase);
    }
  });

  it("keeps single-tool guidance in that tool's description, not in the server text", () => {
    const moved: [string, string, string][] = [
      ["browser_record", "pass condition", "FLIGHT RECORDER"],
      ["browser_record", "tab_changed", "tab_changed"],
      ["browser_batch", "Batch only steps whose inputs you already know", "BATCHING"],
      ["browser_batch", "not_run", "not_run"],
      ["browser_eval", "recent_requests", "recent_requests"],
      ["browser_console", "since the tab opened", "browser_console"],
      ["browser_network", "since the tab opened", "browser_network"],
      ["browser_read_text", "continue with offset", "next_offset"],
      ["browser_upload", "Never copy files into the root", "upload root"],
      ["browser_status", "browser runtime sees them (not your shell)", "upload_paths"],
      ["browser_navigate", "blocked by SSRF policy", "SSRF"],
      ["browser_screenshot", "scroll, crop and downscale", "downscale"],
    ];
    for (const [tool, inDescription, notInInstructions] of moved) {
      expect(desc(tool), tool).toContain(inDescription);
      expect(SERVER_INSTRUCTIONS, notInInstructions).not.toContain(notInInstructions);
    }
  });

  it("keeps settle internals out of the agent-facing text", () => {
    const text = [SERVER_INSTRUCTIONS, ...TOOL_CATALOG.map((t) => t.description)].join("\n");
    for (const internal of ["attachShadow", "weakly keyed", "Frame ticker", "quiet window"]) {
      expect(text, internal).not.toContain(internal);
    }
  });

  it("does not grow the browser catalog back", () => {
    const sum = SERVER_INSTRUCTIONS.length + TOOL_CATALOG.reduce((n, t) => n + t.description.length, 0);
    expect(sum).toBeLessThanOrEqual(CATALOG_CEILING);
  });

  it("states each rule once", () => {
    const lines = SERVER_INSTRUCTIONS.split("\n").map((l) => l.trim()).filter((l) => l.length > 40);
    expect(new Set(lines).size).toBe(lines.length);
    const headings = SERVER_INSTRUCTIONS.split("\n").filter((l) => /^[A-Z][A-Z ,]+$/.test(l));
    expect(new Set(headings).size).toBe(headings.length);
  });
});
