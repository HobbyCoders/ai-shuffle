/** The disclosed deadline stops waiting, not the page's work (#683). */
import { describe, it, expect, vi, afterEach } from "vitest";
import { existsSync } from "node:fs";
import { chromium } from "playwright-core";
import { Session, EVAL_TIMEOUT_MS, EvalTimeoutError } from "../src/session.js";
import { runEval } from "../src/tools/eval.js";
import { TOOL_CATALOG } from "../src/catalog.js";
import { evalSchema } from "../src/schemas.js";
import type { McpToolResult } from "../src/tools/result.js";

const bodyOf = (result: McpToolResult) => {
  const content = result.content[0];
  if (content.type !== "text") throw new Error("expected text");
  return JSON.parse(content.text);
};

afterEach(() => vi.useRealTimers());

it("discloses the deadline and non-cancellation in both discovery surfaces", () => {
  const description = TOOL_CATALOG.find(tool => tool.name === "browser_eval")!.description;
  // Fixed measured discovery payload (660 chars) + 1%; no duplicate paragraphs.
  const tool = TOOL_CATALOG.find(tool => tool.name === "browser_eval")!;
  expect(JSON.stringify(tool).length).toBeLessThanOrEqual(Math.floor(660 * 1.01));
  const paragraphs = description.split("\n\n").filter(Boolean);
  expect(new Set(paragraphs).size).toBe(paragraphs.length);
  for (const text of [description, evalSchema.properties.code.description]) {
    expect(text).toContain(`${EVAL_TIMEOUT_MS} ms deadline`);
    expect(text).toContain("does not cancel page-side work");
    expect(text).toMatch(/inspect state before retrying mutations/i);
    expect(text).toContain("browser_wait");
  }
});

it("reports typed timeout errors as unknown, not not_executed", async () => {
  const session = {
    ensurePage: async () => undefined,
    invalidateScreenshot: () => {},
    evaluate: async () => { throw new EvalTimeoutError(EVAL_TIMEOUT_MS); },
  } as unknown as Session;
  const result = await runEval(session, { code: "doSomething()" });
  expect(result.isError).toBe(true);
  expect(bodyOf(result)).toMatchObject({ ok: false, code: "outcome_unknown",
    outcome: "unknown", timed_out: true, timeout_ms: 5000 });
  expect(bodyOf(result).error).toContain("page-side work may still be running");
});

it("does not mislabel page closure as a deadline", async () => {
  const session = {
    ensurePage: async () => undefined,
    invalidateScreenshot: () => {},
    evaluate: async () => { throw new Error("Page closed"); },
  } as unknown as Session;
  const result = bodyOf(await runEval(session, { code: "42" }));
  expect(result).toMatchObject({ ok: false, error: "Page closed" });
  expect(result.timed_out).toBeUndefined();
});

it("uses the real Session deadline without cancelling its pending evaluation", async () => {
  vi.useFakeTimers();
  let finish!: (value: number) => void;
  const pendingPageWork = new Promise<number>(resolve => { finish = resolve; });
  const context = { requirePage: () => ({ evaluate: () => pendingPageWork }) };
  const pending = Session.prototype.evaluate.call(context as unknown as Session, () => 42, "");
  const assertion = expect(pending).rejects.toBeInstanceOf(EvalTimeoutError);
  await vi.advanceTimersByTimeAsync(EVAL_TIMEOUT_MS - 1);
  expect(vi.getTimerCount()).toBe(1);
  await vi.advanceTimersByTimeAsync(1);
  await assertion;
  expect(vi.getTimerCount()).toBe(0);
  finish(42);
  await expect(pendingPageWork).resolves.toBe(42);
});

const browserPath = process.env.CHROMIUM_PATH || [
  "/usr/bin/chromium", "/usr/bin/chromium-browser", "/usr/bin/google-chrome-stable",
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
].find(path => existsSync(path));

describe.runIf(Boolean(browserPath))("late completion in a real page", () => {
  it("observes the marker independently after runEval has already timed out", async () => {
    const browser = await chromium.launch({ executablePath: browserPath, args: ["--no-sandbox"] });
    try {
      const page = await browser.newPage();
      await page.setContent("<main>eval deadline fixture</main>");
      const session = {
        ensurePage: async () => undefined,
        invalidateScreenshot: () => {},
        requirePage: () => page,
        evaluate: Session.prototype.evaluate,
      } as unknown as Session;
      const result = bodyOf(await runEval(session, { code: `
        await new Promise(resolve => { window.__finishDeadlineEval = resolve; });
        document.body.dataset.lateEval = "finished";
        return "done";
      ` }));
      expect(result).toMatchObject({ ok: false, timed_out: true, outcome: "unknown" });
      expect(await page.getAttribute("body", "data-late-eval")).toBeNull();
      await page.evaluate("window.__finishDeadlineEval()");
      await page.waitForFunction(() => document.body.dataset.lateEval === "finished");
      expect(await page.getAttribute("body", "data-late-eval")).toBe("finished");
    } finally {
      await browser.close();
    }
  });
});
