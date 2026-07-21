import { describe, it, expect } from "vitest";
import {
  parseAriaYaml,
  parseNodeContent,
  filterInteractive,
  collapseSiblings,
  nameShape,
  serialize,
  collectRefs,
  truncateWithTail,
  renderSnapshot,
  INTERACTIVE_ROLES,
} from "../src/snapshot.js";

const SAMPLE = `- banner [ref=e1]:
  - heading "Welcome" [level=1] [ref=e2]
  - navigation "Primary" [ref=e3]:
    - link "Home" [ref=e4]
    - link "About" [ref=e5]
- main [ref=e6]:
  - text: Some intro paragraph.
  - list [ref=e7]:
    - listitem [ref=e8]:
      - link "Item 1" [ref=e9]
  - button "Submit" [ref=e20]`;

describe("parseAriaYaml", () => {
  it("builds a tree from indentation", () => {
    const roots = parseAriaYaml(SAMPLE);
    expect(roots).toHaveLength(2);
    expect(roots[0].role).toBe("banner");
    expect(roots[0].ref).toBe("e1");
    expect(roots[0].children.map((c) => c.role)).toEqual(["heading", "navigation"]);
    const nav = roots[0].children[1];
    expect(nav.children).toHaveLength(2);
    expect(nav.children[0].name).toBe("Home");
    expect(nav.children[0].ref).toBe("e4");
  });

  it("captures the deeply nested list item", () => {
    const roots = parseAriaYaml(SAMPLE);
    const main = roots[1];
    const list = main.children.find((c) => c.role === "list")!;
    expect(list.children[0].children[0].name).toBe("Item 1");
    expect(list.children[0].children[0].ref).toBe("e9");
  });
});

describe("parseNodeContent", () => {
  it("extracts role, name, ref, and non-ref attrs", () => {
    const n = parseNodeContent('heading "Welcome" [level=1] [ref=e2]', 0);
    expect(n.role).toBe("heading");
    expect(n.name).toBe("Welcome");
    expect(n.ref).toBe("e2");
    expect(n.attrs).toEqual(["level=1"]);
  });

  it("handles text nodes without a role token", () => {
    const n = parseNodeContent("text: Some intro paragraph.", 0);
    expect(n.role).toBe("text");
    expect(n.name).toBe("");
    expect(n.ref).toBeUndefined();
  });

  it("keeps escaped quotes inside the name", () => {
    const n = parseNodeContent('button "Say \\"hi\\"" [ref=e3]', 0);
    expect(n.name).toBe('Say \\"hi\\"');
  });
});

describe("filterInteractive", () => {
  it("keeps interactive roles and the ancestors that lead to them", () => {
    const roots = filterInteractive(parseAriaYaml(SAMPLE));
    const out = serialize(roots);
    // Interactive controls survive.
    expect(out).toContain('link "Home"');
    expect(out).toContain('button "Submit"');
    // Free text is dropped.
    expect(out).not.toContain("Some intro paragraph");
  });

  it("drops branches with no interactive descendant", () => {
    // article/paragraph are neither interactive nor landmarks, so a subtree of
    // pure text under them is pruned entirely.
    const tree = parseAriaYaml(`- article [ref=e1]:
  - text: filler
  - paragraph [ref=e2]:
    - text: more filler`);
    expect(filterInteractive(tree)).toHaveLength(0);
  });

  it("treats headings and landmarks as orientation anchors", () => {
    expect(INTERACTIVE_ROLES.has("heading")).toBe(true);
    expect(INTERACTIVE_ROLES.has("navigation")).toBe(true);
    expect(INTERACTIVE_ROLES.has("main")).toBe(true);
  });
});

describe("nameShape", () => {
  it("normalizes digits and whitespace", () => {
    expect(nameShape("Item 12")).toBe("item #");
    expect(nameShape("Item 305")).toBe("item #");
    expect(nameShape("Result  7 of 90")).toBe("result # of #");
  });
});

describe("collapseSiblings", () => {
  function rows(n: number): string {
    const lines = ["- list [ref=e1]:"];
    for (let i = 1; i <= n; i++) {
      lines.push(`  - listitem [ref=e${100 + i}]:`);
      lines.push(`    - link "Item ${i}" [ref=e${200 + i}]`);
    }
    return lines.join("\n");
  }

  it("collapses runs longer than the threshold, keeping the first N", () => {
    const collapsed = collapseSiblings(parseAriaYaml(rows(20)), 8, 5);
    const list = collapsed[0];
    // 5 kept items + 1 marker.
    expect(list.children).toHaveLength(6);
    const marker = list.children[5];
    expect(marker.raw).toContain("... and 15 more similar items");
    expect(marker.raw).toContain("scope_ref=e1");
  });

  it("does not collapse short runs", () => {
    const collapsed = collapseSiblings(parseAriaYaml(rows(6)), 8, 5);
    expect(collapsed[0].children).toHaveLength(6);
    expect(serialize(collapsed)).not.toContain("more similar items");
  });

  it("uses the parent ref in the expand hint", () => {
    const out = serialize(collapseSiblings(parseAriaYaml(rows(30)), 8, 5));
    expect(out).toContain("scope_ref=e1 to expand");
  });
});

describe("truncateWithTail", () => {
  it("returns text unchanged when under budget", () => {
    const r = truncateWithTail("a\nb\nc", 1000);
    expect(r.truncated).toBe(false);
    expect(r.text).toBe("a\nb\nc");
    expect(r.fullChars).toBe(5);
  });

  it("cuts at a line boundary and appends a self-describing tail", () => {
    const body = Array.from({ length: 50 }, (_, i) => `line-${i}`).join("\n");
    const r = truncateWithTail(body, 120);
    expect(r.truncated).toBe(true);
    expect(r.text.length).toBeLessThanOrEqual(120);
    expect(r.text).toContain(`full snapshot is ${body.length} chars`);
    expect(r.text).toContain("filter=interactive");
    // Head ends on a clean line (no dangling partial line before the tail).
    const head = r.text.split("\n[truncated")[0];
    expect(head.endsWith("\n")).toBe(false);
    expect(body.startsWith(head)).toBe(true);
  });
});

describe("collectRefs", () => {
  it("gathers refs in document order", () => {
    expect(collectRefs(parseAriaYaml(SAMPLE))).toEqual([
      "e1", "e2", "e3", "e4", "e5", "e6", "e7", "e8", "e9", "e20",
    ]);
  });
});

describe("renderSnapshot pipeline", () => {
  it("applies filter + collapse + truncation and reports refs", () => {
    const r = renderSnapshot(SAMPLE, { filter: "all", maxChars: 40000 });
    expect(r.truncated).toBe(false);
    expect(r.refs).toContain("e20");
    expect(r.text).toContain('button "Submit"');
  });

  it("filter=interactive strips free text", () => {
    const r = renderSnapshot(SAMPLE, { filter: "interactive" });
    expect(r.text).not.toContain("Some intro paragraph");
    expect(r.text).toContain('link "About"');
  });
});
