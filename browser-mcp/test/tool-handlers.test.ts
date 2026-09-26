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
import { Session } from "../src/session.js";
import { BrowserManager } from "../src/browser.js";
import { runNetwork, NETWORK_BODY_TIMEOUT_MS } from "../src/tools/network.js";
import { runReadText, READ_TEXT_MAX_CHARS } from "../src/tools/read_text.js";
import { runEval, EVAL_MAX_CHARS } from "../src/tools/eval.js";
import { runConsole } from "../src/tools/console.js";
import { runTabs } from "../src/tools/tabs.js";
import { runUpload } from "../src/tools/upload.js";
import { runAct, typeTimeoutMs, TYPE_MS_PER_CHAR } from "../src/tools/act.js";
import { runNavigate } from "../src/tools/navigate.js";
import { capText, type McpToolResult } from "../src/tools/result.js";
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
    return {
      ensurePage: async () => undefined,
      evaluate: async () => ({ title: "Long", url: "http://h/long", text }),
    } as unknown as Session;
  }
  it("keeps the tail note and reports the returned length", async () => {
    const res = await runReadText(readSession("word ".repeat(READ_TEXT_MAX_CHARS)));
    const body = bodyOf(res);
    expect(body).toMatchObject({ ok: true, truncated: true, chars: READ_TEXT_MAX_CHARS });
    expect(texts(res)[1]).toMatch(/\n\[truncated: full text is 150000 chars\]$/);
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

function actSession(locator: Record<string, unknown>): Session {
  return {
    ensurePage: async () => ({ url: () => "http://h/" }),
    resolveRef: () => locator,
    beginWatch: async () => {},
    disposeWatch: async () => {},
    invalidateScreenshot: () => {},
    settle: async () => ({ urlChanged: false, domMutated: "minor" }),
  } as unknown as Session;
}

describe("browser_act type budget", () => {
  it("scales the ref timeout with the value so a long value is not cut off as stale_ref", async () => {
    const pressSequentially = vi.fn(async () => {});
    const value = "x".repeat(3000);
    const res = await runAct(actSession({ pressSequentially }), { kind: "type", ref: "e1", value });
    expect(bodyOf(res).ok).toBe(true);
    expect(pressSequentially).toHaveBeenCalledWith(value, { timeout: typeTimeoutMs(value) });
    expect(typeTimeoutMs(value)).toBe(2000 + 3000 * TYPE_MS_PER_CHAR);
    expect(typeTimeoutMs("")).toBe(2000);
  });
  it("still maps an actionability timeout on a ref to stale_ref", async () => {
    const click = vi.fn(async () => { throw new Error("locator.click: Timeout 2000ms exceeded."); });
    const res = await runAct(actSession({ click }), { kind: "click", ref: "e7" });
    expect(res.isError).toBe(true);
    expect(texts(res)[0]).toMatch(/stale_ref: e7/);
  });
  it("reports a missing value as a plain error", async () => {
    const res = await runAct(actSession({ fill: vi.fn() }), { kind: "fill", ref: "e1" });
    expect(bodyOf(res).error).toBe("`fill` requires `value`");
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

  it("passes a navigation timeout through unchanged", async () => {
    const page = {
      goto: async () => { throw new Error("page.goto: Timeout 30000ms exceeded."); },
    };
    const session = {
      ensurePage: async () => page,
      notifyNavigation: () => {},
      ssrfBlockReason: () => undefined,
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
