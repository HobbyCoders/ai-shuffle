import { describe, it, expect } from "vitest";
import {
  TOOL_SCHEMAS,
  validateInput,
  navigateSchema,
  snapshotSchema,
  actSchema,
  screenshotSchema,
  consoleSchema,
  networkSchema,
  tabsSchema,
  resizeSchema,
  uploadSchema,
  batchSchema,
  evalSchema,
  ACT_KINDS,
} from "../src/schemas.js";

const BANNED = ["anyOf", "oneOf", "allOf", "$ref", "not", "if", "then"];

/** Collect every object key that appears anywhere in a value. */
function allKeys(value: unknown, acc: Set<string> = new Set()): Set<string> {
  if (Array.isArray(value)) {
    for (const el of value) allKeys(el, acc);
  } else if (value && typeof value === "object") {
    for (const [k, v] of Object.entries(value)) {
      acc.add(k);
      allKeys(v, acc);
    }
  }
  return acc;
}

describe("schema floor (OpenAI strict / Gemini compat)", () => {
  it("exports exactly the full tool set and no more", () => {
    expect(Object.keys(TOOL_SCHEMAS).sort()).toEqual([
      "browser_act",
      "browser_batch",
      "browser_console",
      "browser_eval",
      "browser_find",
      "browser_navigate",
      "browser_network",
      "browser_read_text",
      "browser_resize",
      "browser_screenshot",
      "browser_snapshot",
      "browser_tabs",
      "browser_upload",
    ]);
  });

  it("contains no banned JSON-schema constructs (as schema keys) anywhere", () => {
    // Check object KEYS, not raw substrings — English text like "Shift" happens
    // to contain "if" but is not a schema construct.
    const keys = allKeys(TOOL_SCHEMAS);
    for (const construct of BANNED) {
      expect(keys.has(construct)).toBe(false);
    }
  });

  it("is flat: every property is a primitive/enum/array-of-primitive, no nested objects", () => {
    for (const schema of Object.values(TOOL_SCHEMAS)) {
      expect(schema.type).toBe("object");
      expect(schema.additionalProperties).toBe(false);
      for (const prop of Object.values(schema.properties)) {
        expect(["string", "number", "integer", "boolean", "array"]).toContain(prop.type);
        if (prop.type === "array") {
          expect(["string", "number", "integer", "boolean"]).toContain(prop.items?.type);
        }
      }
    }
  });
});

describe("validateInput — navigate", () => {
  it("requires url", () => {
    expect(validateInput(navigateSchema, {}).ok).toBe(false);
    expect(validateInput(navigateSchema, { url: "example.com" }).ok).toBe(true);
  });
  it("rejects unknown keys and wrong types", () => {
    expect(validateInput(navigateSchema, { url: 5 }).ok).toBe(false);
    expect(validateInput(navigateSchema, { url: "x", bogus: 1 }).ok).toBe(false);
  });
});

describe("validateInput — snapshot", () => {
  it("accepts empty input (all optional)", () => {
    expect(validateInput(snapshotSchema, {}).ok).toBe(true);
  });
  it("enforces the filter enum", () => {
    expect(validateInput(snapshotSchema, { filter: "interactive" }).ok).toBe(true);
    expect(validateInput(snapshotSchema, { filter: "everything" }).ok).toBe(false);
  });
  it("enforces max_chars minimum and integer type", () => {
    expect(validateInput(snapshotSchema, { max_chars: 100 }).ok).toBe(false);
    expect(validateInput(snapshotSchema, { max_chars: 5000 }).ok).toBe(true);
    expect(validateInput(snapshotSchema, { max_chars: 5000.5 }).ok).toBe(false);
  });
});

