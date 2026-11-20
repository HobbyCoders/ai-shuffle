// integration-part.ts deals the suite into PARTS; a part without its file
// would drop those tests silently, so the files must be exactly 1..PARTS.
import { readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import { PARTS } from "./integration-part.js";

describe("integration.browser parts", () => {
  it("has exactly one test file per part", () => {
    const dir = fileURLToPath(new URL(".", import.meta.url));
    const files = readdirSync(dir).filter((name) => /^integration\.browser\..*\.test\.ts$/.test(name)).sort();
    const expected = Array.from({ length: PARTS }, (_, i) => `integration.browser.${i + 1}.test.ts`).sort();
    expect(files).toEqual(expected);
  });
});
