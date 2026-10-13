import { describe, it, expect } from "vitest";
import {
  classifyMutations,
  DEFAULT_MUTATION_THRESHOLDS,
  QUIET_MS,
  QUIET_CAP_MS,
  type MutationStats,
} from "../src/dommutation.js";

function stats(p: Partial<MutationStats>): MutationStats {
  return {
    addedNodes: 0,
    removedNodes: 0,
    attributeChanges: 0,
    characterDataChanges: 0,
    urlChanged: false,
    ...p,
  };
}

describe("classifyMutations", () => {
  it("returns none for an empty record", () => {
    expect(classifyMutations(stats({}))).toBe("none");
  });

  it("returns major when the URL changed regardless of node counts", () => {
    expect(classifyMutations(stats({ urlChanged: true }))).toBe("major");
    expect(classifyMutations(stats({ urlChanged: true, addedNodes: 1 }))).toBe("major");
  });

  it("returns major when structural churn hits the threshold", () => {
    const t = DEFAULT_MUTATION_THRESHOLDS.majorNodeThreshold;
    expect(classifyMutations(stats({ addedNodes: t }))).toBe("major");
    expect(classifyMutations(stats({ addedNodes: t - 1, removedNodes: 1 }))).toBe("major");
  });

  it("returns minor for structural churn below the threshold", () => {
    expect(classifyMutations(stats({ addedNodes: 3 }))).toBe("minor");
    expect(classifyMutations(stats({ removedNodes: 1 }))).toBe("minor");
  });

  it("returns minor for attribute/text-only changes", () => {
    expect(classifyMutations(stats({ attributeChanges: 5 }))).toBe("minor");
    expect(classifyMutations(stats({ characterDataChanges: 2 }))).toBe("minor");
  });

  it("respects a custom threshold", () => {
    expect(classifyMutations(stats({ addedNodes: 4 }), { majorNodeThreshold: 4 })).toBe("major");
    expect(classifyMutations(stats({ addedNodes: 3 }), { majorNodeThreshold: 4 })).toBe("minor");
  });
});

describe("settle timing constants", () => {
  it("uses ~300ms quiet capped at 5s", () => {
    expect(QUIET_MS).toBe(300);
    expect(QUIET_CAP_MS).toBe(5000);
    expect(QUIET_CAP_MS).toBeGreaterThan(QUIET_MS);
  });
});
