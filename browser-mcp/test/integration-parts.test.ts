// integration-part.ts deals the suite into PARTS; a part without its file
// would drop those tests silently, so the files must be exactly 1..PARTS.
import { readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import { PARTS, describe as partDescribe, rootDescribeSkipIf } from "./integration-part.js";

// A native gated block inside a dealt one would register during the dry pass
// and again on replay (one test run twice): the gate refuses to nest. The
// block holds no units, so nothing is registered here.
let nested: unknown;
partDescribe("a dealt block", () => {
  try {
    rootDescribeSkipIf(false)("a nested gate", () => {});
  } catch (error) {
    nested = error;
  }
});

describe("integration.browser parts", () => {
  it("has exactly one test file per part", () => {
    const dir = fileURLToPath(new URL(".", import.meta.url));
    const files = readdirSync(dir).filter((name) => /^integration\.browser\..*\.test\.ts$/.test(name)).sort();
    const expected = Array.from({ length: PARTS }, (_, i) => `integration.browser.${i + 1}.test.ts`).sort();
    expect(files).toEqual(expected);
  });

  it("allows the browser gate only on the suite's outer block", () => {
    expect(String(nested)).toMatch(/only for the suite's outer block/);
    expect((partDescribe as unknown as { skipIf?: unknown }).skipIf).toBeUndefined();
  });
});
