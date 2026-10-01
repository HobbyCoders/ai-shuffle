import { describe, it, expect } from "vitest";
import { RefRegistry, isRefToken, STALE_REF_REMEDY } from "../src/refs.js";

describe("RefRegistry", () => {
  it("advances snapshot_id monotonically", () => {
    const r = new RefRegistry();
    expect(r.currentSnapshotId).toBe(0);
    expect(r.registerSnapshot(["e1"])).toBe(1);
    expect(r.registerSnapshot(["e2"])).toBe(2);
    expect(r.currentSnapshotId).toBe(2);
  });

  it("unions a scoped snapshot's refs onto the latest full snapshot", () => {
    const r = new RefRegistry();
    r.registerSnapshot(["e1", "e2"]); // full
    r.registerSnapshot(["e3"], true); // scoped — e1/e2 must stay valid
    expect(r.isKnown("e1")).toBe(true);
    expect(r.isKnown("e2")).toBe(true);
    expect(r.isKnown("e3")).toBe(true);
    expect(r.knownCount()).toBe(3);
  });

  it("prunes refs from a superseded full snapshot (bounded on SPAs)", () => {
    const r = new RefRegistry();
    r.registerSnapshot(["e1", "e2"]); // full
    r.registerSnapshot(["e5", "e6"]); // new full snapshot supersedes the old one
    expect(r.isKnown("e1")).toBe(false);
    expect(r.isKnown("e2")).toBe(false);
    expect(r.isKnown("e5")).toBe(true);
    expect(r.isKnown("e6")).toBe(true);
    expect(r.knownCount()).toBe(2);
  });

  it("a new full snapshot drops stale scoped refs", () => {
    const r = new RefRegistry();
    r.registerSnapshot(["e1", "e2"]); // full
    r.registerSnapshot(["e9"], true); // scoped — adds e9
    expect(r.isKnown("e9")).toBe(true);
    r.registerSnapshot(["e1", "e2"]); // full again — e9 is now stale
    expect(r.isKnown("e9")).toBe(false);
    expect(r.isKnown("e1")).toBe(true);
  });

  it("invalidates every ref on navigation", () => {
    const r = new RefRegistry();
    r.registerSnapshot(["e1", "e2"]);
    r.invalidateForNavigation();
    expect(r.isKnown("e1")).toBe(false);
    expect(r.knownCount()).toBe(0);
    // snapshot_id keeps climbing (does not reset).
    expect(r.registerSnapshot(["e9"])).toBe(2);
  });

  it("keeps refs across a same-document navigation once the document is verified", () => {
    const r = new RefRegistry();
    r.registerSnapshot(["e1", "e2"], false, "doc-A");
    r.noteNavigation(); // history.replaceState / pushState / hash change
    expect(r.needsVerification).toBe(true);
    r.verifyDocument("doc-A", r.navigationMark);
    expect(r.needsVerification).toBe(false);
    expect(r.isKnown("e1")).toBe(true);
    expect(r.isKnown("e2")).toBe(true);
  });

  it("drops every ref when the navigation committed a new document", () => {
    const r = new RefRegistry();
    r.registerSnapshot(["e1", "e2"], false, "doc-A");
    r.registerSnapshot(["e3"], true, "doc-A");
    r.noteNavigation();
    r.verifyDocument("doc-B", r.navigationMark);
    expect(r.needsVerification).toBe(false);
    expect(r.knownCount()).toBe(0);
  });

  it("treats an unreadable document identity as a new document", () => {
    const r = new RefRegistry();
    r.registerSnapshot(["e1"], false, "doc-A");
    r.noteNavigation();
    r.verifyDocument(null, r.navigationMark);
    expect(r.isKnown("e1")).toBe(false);
  });

  it("keeps navigations that arrive during verification pending", () => {
    const r = new RefRegistry();
    r.registerSnapshot(["e1"], false, "doc-A");
    r.noteNavigation();
    const mark = r.navigationMark;
    r.noteNavigation(); // lands while the identity lookup is in flight
    r.verifyDocument("doc-A", mark);
    expect(r.isKnown("e1")).toBe(true);
    expect(r.needsVerification).toBe(true);
    r.verifyDocument("doc-B", r.navigationMark);
    expect(r.isKnown("e1")).toBe(false);
  });

  it("does nothing without a pending navigation", () => {
    const r = new RefRegistry();
    r.registerSnapshot(["e1"], false, "doc-A");
    r.verifyDocument("doc-B", r.navigationMark);
    expect(r.isKnown("e1")).toBe(true);
  });

  it("a scoped snapshot of a different document drops the old full refs", () => {
    const r = new RefRegistry();
    r.registerSnapshot(["e1", "e2"], false, "doc-A");
    r.registerSnapshot(["e7"], true, "doc-B");
    expect(r.isKnown("e1")).toBe(false);
    expect(r.isKnown("e7")).toBe(true);
    // ...and a scoped re-read of the same document keeps them.
    r.registerSnapshot(["e1", "e2"], false, "doc-B");
    r.registerSnapshot(["e8"], true, "doc-B");
    expect(r.isKnown("e1")).toBe(true);
    expect(r.isKnown("e8")).toBe(true);
  });

  it("reports unknown refs as not known", () => {
    const r = new RefRegistry();
    r.registerSnapshot(["e1"]);
    expect(r.isKnown("e99")).toBe(false);
  });

  it("builds the aria-ref selector", () => {
    expect(RefRegistry.selectorFor("e12")).toBe("aria-ref=e12");
  });
});

describe("isRefToken", () => {
  it("accepts eN and rejects everything else", () => {
    expect(isRefToken("e1")).toBe(true);
    expect(isRefToken("e123")).toBe(true);
    expect(isRefToken("e")).toBe(false);
    expect(isRefToken("12")).toBe(false);
    expect(isRefToken("x1")).toBe(false);
    expect(isRefToken("e1 ")).toBe(false);
  });
});

describe("STALE_REF_REMEDY", () => {
  it("names the fix", () => {
    expect(STALE_REF_REMEDY).toContain("browser_snapshot");
  });
});
