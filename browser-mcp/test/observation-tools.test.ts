import { describe, it, expect } from "vitest";
import type { Session } from "../src/session.js";
import { runBatch, BATCH_MAX_CHARS } from "../src/tools/batch.js";
import { jsonResult, type McpToolResult } from "../src/tools/result.js";
import type { JsonSchema } from "../src/schemas.js";

const schema: JsonSchema = { type: "object", properties: {}, additionalProperties: false };
const session = {} as Session;
const act = (tool: string) => JSON.stringify({ tool, input: {} });
function batch(names: string[], outputs: Record<string, McpToolResult>) {
  return runBatch(session, { actions: names.map(act) }, name => ({ inputSchema: schema, run: async () => outputs[name] }));
}
const text = (r: McpToolResult) => r.content.flatMap(c => c.type === "text" ? [c.text] : []).join("\n");

describe("batch observations", () => {
  it("keeps navigate refs and the final read_text prose, not just JSON metadata", async () => {
    const prose = "Useful prose. ".repeat(1000);
    const result = await batch(["browser_snapshot", "browser_navigate", "browser_read_text"], {
      browser_snapshot: jsonResult({ ok: true }, '- button "Before" [ref=e1]'),
      browser_navigate: jsonResult({ ok: true, url_changed: true }, '- button "After" [ref=e2]'),
      browser_read_text: jsonResult({ ok: true }, prose),
    });
    expect(text(result)).toContain('[ref=e1]');
    expect(text(result)).toContain('[ref=e2]');
    expect(text(result)).toContain(prose);
  });
  it("preserves a stale act's attached tree, including failed acts", async () => {
    for (const ok of [true, false]) {
      const result = await batch(["browser_act", "browser_read_text"], {
        browser_act: jsonResult({ ok, snapshot_stale: true }, '- button "Fresh" [ref=e9]'),
        browser_read_text: jsonResult({ ok: true }, "Prose"),
      });
      expect(text(result)).toContain('[ref=e9]');
    }
  });
  it("reserves a fresh tree even when the final observation exceeds the whole budget", async () => {
    const result = await batch(["browser_act", "browser_read_text"], {
      browser_act: jsonResult({ ok: true, snapshot_stale: true }, '- button "Fresh" [ref=e9]'),
      browser_read_text: jsonResult({ ok: true }, "X".repeat(BATCH_MAX_CHARS * 2)),
    });
    expect(text(result)).toContain('[ref=e9]');
    expect(text(result).length).toBeLessThanOrEqual(BATCH_MAX_CHARS);
  });
  it("keeps each screenshot next to its own screenshot_id", async () => {
    const result = await batch(["shot1", "shot2", "browser_read_text"], {
      shot1: { content: [{ type: "text", text: '{"screenshot_id":"s1"}' }, { type: "image", data: "one", mimeType: "image/jpeg" }] },
      shot2: { content: [{ type: "text", text: '{"screenshot_id":"s2"}' }, { type: "image", data: "two", mimeType: "image/jpeg" }] },
      browser_read_text: jsonResult({ ok: true }, "After"),
    });
    for (const [i, c] of result.content.entries()) {
      if (c.type !== "image") continue;
      expect(result.content[i - 1]).toEqual({ type: "text", text: c.data === "one" ? '{"screenshot_id":"s1"}' : '{"screenshot_id":"s2"}' });
    }
  });
  it("gives the final observation priority under a bounded shared text budget", async () => {
    const tail = "Final prose ".repeat(2000);
    const result = await batch(["browser_snapshot", "browser_read_text"], {
      browser_snapshot: jsonResult({ ok: true }, "Earlier ".repeat(20000)),
      browser_read_text: jsonResult({ ok: true }, tail),
    });
    expect(text(result)).toContain(tail);
    expect(text(result)).toContain("batch output truncated");
    expect(text(result).length).toBeLessThanOrEqual(BATCH_MAX_CHARS);
  });
});
