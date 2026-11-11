/**
 * Some hosts deliver only the start of the server instructions (the Claude Code
 * CLI cuts them at 4,096 characters). The first CORE_BUDGET characters must
 * hold a complete core of the rules an agent needs most, and the whole text
 * must not grow back.
 */
import { describe, expect, it } from "vitest";
import { CORE_BUDGET, SERVER_INSTRUCTIONS } from "../src/instructions.js";

// About 3% above the size after the restructure (7,055 characters). Raise it
// only with a reason; the previous text was 9,043 characters.
const TOTAL_CEILING = 7300;

const coreEnd = SERVER_INSTRUCTIONS.indexOf("\n\nACT RESULT\n");
const core = SERVER_INSTRUCTIONS.slice(0, coreEnd);

describe("server instructions budget", () => {
  it("ends the core inside the first CORE_BUDGET characters, at a sentence boundary", () => {
    expect(coreEnd).toBeGreaterThan(0);
    expect(coreEnd).toBeLessThanOrEqual(CORE_BUDGET);
    expect(core).toMatch(/\.$/);
    // A host cut at CORE_BUDGET keeps the whole core: nothing of it falls after the cut.
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

  it("keeps the whole text under its ceiling", () => {
    expect(SERVER_INSTRUCTIONS.length).toBeLessThanOrEqual(TOTAL_CEILING);
  });

  it("states each rule once", () => {
    const lines = SERVER_INSTRUCTIONS.split("\n").map((l) => l.trim()).filter((l) => l.length > 40);
    expect(new Set(lines).size).toBe(lines.length);
    const headings = SERVER_INSTRUCTIONS.split("\n").filter((l) => /^[A-Z][A-Z ,]+$/.test(l));
    expect(new Set(headings).size).toBe(headings.length);
  });
});
