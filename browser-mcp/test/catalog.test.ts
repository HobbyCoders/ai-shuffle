import { describe, it, expect } from "vitest";
import { TOOL_CATALOG } from "../src/catalog.js";
import { TOOL_DEFINITIONS } from "../src/tools/index.js";
import { readFileSync } from "node:fs";

 describe("metadata-only browser catalog", () => {
  it("has exact runtime catalog parity, including order and all schemas", () => {
    expect(TOOL_CATALOG).toEqual(TOOL_DEFINITIONS.map(({ run, ...metadata }) => metadata));
    expect(new Set(TOOL_CATALOG.map(t => t.name)).size).toBe(15);
    for (const definition of TOOL_DEFINITIONS) expect(typeof definition.run).toBe("function");
  });
  it("exports the same metadata for native sessions at build time", () => {
    const exported = JSON.parse(readFileSync(new URL("../dist/catalog.json", import.meta.url), "utf8"));
    expect(exported.version).toBe(1);
    expect(exported.tools).toEqual(TOOL_CATALOG);
  });
  it("keeps runtime implementations out of proxy startup imports", () => {
    for (const file of ["catalog.ts", "proxy.ts", "index.ts"]) {
      const source = readFileSync(new URL(`../src/${file}`, import.meta.url), "utf8");
      expect(source).not.toMatch(/^import .*from ["'].*(?:browser|session|workspace|tools\/index|playwright)/m);
    }
  });
});
