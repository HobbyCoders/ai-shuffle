/**
 * Tool handlers over a fake session: the error, bound and truncation paths the
 * real-Chromium suite does not reach cheaply (a body that never finishes, a
 * binary body with no type, a redirect that lands on a refused hop, a page
 * script that never returns inside a panel edit, a very long typed value).
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Session, StaleRefError } from "../src/session.js";
import { BrowserManager } from "../src/browser.js";
import { runNetwork, NETWORK_BODY_TIMEOUT_MS } from "../src/tools/network.js";
import { runReadText, READ_TEXT_MAX_CHARS } from "../src/tools/read_text.js";
import { runEval, EVAL_MAX_CHARS } from "../src/tools/eval.js";
import { runConsole } from "../src/tools/console.js";
import { runTabs } from "../src/tools/tabs.js";
import { runUpload } from "../src/tools/upload.js";
import { runAct, typeTimeoutMs, TYPE_MS_PER_CHAR, type ActInput } from "../src/tools/act.js";
import { runNavigate } from "../src/tools/navigate.js";
import { capText, errorResult, type McpToolResult } from "../src/tools/result.js";
import { TOOL_MAP } from "../src/tools/index.js";
import { BrowserWorkspace, EDIT_TIMEOUT_MS } from "../src/workspace.js";
import { uploadField, FILE_FIELD_TIMEOUT_MS } from "../src/editors.js";
import { NetworkBuffer, newConsoleBuffer, cappedConsoleEntry } from "../src/buffers.js";

const texts = (res: McpToolResult) => res.content.flatMap((c) => (c.type === "text" ? [c.text] : []));
const bodyOf = (res: McpToolResult) => JSON.parse(texts(res)[0] ?? "{}") as Record<string, unknown>;

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  delete process.env.BROWSER_UPLOAD_DIR;
  delete process.env.AIS_BROWSER_PANEL_UPLOAD_DIR;
});

// ---------------------------------------------------------------------------
// browser_network

function networkSession(response: { headers: () => Record<string, string>; text: () => Promise<string> }) {
  const network = new NetworkBuffer();
  network.record({ method: "GET", url: "http://h/a.js", status: 200, resourceType: "script", response: response as never });
  network.record({ method: "POST", url: "http://h/api/x", status: 201, resourceType: "fetch", response: response as never });
  network.record({ method: "GET", url: "http://h/api/y", status: 404, resourceType: "fetch", response: response as never });
  let cleared = false;
  const session = {
    ensurePage: async () => undefined,
    findNetworkEntry: (id: string) => network.find(id),
    networkEntries: () => (cleared ? [] : network.all()),
    clearNetwork: () => { cleared = true; },
    checkCaptcha: async () => "none" as const,
  } as unknown as Session;
  return { session, isCleared: () => cleared };
}

describe("browser_network body phase", () => {
  it("bounds a body that never finishes (event stream / long poll) instead of hanging the queue", async () => {
    vi.useFakeTimers();
    const { session } = networkSession({
      headers: () => ({ "content-type": "text/event-stream" }),
      text: () => new Promise<string>(() => {}),
    });
    const pending = runNetwork(session, { request_id: "req1" });
    await vi.advanceTimersByTimeAsync(NETWORK_BODY_TIMEOUT_MS);
    const res = await pending;
    expect(res.isError).toBe(true);
    const body = bodyOf(res);
    expect(String(body.error)).toMatch(/still streaming/);
    expect(body).toMatchObject({ id: "req1", mimeType: "text/event-stream" });
  });

  it("notes a binary body sent without a content-type instead of returning mojibake", async () => {
    const { session } = networkSession({
      headers: () => ({}),
      text: async () => "\u0089PNG\r\n\u001a\n\u0000\u0000\u0000\rIHDR",
    });
    const res = await runNetwork(session, { request_id: "req1" });
    expect(res.isError).toBeFalsy();
    expect(bodyOf(res)).toMatchObject({ ok: true, note: "non-text body not returned", mimeType: null });
    expect(texts(res)).toHaveLength(1);
  });

  it("still returns an untyped textual body", async () => {
    const { session } = networkSession({ headers: () => ({}), text: async () => "plain words" });
    const res = await runNetwork(session, { request_id: "req1" });
    expect(texts(res)[1]).toBe("plain words");
  });

  it("notes a typed binary body without reading it", async () => {
    const text = vi.fn(async () => "never");
    const { session } = networkSession({ headers: () => ({ "content-type": "image/png", "content-length": "12" }), text });
    const res = await runNetwork(session, { request_id: "req1" });
    expect(bodyOf(res)).toMatchObject({ note: "non-text body not returned", mimeType: "image/png", size: 12 });
    expect(text).not.toHaveBeenCalled();
  });

  it("reports an evicted body as a clean error", async () => {
    const { session } = networkSession({
      headers: () => ({ "content-type": "application/json" }),
      text: async () => { throw new Error("Protocol error (Network.getResponseBody): No resource with given identifier found"); },
    });
    const res = await runNetwork(session, { request_id: "req1" });
    expect(res.isError).toBe(true);
    expect(String(bodyOf(res).error)).toMatch(/response body unavailable .*evicted after navigation/);
  });

  it("caps an oversized textual body and reports its full length", async () => {
    const { session } = networkSession({ headers: () => ({ "content-type": "text/plain" }), text: async () => "a".repeat(25000) });
    const res = await runNetwork(session, { request_id: "req1" });
    expect(bodyOf(res)).toMatchObject({ truncated: true, chars: 20000, full_chars: 25000 });
  });
});

describe("browser_network list phase", () => {
  const response = { headers: () => ({}), text: async () => "" };
  it("filters by url_pattern, keeps the newest `limit`, and clears after listing", async () => {
    const { session, isCleared } = networkSession(response);
    const listed = bodyOf(await runNetwork(session, { url_pattern: "/api/", limit: 1, clear: true }));
    expect(listed).toMatchObject({ matched: 2, returned: 1 });
    expect((listed.requests as { id: string; status: number }[])[0]).toMatchObject({ id: "req3", status: 404 });
    expect(isCleared()).toBe(true);
    expect(bodyOf(await runNetwork(session, {}))).toMatchObject({ matched: 0, returned: 0 });
  });
});

// ---------------------------------------------------------------------------
// truncation

describe("capText", () => {
  it("never leaves half of a surrogate pair at the cut", () => {
    const value = "ab" + "😀".repeat(10);
    const cut = capText(value, 3); // "ab" + the high half of the first emoji
    expect(cut.text).toBe("ab");
    expect(cut.truncated).toBe(true);
    expect(cut.fullChars).toBe(value.length);
    expect(capText(value, 4).text).toBe("ab😀");
  });
});

describe("browser_read_text truncation", () => {
  function readSession(text: string): Session {
    const frame = { isDetached: () => false, parentFrame: () => null, page: (): object => page,
      locator: () => ({ evaluate: async () => text }) };
    const page = { title: async () => "Long", url: () => "http://h/long", mainFrame: () => frame, frames: () => [frame] };
    return {
      ensurePage: async () => page,
      checkCaptcha: async () => "none" as const,
    } as unknown as Session;
  }
  it("keeps the tail note and reports the returned length", async () => {
    const res = await runReadText(readSession("word ".repeat(READ_TEXT_MAX_CHARS)));
    const body = bodyOf(res);
    expect(body).toMatchObject({ ok: true, truncated: true, chars: READ_TEXT_MAX_CHARS });
    expect(body).toMatchObject({ total_chars: 150000, next_offset: READ_TEXT_MAX_CHARS });
    expect(texts(res)[1]).toMatch(/\n\[truncated: chars 0-30000 of 150000; call browser_read_text with offset=30000 for the rest\]$/);
  });
  it("does not end a cut page on a lone surrogate", async () => {
    const res = await runReadText(readSession("x".repeat(READ_TEXT_MAX_CHARS - 1) + "😀".repeat(10)));
    const trailing = texts(res)[1];
    const head = trailing.slice(0, trailing.indexOf("\n[truncated"));
    expect(head).toBe("x".repeat(READ_TEXT_MAX_CHARS - 1));
  });
});

// ---------------------------------------------------------------------------
// browser_eval

describe("browser_eval result shaping", () => {
  it("caps a huge value and reports full_chars", async () => {
    const value = JSON.stringify("v".repeat(EVAL_MAX_CHARS * 2));
    const session = {
      ensurePage: async () => undefined,
      invalidateScreenshot: () => {},
      evaluate: async () => ({ ok: true, type: "string", value }),
      checkCaptcha: async () => "none" as const,
    } as unknown as Session;
    const body = bodyOf(await runEval(session, { code: "x" }));
    expect(body).toMatchObject({ ok: true, truncated: true, full_chars: value.length });
    expect(String(body.value)).toHaveLength(EVAL_MAX_CHARS);
  });
  it("attaches recent responses to a page-side throw", async () => {
    const session = {
      ensurePage: async () => undefined,
      invalidateScreenshot: () => {},
      evaluate: async () => ({ ok: false, error: "Unexpected token '<'", stack: "SyntaxError: x\n    at f" }),
      recentNetwork: () => [{ id: "req4", method: "GET", url: "http://h/api", status: 302, content_type: "text/html" }],
      checkCaptcha: async () => "none" as const,
    } as unknown as Session;
    const res = await runEval(session, { code: "await (await fetch('/api')).json()" });
    expect(res.isError).toBe(true);
    expect(bodyOf(res)).toMatchObject({ error: "Unexpected token '<'", recent_requests: [{ status: 302 }] });
  });
  it("turns an evaluation that could not complete (closed page, timeout) into an error result", async () => {
    const session = {
      ensurePage: async () => undefined,
      invalidateScreenshot: () => {},
      evaluate: async () => { throw new Error("eval timed out"); },
      checkCaptcha: async () => "none" as const,
    } as unknown as Session;
    const res = await runEval(session, { code: "while(true){}" });
    expect(res.isError).toBe(true);
    expect(bodyOf(res).error).toBe("eval timed out");
  });
});

// ---------------------------------------------------------------------------
// browser_console / browser_tabs / browser_upload

describe("browser_console", () => {
  it("filters errors by pattern, returns the newest last, and clears on request", async () => {
    const buffer = newConsoleBuffer();
    for (const [level, text] of [["log", "boot a"], ["error", "boom 1"], ["error", "boom 2"], ["error", "other"]]) {
      buffer.push(cappedConsoleEntry(level, text, ""));
    }
    const session = {
      ensurePage: async () => undefined,
      consoleEntries: () => buffer.all(),
      clearConsole: () => buffer.clear(),
      checkCaptcha: async () => "none" as const,
    } as unknown as Session;
    const body = bodyOf(await runConsole(session, { pattern: "boom", only_errors: true, limit: 1, clear: true }));
    expect(body).toMatchObject({ matched: 2, returned: 1, entries: [{ level: "error", text: "boom 2" }] });
    expect(buffer.length).toBe(0);
  });
});

describe("browser_tabs error paths", () => {
  const session = {
    selectTab: async (id: string) => { throw new Error(`no such tab: ${id}`); },
    closeTab: async () => { throw new Error("cannot close the last tab"); },
    checkCaptcha: async () => "none" as const,
  } as unknown as Session;
  it("requires tab_id for select and close", async () => {
    expect(bodyOf(await runTabs(session, { op: "select" })).error).toBe("op=select requires tab_id");
    expect(bodyOf(await runTabs(session, { op: "close" })).error).toBe("op=close requires tab_id");
  });
  it("returns the session's refusal as a tool error, not a throw", async () => {
    const select = await runTabs(session, { op: "select", tab_id: "tab9" });
    expect(select.isError).toBe(true);
    expect(bodyOf(select).error).toBe("no such tab: tab9");
    expect(bodyOf(await runTabs(session, { op: "close", tab_id: "tab1" })).error).toBe("cannot close the last tab");
  });
});

describe("browser_upload guards", () => {
  it("refuses a ref that no longer resolves to an element as stale_ref", async () => {
    const dir = mkdtempSync(join(tmpdir(), "bmcp-up-"));
    try {
      writeFileSync(join(dir, "a.txt"), "a");
      process.env.BROWSER_UPLOAD_DIR = dir;
      const setInputFiles = vi.fn();
      const session = {
        ensurePage: async () => undefined,
        resolveRef: () => ({ evaluate: async () => { throw new Error("detached"); }, setInputFiles }),
        checkCaptcha: async () => "none" as const,
      } as unknown as Session;
      await expect(runUpload(session, { ref: "e3", paths: [join(dir, "a.txt")] })).rejects.toThrow(/^stale_ref: e3/);
      expect(setInputFiles).not.toHaveBeenCalled();
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
  it("validates paths before acquiring a page", async () => {
    const ensurePage = vi.fn();
    const res = await runUpload({ ensurePage } as unknown as Session, { ref: "e1", paths: ["/etc/passwd"] });
    expect(bodyOf(res).error).toMatch(/uploads are disabled/);
    expect(ensurePage).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// browser_act

// A keyboard that refuses keys Playwright does not know, as Playwright does.
const KNOWN_KEYS = new Set(["Control", "Shift", "Alt", "Meta", "Enter", "\n", "ArrowDown", "Delete", "a", "b"]);
function fakeKeyboard() {
  const check = (verb: string, key: string) => { if (!KNOWN_KEYS.has(key)) throw new Error(`keyboard.${verb}: Unknown key: "${key}"`); };
  return {
    down: vi.fn(async (key: string) => check("down", key)),
    up: vi.fn(async (key: string) => check("up", key)),
    type: vi.fn(async (_text: string) => {}),
  };
}

function actSession(locator: Record<string, unknown>, keyboard = fakeKeyboard()): Session {
  // A ref is focused with an empty type before its keys go out.
  const target = { pressSequentially: async () => {}, ...locator };
  return {
    ensurePage: async () => ({ url: () => "http://h/", keyboard }),
    resolveRef: () => target,
    beginWatch: async () => {},
    disposeWatch: async () => {},
    invalidateScreenshot: () => {},
    allowScreenshotRevalidation: () => {},
    settle: async () => ({ urlChanged: false, domMutated: "minor" }),
    eventMark: () => 0,
    eventsSince: () => [],
    settleAdoptions: async () => {},
    checkCaptcha: async () => "none" as const,
  } as unknown as Session;
}

describe("browser_act type budget", () => {
  it("scales the ref timeout with the value so a long value is not cut off as stale_ref", async () => {
    const pressSequentially = vi.fn(async () => {});
    const keyboard = fakeKeyboard();
    const value = "x".repeat(3000);
    const res = await runAct(actSession({ pressSequentially }, keyboard), { kind: "type", ref: "e1", value });
    expect(bodyOf(res).ok).toBe(true);
    expect(pressSequentially).toHaveBeenCalledWith("", { timeout: typeTimeoutMs(value) });
    expect(keyboard.type).toHaveBeenCalledTimes(3000);
    expect(typeTimeoutMs(value)).toBe(2000 + 3000 * TYPE_MS_PER_CHAR);
    expect(typeTimeoutMs("")).toBe(2000);
  });
  it("still maps an actionability timeout on a ref to stale_ref", async () => {
    const click = vi.fn(async () => { throw new Error("locator.click: Timeout 2000ms exceeded."); });
    const res = await runAct(actSession({ click }), { kind: "click", ref: "e7" });
    expect(res.isError).toBe(true);
    expect(texts(res)[0]).toMatch(/stale_ref: e7/);
  });
  it("maps a timeout to stale_ref only when the ref no longer resolves", async () => {
    const click = vi.fn(async () => { throw new Error("locator.click: Timeout 2000ms exceeded.\nCall log:\n  - waiting for locator('aria-ref=e7')"); });
    const count = vi.fn(async () => 0);
    const res = await runAct(actSession({ click, count }), { kind: "click", ref: "e7" });
    expect(bodyOf(res).error).toMatch(/^stale_ref: e7/);
    expect(count).toHaveBeenCalled();
  });
  it("reports a still-resolving ref that cannot be clicked as not_actionable, not stale_ref", async () => {
    const message = [
      "locator.click: Timeout 2000ms exceeded.",
      "Call log:",
      "\x1b[2m  - waiting for locator('aria-ref=e7')\x1b[22m",
      "\x1b[2m    - locator resolved to <button id=\"d\" role=\"menuitem\">Docs</button>\x1b[22m",
      "\x1b[2m    2 × waiting for element to be visible, enabled and stable\x1b[22m",
      "\x1b[2m      - element is visible, enabled and stable\x1b[22m",
      "\x1b[2m      - <div class=\"scrim\"></div> intercepts pointer events\x1b[22m",
      "\x1b[2m    - retrying click action\x1b[22m",
    ].join("\n");
    const click = vi.fn(async () => { throw new Error(message); });
    const res = await runAct(actSession({ click, count: async () => 1 }), { kind: "click", ref: "e7" });
    const error = String(bodyOf(res).error);
    expect(error).toMatch(/^not_actionable: e7 is still valid/);
    expect(error).toContain('<div class="scrim"></div> intercepts pointer events');
    expect(error).not.toContain("stale_ref");
    expect(error).not.toContain("\x1b");
  });
  it("rejects an unknown ref before dispatch and clears any passive baseline", async () => {
    const beginWatch = vi.fn(async () => {});
    const disposeWatch = vi.fn(async () => {});
    const session = {
      ...actSession({}),
      beginWatch, disposeWatch,
      resolveRef: async (ref: string) => { throw new StaleRefError(ref); },
      checkCaptcha: async () => "none" as const,
    } as unknown as Session;
    const res = await runAct(session, { kind: "click", ref: "e404" });
    expect(bodyOf(res).error).toMatch(/^stale_ref: e404/);
    expect(beginWatch).not.toHaveBeenCalled();
    expect(disposeWatch).toHaveBeenCalledOnce();
  });
  it("reports a missing value as a plain error", async () => {
    const res = await runAct(actSession({ fill: vi.fn() }), { kind: "fill", ref: "e1" });
    expect(bodyOf(res)).toMatchObject({ code: "invalid_input", error: "invalid_input: `fill` requires `value`", outcome: "not_executed" });
  });
});

describe("browser_act outcomes", () => {
  it("never calls a half-typed value retryable: a timeout without a named blocker is outcome_unknown", async () => {
    const pressSequentially = vi.fn(async () => { throw new Error("locator.pressSequentially: Timeout 2100ms exceeded."); });
    const res = await runAct(actSession({ pressSequentially, count: async () => 1 }), { kind: "type", ref: "e1", value: "hello" });
    expect(bodyOf(res)).toMatchObject({ ok: false, code: "outcome_unknown", outcome: "unknown" });
    expect(String(bodyOf(res).error)).toMatch(/do not repeat it blindly/);
  });
  it("keeps a typing timeout that names a blocker as not_actionable (nothing was typed)", async () => {
    const pressSequentially = vi.fn(async () => {
      throw new Error("locator.pressSequentially: Timeout 2100ms exceeded.\nCall log:\n  - element is not visible");
    });
    const res = await runAct(actSession({ pressSequentially, count: async () => 1 }), { kind: "type", ref: "e1", value: "hello" });
    expect(bodyOf(res)).toMatchObject({ code: "not_actionable", outcome: "not_executed" });
  });
  it("reports a typing timeout as outcome_unknown when only the typed text names a blocker", async () => {
    const value = "the field is disabled";
    const pressSequentially = vi.fn(async () => {
      throw new Error(`locator.pressSequentially: Timeout 2100ms exceeded.\nCall log:\n  - elementHandle.type("${value}")`);
    });
    const res = await runAct(actSession({ pressSequentially, count: async () => 1 }), { kind: "type", ref: "e1", value });
    expect(bodyOf(res)).toMatchObject({ ok: false, code: "outcome_unknown", outcome: "unknown" });
  });
  // Playwright's call log, as it reads when the 2s ref budget ends at each step.
  const log = (method: string, ...lines: string[]) => [`locator.${method}: Timeout 2000ms exceeded.`, "Call log:", "  - waiting for locator('aria-ref=e7')", "    - locator resolved to <button>Save</button>", ...lines.map(line => `  - ${line}`)].join("\n");
  const ready = ["attempting click action", "waiting for element to be visible, enabled and stable", "element is visible, enabled and stable"];
  const editable = "waiting for element to be visible, enabled and editable";
  it("counts a click Playwright confirmed as done when only the navigation wait ran out", async () => {
    const click = vi.fn(async () => { throw new Error(log("click", ...ready, "performing click action", "click action done", "waiting for scheduled navigations to finish")); });
    expect(bodyOf(await runAct(actSession({ click, count: async () => 1 }), { kind: "click", ref: "e7" }))).toMatchObject({ ok: true });
  });
  it.each<[string, string, Record<string, unknown>, string[]]>([
    ["a click sent but not confirmed", "click", { kind: "click" }, [...ready, "performing click action"]],
    ["a fill the page never answered", "fill", { kind: "fill", value: "x" }, ['fill("x")', "attempting fill action", editable]],
    ["a check whose click went out", "setChecked", { kind: "check" }, [...ready, "performing click action", "click action done"]],
    ["a fill on a field that turned enabled and then never answered", "fill", { kind: "fill", value: "x" }, ['fill("x")', "attempting fill action", editable, "element is not enabled", "retrying fill action", "waiting 20ms", editable]],
    ["a fill of a single line break", "fill", { kind: "fill", value: "\n" }, ['fill("\n")', "attempting fill action", editable]],
    ["a fill whose text reads like a refusal", "fill", { kind: "fill", value: "x\n  - element is not enabled" }, ['fill("x\n  - element is not enabled")', "attempting fill action", editable]],
  ])("reports a timeout after %s as outcome_unknown", async (_name, method, input, lines) => {
    const fail = vi.fn(async () => { throw new Error(log(method, ...lines)); });
    const res = await runAct(actSession({ [method]: fail, count: async () => 1 }), { ...input, ref: "e7" } as ActInput);
    expect(bodyOf(res)).toMatchObject({ ok: false, code: "outcome_unknown", outcome: "unknown" });
    expect(fail).toHaveBeenCalledOnce();
  });
  it.each<[string, string, Record<string, unknown>, string[]]>([
    ["a click Playwright withheld and retried", "click", { kind: "click" }, [...ready, "performing click action", "<div></div> intercepts pointer events", "retrying click action", "waiting 500ms"]],
    ["a fill on a field that stays disabled", "fill", { kind: "fill", value: "x" }, ['fill("x")', "attempting fill action", editable, "element is not enabled", "retrying fill action", "waiting 500ms"]],
  ])("keeps a timeout after %s as not_actionable", async (_name, method, input, lines) => {
    const fail = vi.fn(async () => { throw new Error(log(method, ...lines)); });
    const res = await runAct(actSession({ [method]: fail, count: async () => 1 }), { ...input, ref: "e7" } as ActInput);
    expect(bodyOf(res)).toMatchObject({ ok: false, code: "not_actionable", outcome: "not_executed" });
  });
  // The value comes back in Playwright's log and must not be read as a step.
  it.each(["2026\n  - performing click action\n", "2026\u001b[31m\n  - performing click action\n", "Timeout 2000ms exceeded"])("keeps a malformed value that reads like a log line as invalid_input (%j)", async value => {
    const fill = vi.fn(async () => { throw new Error(["locator.fill: Error: Malformed value", "Call log:", `  - fill("${value}")`, "  - attempting fill action", `  - ${editable}`].join("\n")); });
    const res = await runAct(actSession({ fill, count: async () => 1 }), { kind: "fill", ref: "e7", value });
    expect(bodyOf(res)).toMatchObject({ ok: false, code: "invalid_input", outcome: "not_executed" });
  });
  const chordFirst = (chord: string) => { let key = ""; for (const char of chord) { if (char === "+" && key) break; key += char; } return key; };
  const stepLike = "\n  - performing click action\n";
  // Playwright stops a chord at the first key it does not know: nothing is down yet.
  it.each<[string, Record<string, unknown>, string]>([
    ["alone", { key: "Foo" }, "Foo"],
    ["first in its chord", { key: "Foo+a" }, "Foo+a"],
    ["first in a chord that reads like a log line", { key: `${stepLike}+Enter` }, `${stepLike}+Enter`],
    // `value` is not part of a key press, so it must not change what is cut.
    ["that ends in an unused value", { key: `${stepLike}Enter`, value: "Enter" }, `${stepLike}Enter`],
  ])("keeps an unknown key %s as invalid_input", async (_name, input, chord) => {
    const keyboard = fakeKeyboard();
    const res = await runAct(actSession({ count: async () => 1 }, keyboard), { kind: "key", ref: "e7", ...input } as ActInput);
    expect(bodyOf(res)).toMatchObject({ ok: false, code: "invalid_input", outcome: "not_executed" });
    expect(keyboard.down.mock.calls.map(call => call[0])).toEqual([chordFirst(chord)]);
  });
  // The keys before the unknown one are already down.
  it.each<[string, Record<string, unknown>]>([
    ["a modifier", { key: "Foo", modifiers: ["Control"] }],
    ["a key", { key: "a+Foo" }],
    ["a key it starts with", { key: 'a+a"' }],
  ])("reports an unknown key pressed after %s as outcome_unknown", async (_name, input) => {
    const res = await runAct(actSession({ count: async () => 1 }), { kind: "key", ref: "e7", ...input } as ActInput);
    expect(bodyOf(res)).toMatchObject({ ok: false, code: "outcome_unknown", outcome: "unknown" });
    expect(String(bodyOf(res).error)).toContain("Unknown key");
  });
  // A failed key press releases the keys whose keydown it sent, and only those
  // (#1423). A release that fails does not replace the press's own error.
  const focusTimeout = ["locator.pressSequentially: Timeout 2000ms exceeded.", "Call log:", "  - waiting for locator('aria-ref=e7')", '  - elementHandle.type("")'].join("\n");
  it.each<[string, Record<string, unknown>, { focus?: string; hangOn?: string }, string[], string]>([
    ["the modifier before an unknown last key", { key: "Foo", modifiers: ["Control"] }, {}, ["Control"], "outcome_unknown"],
    ["the key before an unknown last key", { key: 'a+a"' }, {}, ["a"], "outcome_unknown"],
    ["nothing for an unknown first key", { key: "Foo+a" }, {}, [], "invalid_input"],
    ["every key of a chord whose last keydown the page never answered", { key: "a", modifiers: ["Control", "Shift"] }, { hangOn: "a" }, ["a", "Shift", "Control"], "outcome_unknown"],
    ["only the keys sent down when a modifier's keydown was never answered", { key: "a", modifiers: ["Control", "Shift"] }, { hangOn: "Shift" }, ["Shift", "Control"], "outcome_unknown"],
    ["a line break key the page never answered", { key: "\n" }, { hangOn: "\n" }, ["\n"], "outcome_unknown"],
    ["nothing when focusing the ref timed out", { key: "Delete", modifiers: ["Control"] }, { focus: focusTimeout }, [], "outcome_unknown"],
  ])("releases %s", async (_name, input, fail, released, code) => {
    const pressSequentially = vi.fn(async () => { if (fail.focus) throw new Error(fail.focus); });
    const keyboard = fakeKeyboard();
    const known = keyboard.down.getMockImplementation()!;
    keyboard.down.mockImplementation(async (key: string) => { if (key === fail.hangOn) await new Promise(() => {}); return known(key); });
    keyboard.up.mockImplementation(async () => { throw new Error("Target closed"); });
    const res = await runAct(actSession({ pressSequentially, count: async () => 1 }, keyboard), { kind: "key", ref: "e7", ...input } as ActInput);
    expect(keyboard.up.mock.calls.map(call => call[0])).toEqual(released);
    expect(bodyOf(res)).toMatchObject({ ok: false, code });
    expect(String(bodyOf(res).error)).not.toContain("Target closed");
    if (fail.focus) expect(keyboard.down).not.toHaveBeenCalled();
  }, 10000);
  it("presses a chord on a ref down in order and up in reverse, all at page level", async () => {
    const keyboard = fakeKeyboard();
    const order: string[] = [];
    keyboard.down.mockImplementation(async (key: string) => { order.push(`down ${key}`); });
    keyboard.up.mockImplementation(async (key: string) => { order.push(`up ${key}`); });
    const res = await runAct(actSession({ count: async () => 1 }, keyboard), { kind: "key", ref: "e7", key: "a", modifiers: ["Control", "Shift"] });
    expect(bodyOf(res).ok).toBe(true);
    expect(order).toEqual(["down Control", "down Shift", "down a", "up a", "up Shift", "up Control"]);
  });
  it("types a ref one character per call and stops the text where the page stopped answering", async () => {
    const keyboard = fakeKeyboard();
    keyboard.type.mockImplementation(async (char: string) => { if (char === "b") await new Promise(() => {}); });
    const res = await runAct(actSession({ count: async () => 1 }, keyboard), { kind: "type", ref: "e7", value: "abc" });
    expect(bodyOf(res)).toMatchObject({ ok: false, code: "outcome_unknown", outcome: "unknown" });
    expect(String(bodyOf(res).error)).toContain("1 of 3 characters typed");
    expect(keyboard.type.mock.calls.map(call => call[0])).toEqual(["a", "b"]);
    expect(keyboard.up).not.toHaveBeenCalled();
  }, 10000);
  // Words the caller typed are not Playwright's verdict on the input.
  it.each(["Unknown key", "Malformed value", "Element is not a <select> element"])("reports typing %j into a page that never answered as outcome_unknown", async value => {
    const pressSequentially = vi.fn(async () => { throw new Error(["locator.pressSequentially: Timeout 2000ms exceeded.", "Call log:", "  - waiting for locator('aria-ref=e7')", `  - elementHandle.type("${value}")`].join("\n")); });
    const res = await runAct(actSession({ pressSequentially, count: async () => 1 }), { kind: "type", ref: "e7", value });
    expect(bodyOf(res)).toMatchObject({ ok: false, code: "outcome_unknown", outcome: "unknown" });
  });
  // Playwright escapes "\n" in the page text it quotes but not "\r" or U+2028,
  // so only "\n" ends one of its lines.
  it.each(["\r", " ", " "])("does not read a step out of a label that holds %j", async separator => {
    const label = `x${separator}  - performing click action${separator}`;
    const click = vi.fn(async () => { throw new Error(["locator.click: Timeout 2000ms exceeded.", "Call log:", `  - waiting for getByRole('option', { name: '${label}', exact: true })`].join("\n")); });
    const option = { first: () => ({ waitFor: async () => {} }), count: async () => 1, click };
    const frame = { locator: () => ({}) };
    const locator = {
      evaluate: async () => ({ native: false, multiple: false, role: "listbox", controls: null, options: [] }),
      elementHandle: async () => ({ ownerFrame: async () => frame, dispose: async () => {} }),
      getByRole: () => option,
      count: async () => 1,
    };
    const res = await runAct(actSession(locator), { kind: "select", ref: "e7", values: [label] });
    expect(bodyOf(res)).toMatchObject({ ok: false, outcome: "not_executed" });
    expect(click).toHaveBeenCalledOnce();
  });
  it("does not read a step log out of an error the tool raised itself", async () => {
    const label = "x\n  - performing click action\n";
    const evaluate = vi.fn(async () => { throw new Error(`invalid_input: no visible option labelled "${label}"; inspect the open list and use its exact label`); });
    const res = await runAct(actSession({ evaluate, count: async () => 1 }), { kind: "select", ref: "e7", values: [label] });
    expect(bodyOf(res)).toMatchObject({ ok: false, code: "invalid_input", outcome: "not_executed" });
  });
  it("reports a coordinate gesture that failed after input was sent as outcome_unknown", async () => {
    const session = {
      ...actSession({}),
      hasValidScreenshot: () => true,
      toViewportPoint: async (c: [number, number]) => c,
      ensurePage: async () => ({ url: () => "http://h/", mouse: { click: async () => { throw new Error("Protocol error: Input.dispatchMouseEvent: socket hang up"); } } }),
      checkCaptcha: async () => "none" as const,
    } as unknown as Session;
    const res = await runAct(session, { kind: "click", coordinate: [5, 5] });
    expect(bodyOf(res)).toMatchObject({ code: "outcome_unknown", outcome: "unknown" });
  });
  it("rejects ref-only kinds on a coordinate before touching the page", async () => {
    const ensurePage = vi.fn();
    for (const kind of ["select", "check", "uncheck"] as const) {
      const res = await runAct({ ensurePage } as unknown as Session, { kind, coordinate: [1, 1], value: "x" });
      expect(bodyOf(res)).toMatchObject({ code: "invalid_input", outcome: "not_executed" });
    }
    expect(ensurePage).not.toHaveBeenCalled();
  });
  it("sets checkbox state with setChecked instead of toggling", async () => {
    const setChecked = vi.fn(async () => {});
    expect(bodyOf(await runAct(actSession({ setChecked }), { kind: "check", ref: "e3" })).ok).toBe(true);
    expect(bodyOf(await runAct(actSession({ setChecked }), { kind: "uncheck", ref: "e3" })).ok).toBe(true);
    expect(setChecked.mock.calls.map((c) => (c as unknown[])[0])).toEqual([true, false]);
  });
  it("reports page events (new tabs, downloads, dialogs) that happened during the action", async () => {
    const session = {
      ...actSession({ click: async () => {} }),
      eventsSince: () => [{ type: "new_tab", tab_id: "tab2", url: "https://x.test/" }],
      checkCaptcha: async () => "none" as const,
    } as unknown as Session;
    expect(bodyOf(await runAct(session, { kind: "click", ref: "e1" })).events).toEqual([{ type: "new_tab", tab_id: "tab2", url: "https://x.test/" }]);
  });
  it("rejects coordinates bound to an older screenshot and attaches a fresh one", async () => {
    const click = vi.fn();
    const session = {
      ...actSession({}),
      latestScreenshotId: () => "s5",
      hasValidScreenshot: () => true,
      currentTabId: () => "tab1",
      currentUrl: () => "http://h/",
      bringToFront: async () => true,
      setLastScreenshot: () => "s6",
      ensurePage: async () => ({
        url: () => "http://h/",
        mouse: { click },
        evaluate: async () => ({ scrollW: 800, scrollH: 600, innerW: 800, innerH: 600, scrollX: 0, scrollY: 0 }),
        screenshot: async () => Buffer.from("jpeg"),
      }),
      checkCaptcha: async () => "none" as const,
    } as unknown as Session;
    const res = await runAct(session, { kind: "click", coordinate: [5, 5], screenshot_id: "s4" });
    expect(bodyOf(res)).toMatchObject({ code: "stale_screenshot", outcome: "not_executed" });
    expect(res.content.some((c) => c.type === "image")).toBe(true);
    expect(click).not.toHaveBeenCalled();
  });
});

describe("error contract", () => {
  it("derives a stable code from the message prefix", () => {
    expect(bodyOf(errorResult("stale_ref: e1 is gone"))).toMatchObject({ ok: false, code: "stale_ref" });
    expect(bodyOf(errorResult("no prefix here")).code).toBeUndefined();
  });
});

describe("dialog gate", () => {
  const pending = { type: "confirm", message: "Delete?", tab_id: "tab1", default_value: "", dialog: {} };
  function gated(extra: Record<string, unknown> = {}): Session {
    return {
      pendingDialog: pending,
      describeDialog: () => ({ type: "confirm", message: "Delete?", tab_id: "tab1" }),
      ...extra,
      checkCaptcha: async () => "none" as const,
    } as unknown as Session;
  }
  it("refuses page work while a dialog waits, before running anything", async () => {
    const ensurePage = vi.fn();
    for (const [name, input] of [["browser_snapshot", {}], ["browser_act", { kind: "click", ref: "e1" }], ["browser_batch", { actions: ['{"tool":"browser_snapshot"}'] }]] as const) {
      const res = await TOOL_MAP.get(name)!.run(gated({ ensurePage }), input);
      expect(bodyOf(res)).toMatchObject({ ok: false, code: "dialog_open", outcome: "not_executed", dialog: { type: "confirm", message: "Delete?" } });
    }
    expect(ensurePage).not.toHaveBeenCalled();
  });
  it("lets the answer through and reports when no dialog is waiting", async () => {
    const res = await TOOL_MAP.get("browser_act")!.run({ pendingDialog: undefined } as unknown as Session, { kind: "dismiss_dialog" });
    expect(bodyOf(res)).toMatchObject({ code: "no_dialog", outcome: "not_executed" });
  });
});

// ---------------------------------------------------------------------------
// browser_navigate: a redirect onto a refused hop

describe("browser_navigate redirect onto a refused address", () => {
  function redirectSession(finalUrl: string, headers: Record<string, string>, reason?: string): Session {
    const page = {
      url: () => finalUrl,
      goto: async () => ({ headers: () => headers, url: () => finalUrl }),
      title: async () => "",
      locator: () => ({ first: () => ({ isVisible: async () => true }) }),
    };
    return {
      ensurePage: async () => page,
      notifyNavigation: () => {},
      ssrfBlockReason: (host: string) => (reason && host === "127.0.0.2" ? reason : undefined),
      checkCaptcha: async () => "none" as const,
    } as unknown as Session;
  }

  it("names the refused hop, not the public page that redirected to it", async () => {
    const session = redirectSession("http://127.0.0.2:8000/admin", { "x-browser-egress": "blocked" }, "IPv4 loopback: blocked");
    const err = await runNavigate(session, { url: "http://public.example/start" }).catch((e: Error) => e);
    expect(err).toBeInstanceOf(Error);
    const message = (err as Error).message;
    expect(message).toMatch(/^blocked by the browser's egress proxy: http:\/\/127\.0\.0\.2:8000\/admin is outside/);
    expect(message).not.toContain("public.example");
    expect(message).toContain("Policy detail: IPv4 loopback: blocked");
  });

  it.each(["back", "forward"])("normalizes %s history navigation failures", async direction => {
    const page = {
      url: () => "https://blocked.example/",
      _timeoutSettings: { navigationTimeout: () => 30000 },
      goBack: async () => { throw new Error("net::ERR_BLOCKED_BY_CLIENT"); },
      goForward: async () => { throw new Error("net::ERR_BLOCKED_BY_CLIENT"); },
    };
    const session = {
      ensurePage: async () => page, notifyNavigation: () => {},
      ssrfBlockReason: () => "blocked destination",
    } as unknown as Session;
    await expect(runNavigate(session, { url: direction })).rejects.toThrow("blocked by SSRF policy: blocked destination");
  });
  it("passes a navigation timeout through unchanged", async () => {
    const page = {
      goto: async () => { throw new Error("page.goto: Timeout 30000ms exceeded."); },
    };
    const session = {
      ensurePage: async () => page,
      notifyNavigation: () => {},
      ssrfBlockReason: () => undefined,
      checkCaptcha: async () => "none" as const,
    } as unknown as Session;
    await expect(runNavigate(session, { url: "https://slow.example/" })).rejects.toThrow("page.goto: Timeout 30000ms exceeded.");
  });

  it("names the SSRF guard's reason for a hop the guard aborted", async () => {
    const page = {
      goto: async () => { throw new Error("page.goto: net::ERR_BLOCKED_BY_CLIENT at https://public.example/"); },
    };
    const session = {
      ensurePage: async () => page,
      notifyNavigation: () => {},
      ssrfBlockReason: () => "169.254.169.254: cloud metadata",
      checkCaptcha: async () => "none" as const,
    } as unknown as Session;
    await expect(runNavigate(session, { url: "https://public.example/" })).rejects.toThrow(
      "blocked by SSRF policy: 169.254.169.254: cloud metadata",
    );
  });
});

// ---------------------------------------------------------------------------
// panel control: page script that never returns

describe("panel edit and upload are bounded", () => {
  let seq = 0;
  const command = (ws: BrowserWorkspace, op: string, args: Record<string, unknown>, tab: string | null = "tab1") => ({
    id: `h${++seq}`, runtime_id: ws.runtimeId, tab_id: tab, viewer_id: "alice", op, args, expires_at: Date.now() + 60000,
  });

  it("an edit whose page script never returns fails and frees the queue", async () => {
    vi.useFakeTimers();
    const session = new Session(new BrowserManager());
    const ws = new BrowserWorkspace(session);
    try {
      expect((await ws.receive(command(ws, "takeover", {}, null))).ok).toBe(true);
      const page = { evaluate: vi.fn(() => new Promise(() => {})) };
      vi.spyOn(session, "currentTabId").mockReturnValue("tab1");
      vi.spyOn(session, "hasPage").mockReturnValue(true);
      vi.spyOn(session, "ensurePage").mockResolvedValue(page as never);
      const edit = ws.receive(command(ws, "edit", { field: "f-1", action: "focus" }));
      await vi.advanceTimersByTimeAsync(EDIT_TIMEOUT_MS);
      const result = await edit;
      expect(result).toMatchObject({ ok: false, error: "edit timed out; outcome unknown, do not replay" });
      // The queue is free: a later operation runs.
      expect(await session.runExclusive(async () => "next")).toBe("next");
    } finally {
      ws.stop();
    }
  });

  it("an upload whose field lookup never returns fails without transferring", async () => {
    vi.useFakeTimers();
    const dir = mkdtempSync(join(tmpdir(), "bmcp-panel-up-"));
    try {
      writeFileSync(join(dir, "f.txt"), "f");
      process.env.AIS_BROWSER_PANEL_UPLOAD_DIR = dir;
      const page = { evaluateHandle: vi.fn(() => new Promise(() => {})), evaluate: vi.fn() };
      const upload = uploadField(page as never, { field: "f-1", paths: [join(dir, "f.txt")] });
      const outcome = expect(upload).rejects.toThrow("file field lookup timed out");
      await vi.advanceTimersByTimeAsync(FILE_FIELD_TIMEOUT_MS);
      await outcome;
      expect(page.evaluate).not.toHaveBeenCalled();
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it("a field handle that arrives after the deadline is disposed", async () => {
    vi.useFakeTimers();
    const dir = mkdtempSync(join(tmpdir(), "bmcp-panel-up-"));
    try {
      writeFileSync(join(dir, "f.txt"), "f");
      process.env.AIS_BROWSER_PANEL_UPLOAD_DIR = dir;
      let deliver!: (h: unknown) => void;
      const late = { dispose: vi.fn(async () => {}), asElement: () => null };
      const page = { evaluateHandle: vi.fn(() => new Promise((r) => { deliver = r; })) };
      const upload = uploadField(page as never, { field: "f-1", paths: [join(dir, "f.txt")] });
      const outcome = expect(upload).rejects.toThrow("file field lookup timed out");
      await vi.advanceTimersByTimeAsync(FILE_FIELD_TIMEOUT_MS);
      await outcome;
      deliver(late);
      await vi.advanceTimersByTimeAsync(0);
      expect(late.dispose).toHaveBeenCalledOnce();
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
});