describe("validateInput — act", () => {
  it("requires kind and enforces the kind enum", () => {
    expect(validateInput(actSchema, {}).ok).toBe(false);
    expect(validateInput(actSchema, { kind: "teleport" }).ok).toBe(false);
    for (const kind of ACT_KINDS) {
      expect(validateInput(actSchema, { kind }).ok).toBe(true);
    }
  });
  it("accepts coordinate arrays of numbers, rejects non-numeric items", () => {
    expect(validateInput(actSchema, { kind: "click", coordinate: [10, 20] }).ok).toBe(true);
    expect(validateInput(actSchema, { kind: "click", coordinate: ["a", "b"] }).ok).toBe(false);
  });
  it("rejects non-finite numbers in number arrays (NaN/Infinity)", () => {
    expect(validateInput(actSchema, { kind: "click", coordinate: [NaN, 20] }).ok).toBe(false);
    expect(validateInput(actSchema, { kind: "click", coordinate: [10, Infinity] }).ok).toBe(false);
    expect(validateInput(actSchema, { kind: "click", coordinate: [10, -Infinity] }).ok).toBe(false);
  });
  it("enforces the scroll direction enum", () => {
    expect(validateInput(actSchema, { kind: "scroll", direction: "down" }).ok).toBe(true);
    expect(validateInput(actSchema, { kind: "scroll", direction: "sideways" }).ok).toBe(false);
  });
});

describe("validateInput — screenshot", () => {
  it("accepts region arrays and full_page boolean", () => {
    expect(validateInput(screenshotSchema, { region: [0, 0, 100, 100] }).ok).toBe(true);
    expect(validateInput(screenshotSchema, { full_page: true }).ok).toBe(true);
    expect(validateInput(screenshotSchema, { full_page: "yes" }).ok).toBe(false);
  });
});

describe("validateInput — eval", () => {
  it("requires code as a string", () => {
    expect(validateInput(evalSchema, {}).ok).toBe(false);
    expect(validateInput(evalSchema, { code: "1+1" }).ok).toBe(true);
    expect(validateInput(evalSchema, { code: 5 }).ok).toBe(false);
  });
});

describe("validateInput — console", () => {
  it("requires pattern", () => {
    expect(validateInput(consoleSchema, {}).ok).toBe(false);
    expect(validateInput(consoleSchema, { pattern: "err" }).ok).toBe(true);
  });
  it("validates optional flags and limit bounds", () => {
    expect(validateInput(consoleSchema, { pattern: "x", only_errors: true, clear: true }).ok).toBe(true);
    expect(validateInput(consoleSchema, { pattern: "x", limit: 0 }).ok).toBe(false);
    expect(validateInput(consoleSchema, { pattern: "x", only_errors: "yes" }).ok).toBe(false);
  });
});

describe("validateInput — network", () => {
  it("accepts empty input and optional fields", () => {
    expect(validateInput(networkSchema, {}).ok).toBe(true);
    expect(validateInput(networkSchema, { url_pattern: "/api", limit: 5 }).ok).toBe(true);
    expect(validateInput(networkSchema, { request_id: "req1" }).ok).toBe(true);
    expect(validateInput(networkSchema, { limit: 0 }).ok).toBe(false);
  });
});

describe("validateInput — tabs", () => {
  it("requires op and enforces the op enum", () => {
    expect(validateInput(tabsSchema, {}).ok).toBe(false);
    expect(validateInput(tabsSchema, { op: "list" }).ok).toBe(true);
    expect(validateInput(tabsSchema, { op: "select", tab_id: "tab2" }).ok).toBe(true);
    expect(validateInput(tabsSchema, { op: "teleport" }).ok).toBe(false);
  });
});

describe("validateInput — resize", () => {
  it("accepts presets and explicit dims", () => {
    expect(validateInput(resizeSchema, { preset: "mobile" }).ok).toBe(true);
    expect(validateInput(resizeSchema, { width: 800, height: 600 }).ok).toBe(true);
    expect(validateInput(resizeSchema, { preset: "phone" }).ok).toBe(false);
    expect(validateInput(resizeSchema, { width: 0, height: 600 }).ok).toBe(false);
  });
});

describe("validateInput — upload", () => {
  it("requires ref and a string-array of paths", () => {
    expect(validateInput(uploadSchema, { ref: "e1" }).ok).toBe(false);
    expect(validateInput(uploadSchema, { ref: "e1", paths: ["/a"] }).ok).toBe(true);
    expect(validateInput(uploadSchema, { ref: "e1", paths: [1] }).ok).toBe(false);
  });
});

describe("validateInput — batch", () => {
  it("requires actions as a string array", () => {
    expect(validateInput(batchSchema, {}).ok).toBe(false);
    expect(validateInput(batchSchema, { actions: ["{}"] }).ok).toBe(true);
    expect(validateInput(batchSchema, { actions: [{ tool: "x" }] }).ok).toBe(false);
  });
});
