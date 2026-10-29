/**
 * The UI flight recorder in a real Chromium: the in-page recorder on its own
 * (detectors, page hooks, persistence across full loads) and browser_record
 * through a real Session (auto marks, throttling, keyframes, expiry).
 * Skipped when no Chromium is discoverable, like the other *.browser tests.
 */
import { describe, it, expect, beforeAll, afterAll, beforeEach, afterEach } from "vitest";
import { spawn, type ChildProcess } from "node:child_process";
import { createServer } from "node:net";
import { createServer as createHttpServer, type Server } from "node:http";
import { existsSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { chromium, type Browser, type Page } from "playwright-core";
import { stopBrowser } from "./browser-process.js";
import { BrowserManager } from "../src/browser.js";
import { Session } from "../src/session.js";
import { TOOL_MAP } from "../src/tools/index.js";
import { activeRecording } from "../src/tools/record.js";
import type { McpToolResult } from "../src/tools/result.js";
import { recorderSource, resumeScript } from "../src/recorder/driver.js";

const browserPath = process.env.CHROMIUM_PATH || [
  "/usr/bin/chromium", "/usr/bin/chromium-browser", "/usr/bin/google-chrome-stable",
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  "C:/Program Files/Google/Chrome/Application/chrome.exe",
].find((path) => existsSync(path));

/** A tiny app: a [data-ui-state] carrier plus window helpers that misbehave on purpose. */
const APP = `<!doctype html><html><head><title>recorder fixture</title><style>
  body { margin: 0; font: 16px sans-serif; } #hero { height: 120px; } .row { height: 30px; }
</style></head><body>
<main id="view" data-ui-state="chat" data-ui-mount="ChatView" data-ui-surface="new" data-ui-composer="centre" data-ui-messages="0" data-ui-loading="false">
  <section id="hero" data-ui-mount="Hero">Let's build</section>
  <div id="list"></div>
</main>
<script>
  const view = document.getElementById('view');
  const frame = () => new Promise((r) => requestAnimationFrame(() => r()));
  const set = (k, v) => { view.dataset[k] = v; };
  window.clean = async () => {
    await frame(); set('uiLoading', 'true'); set('uiSurface', 'loading'); set('uiComposer', 'bottom');
    document.getElementById('hero').remove();
    await frame(); await frame();
    set('uiSurface', 'active'); set('uiMessages', '1'); set('uiLoading', 'false');
    const row = document.createElement('div'); row.className = 'row'; row.dataset.uiRow = 'm1'; row.textContent = 'hi';
    document.getElementById('list').append(row);
    await frame(); await frame();
  };
  window.flicker = async () => {
    await frame(); set('uiSurface', 'active'); set('uiComposer', 'bottom');
    await frame(); set('uiSurface', 'new'); set('uiComposer', 'centre');
    await frame(); await frame();
  };
  window.remount = async () => {
    const hero = document.getElementById('hero');
    await frame(); hero.remove();
    await frame(); view.prepend(hero);
    await frame(); await frame();
  };
  window.jump = () => new Promise((r) => setTimeout(() => {
    const banner = document.createElement('div'); banner.style.height = '300px'; banner.textContent = 'late banner';
    document.body.prepend(banner);
    setTimeout(r, 200);
  }, 50));
  window.duplicate = async () => {
    for (const id of ['m1', 'm1']) { const row = document.createElement('div'); row.dataset.uiRow = id; document.getElementById('list').append(row); }
    await frame(); await frame();
  };
</script></body></html>`;

function startServer(): Promise<{ server: Server; port: number }> {
  const server = createHttpServer((req, res) => {
    const path = (req.url || "/").split("?")[0];
    if (path === "/ok") { res.writeHead(200, { "content-type": "application/json" }); res.end("{}"); return; }
    if (path === "/missing") { res.writeHead(404); res.end("nope"); return; }
    if (path === "/boom") { res.writeHead(500); res.end("boom"); return; }
    if (path === "/slow") { setTimeout(() => { res.writeHead(200); res.end("late"); }, 3000); return; }
    res.writeHead(200, { "content-type": "text/html" });
    res.end(path === "/next"
      ? APP.replace('data-ui-surface="new"', 'data-ui-surface="active"').replace("recorder fixture", "next page")
      : APP);
  });
  return new Promise((resolve) => server.listen(0, "127.0.0.1", () => {
    const address = server.address();
    resolve({ server, port: typeof address === "object" && address ? address.port : 0 });
  }));
}

function freePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const srv = createServer();
    srv.listen(0, "127.0.0.1", () => {
      const addr = srv.address();
      const port = addr && typeof addr === "object" ? addr.port : 0;
      srv.close(() => (port ? resolve(port) : reject(new Error("no port"))));
    });
  });
}

async function waitForCdp(port: number): Promise<void> {
  const deadline = Date.now() + 15000;
  while (Date.now() < deadline) {
    try { if ((await fetch(`http://127.0.0.1:${port}/json/version`)).ok) return; } catch { /* starting */ }
    await new Promise((r) => setTimeout(r, 150));
  }
  throw new Error("CDP endpoint did not come up");
}

type Recording = {
  ok: boolean; id: string; text: string; anomalies: Array<{ kind: string; field?: string; name?: string; message: string; flicker?: boolean }>;
  timeline: Array<Record<string, unknown>>; stats: Record<string, any>;
};

const textOf = (result: McpToolResult, n = 0) => {
  const part = result.content[n];
  if (!part || part.type !== "text") throw new Error(`content ${n} is not text`);
  return part.text;
};
const bodyOf = (result: McpToolResult) => JSON.parse(textOf(result));
const run = (session: Session, tool: string, input: Record<string, unknown>) =>
  session.runAgentExclusive(() => TOOL_MAP.get(tool)!.run(session, input));

describe.skipIf(!browserPath)("flight recorder in a real browser", () => {
  let proc: ChildProcess;
  let userDataDir: string;
  let endpoint: string;
  let server: Server;
  let base: string;
  let browser: Browser;

  beforeAll(async () => {
    process.env.BROWSER_SSRF_ALLOW = "127.0.0.0/8,::1/128";
    const app = await startServer();
    server = app.server;
    base = `http://127.0.0.1:${app.port}`;
    const port = await freePort();
    userDataDir = mkdtempSync(join(tmpdir(), "browser-mcp-recorder-"));
    proc = spawn(browserPath!, [
      "--headless=new", "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
      `--remote-debugging-port=${port}`, `--user-data-dir=${userDataDir}`, "about:blank",
    ], { stdio: "ignore", detached: process.platform !== "win32" });
    await waitForCdp(port);
    endpoint = `http://127.0.0.1:${port}`;
    browser = await chromium.connectOverCDP(endpoint);
  }, 30000);

  afterAll(async () => {
    delete process.env.BROWSER_SSRF_ALLOW;
    await browser?.close().catch(() => {});
    server?.closeAllConnections?.();
    await new Promise<void>((resolve) => (server ? server.close(() => resolve()) : resolve()));
    await stopBrowser(proc);
    if (userDataDir) rmSync(userDataDir, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 });
  });

  describe("the in-page recorder", () => {
    let page: Page;
    beforeEach(async () => {
      const context = browser.contexts()[0] ?? await browser.newContext();
      page = await context.newPage();
      await page.goto(`${base}/app`);
      await page.evaluate(recorderSource());
    });
    afterEach(async () => { await page.close().catch(() => {}); });

    const start = (opts: Record<string, unknown> = {}) => page.evaluate((o) => (window as any).__flightRecorder.start(o), opts);
    const stop = () => page.evaluate(() => (window as any).__flightRecorder.stop()) as Promise<Recording>;
    const mark = (label: string) => page.evaluate((l) => (window as any).__flightRecorder.mark(l), label);

    it("records a clean transition with zero anomalies at a small per-frame cost", async () => {
      expect(await start()).toMatchObject({ ok: true, fields: ["path", "surface", "composer", "messages", "loading"] });
      await mark("send");
      await page.evaluate(() => (window as any).clean());
      const rec = await stop();
      expect(rec.anomalies).toEqual([]);
      expect(rec.text).toContain("surface=loading");
      expect(rec.text).toContain("surface=active");
      expect(rec.text).toContain("- Hero unmounted");
      expect(rec.stats.frames).toBeGreaterThan(3);
      expect(rec.stats.sampleMsAvg).toBeLessThan(2);
      expect(rec.stats.internalErrors).toBe(0);
    });

    it("flags a one-frame flicker", async () => {
      await start();
      await mark("click");
      await page.evaluate(() => (window as any).flicker());
      const rec = await stop();
      const surface = rec.anomalies.find((a) => a.kind === "reversal" && a.field === "surface");
      expect(surface?.message).toMatch(/^surface went new → active → new within one step/);
      expect(surface?.flicker).toBe(true);
    });

    it("flags a remount", async () => {
      await start();
      await page.evaluate(() => (window as any).remount());
      const rec = await stop();
      expect(rec.anomalies.map((a) => [a.kind, a.name])).toEqual([["remount", "Hero"]]);
    });

    it("flags an unexpected layout jump with its source", async () => {
      await start();
      await page.evaluate(() => (window as any).jump());
      const rec = await stop();
      const shift = rec.anomalies.find((a) => a.kind === "layout_shift");
      expect(shift?.message).toMatch(/layout shifted 0\.\d+ in total/);
      expect(rec.stats.layoutShift.total).toBeGreaterThan(0.05);
    });

    it("flags duplicate transcript rows", async () => {
      await start();
      await page.evaluate(() => (window as any).duplicate());
      const rec = await stop();
      expect(rec.anomalies.find((a) => a.kind === "duplicate_rows")?.message).toContain("m1");
    });

    it("captures errors and failed requests without changing what the page sees", async () => {
      await page.evaluate(() => {
        (window as any).seen = { errors: 0, rejections: 0 };
        window.addEventListener("error", () => { (window as any).seen.errors += 1; });
        window.addEventListener("unhandledrejection", () => { (window as any).seen.rejections += 1; });
      });
      await start();
      const outcome = await page.evaluate(async () => {
        const ok = await fetch("/ok");
        const missing = await fetch("/missing?token=s3cret&page=2");
        let caught = "";
        try { await fetch("http://127.0.0.1:1/refused?api_key=s3cret"); } catch (error) { caught = (error as Error).name; }
        const xhr = await new Promise<number>((resolve) => {
          const request = new XMLHttpRequest();
          request.open("GET", "/boom");
          request.onloadend = () => resolve(request.status);
          request.send();
        });
        const controller = new AbortController();
        const aborted = fetch("/slow", { signal: controller.signal }).catch((error) => error.name);
        controller.abort();
        // Unhandled on purpose: the page must still hear about it.
        fetch("http://127.0.0.1:1/unhandled");
        console.error("render failed", { code: 7 }, "Bearer abcdefghijklmnop");
        setTimeout(() => { throw new Error("late failure"); }, 0);
        Promise.reject(new Error("nobody listens"));
        await new Promise((r) => setTimeout(r, 300));
        return { ok: ok.status, missing: missing.status, caught, xhr, aborted: await aborted, isResponse: ok instanceof Response };
      });
      expect(outcome).toEqual({ ok: 200, missing: 404, caught: "TypeError", xhr: 500, aborted: "AbortError", isResponse: true });
      const rec = await stop();
      const seen = await page.evaluate(() => (window as any).seen);
      expect(seen.errors).toBe(1);
      expect(seen.rejections).toBe(2);
      const kinds = rec.anomalies.map((a) => a.kind);
      expect(kinds.filter((k) => k === "failed_requests")).toHaveLength(4);
      expect(kinds.filter((k) => k === "errors")).toHaveLength(4);
      const all = JSON.stringify(rec);
      expect(all).not.toContain("s3cret");
      expect(all).not.toContain("abcdefghijklmnop");
      expect(rec.text).toContain("GET /missing?… → HTTP 404");
      // An abort is the page's own decision: shown, never flagged.
      expect(rec.text).toContain("· GET /slow → aborted");
      expect(JSON.stringify(rec.anomalies)).not.toContain("/slow");
    });

    it("puts the page's own functions back on stop, and leaves a later wrapper alone", async () => {
      const before = await page.evaluate(() => [window.fetch, console.error, XMLHttpRequest.prototype.open].map(String));
      await start();
      expect(await page.evaluate(() => window.fetch.name)).toBe("fetch");
      await stop();
      expect(await page.evaluate(() => [window.fetch, console.error, XMLHttpRequest.prototype.open].map(String))).toEqual(before);
      await start();
      await page.evaluate(() => {
        const inner = window.fetch;
        (window as any).pageWrapper = function wrapped(...args: Parameters<typeof fetch>) { return inner(...args); };
        window.fetch = (window as any).pageWrapper;
      });
      await stop();
      expect(await page.evaluate(() => window.fetch === (window as any).pageWrapper)).toBe(true);
      expect(await page.evaluate(async () => (await fetch("/ok")).status)).toBe(200);
    });

    it("refuses bad options and misuse without throwing into the page", async () => {
      expect(await start({ selectors: ["broken=[[["] })).toMatchObject({ ok: false });
      expect(await start({ fields: ["Bad Field"] })).toMatchObject({ ok: false, error: expect.stringContaining("invalid field") });
      expect(await mark("nothing")).toMatchObject({ ok: false });
      expect(await page.evaluate(() => (window as any).__flightRecorder.stop())).toMatchObject({ ok: false });
      expect(await start()).toMatchObject({ ok: true });
      expect(await start()).toMatchObject({ ok: false, error: "already recording" });
      expect(await start({ restart: true })).toMatchObject({ ok: true });
      await page.evaluate(() => { (window as any).PerformanceObserver = undefined; });
      expect((await stop()).ok).toBe(true);
    });

    it("tracks explicit fields anywhere and selector counts", async () => {
      await start({ fields: ["surface"], selectors: ["rows=[data-ui-row]"] });
      await page.evaluate(() => (window as any).clean());
      const rec = await stop();
      const fields = new Set(rec.timeline.flatMap((e) => (e.kind === "state" ? Object.keys(e.changes as object) : [])));
      expect([...fields].sort()).toEqual(["path", "rows", "surface"]);
      expect(rec.text).toContain("rows=1");
      expect(rec.anomalies).toEqual([]);
    });

    it("follows client-side routing in the same document", async () => {
      await start();
      await page.evaluate(async () => {
        history.pushState({}, "", "/chat/abc?session=s3cret");
        await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
      });
      const rec = await stop();
      expect(rec.text).toContain("/chat/abc?session=…");
      expect(rec.anomalies).toEqual([]);
    });

    it("resumes after a full page load in the same origin and reports a lost one across origins", async () => {
      const started = await start();
      await page.addInitScript({ content: resumeScript({ id: started.id, startEpoch: started.startedAt, opts: {} }) });
      await mark("go next");
      await page.goto(`${base}/next`);
      await page.waitForTimeout(150);
      let rec = await stop();
      expect(rec.id).toBe(started.id);
      expect(rec.stats.segments).toBe(2);
      expect(rec.text).toContain('> mark "go next"');
      expect(rec.text).toMatch(/\* full page load \/next\n.*\+ ChatView mounted/);
      expect(rec.anomalies).toEqual([]);

      const again = await start();
      await page.addInitScript({ content: resumeScript({ id: again.id, startEpoch: again.startedAt, opts: {} }) });
      await page.goto(base.replace("127.0.0.1", "localhost") + "/app");
      await page.waitForTimeout(150);
      rec = await page.evaluate(() => (window as any).__flightRecorder.stop());
      expect(rec.anomalies.map((a) => a.kind)).toContain("recording_gap");
    });

    it("stops sampling at maxDurationMs but keeps what it has", async () => {
      await start({ maxDurationMs: 100 });
      await page.waitForTimeout(400);
      expect(await page.evaluate(() => (window as any).__flightRecorder.peek().recording)).toBe(false);
      const rec = await stop();
      expect(rec.ok).toBe(true);
      expect(rec.text).toContain("recording stopped: reached maxDurationMs");
    });
  });

  describe("browser_record", () => {
    let manager: BrowserManager;
    let session: Session;
    beforeEach(async () => {
      manager = new BrowserManager({ endpoint });
      session = new Session(manager);
      await session.ensurePage();
      expect(bodyOf(await run(session, "browser_navigate", { url: `${base}/app` })).ok).toBe(true);
    });
    afterEach(async () => {
      if (activeRecording(session)) await run(session, "browser_record", { action: "stop" });
      await manager.cleanup();
    });

    const fetchMs = (path = "/ok") => run(session, "browser_eval", {
      code: `(async () => { const s = performance.now(); await fetch(${JSON.stringify(path)}); return Math.round(performance.now() - s); })()`,
    }).then((r) => Number(bodyOf(r).value));

    it("records a flicker under 4x CPU throttle with keyframes, marking each step", async () => {
      const started = bodyOf(await run(session, "browser_record", { action: "start", cpu_throttle: 4, keyframes: true }));
      expect(started).toMatchObject({ ok: true, cpu_throttle: 4, keyframes: true });
      expect(started.fields).toEqual(["path", "surface", "composer", "messages", "loading"]);
      await run(session, "browser_eval", { code: "flicker()" });
      const stopped = await run(session, "browser_record", { action: "stop" });
      const body = bodyOf(stopped);
      expect(body.ok).toBe(true);
      expect(body.anomalies.map((a: { kind: string; field?: string }) => `${a.kind}:${a.field}`)).toEqual(
        expect.arrayContaining(["reversal:surface", "reversal:composer"]),
      );
      expect(body.throttle).toEqual({ cpu: 4 });
      expect(textOf(stopped, 1)).toContain('> mark "eval"');
      expect(textOf(stopped, 1)).toContain("<< reversal(surface)");
      const image = stopped.content.find((part) => part.type === "image");
      expect(image?.mimeType).toBe("image/jpeg");
      expect(body.keyframes.length).toBeGreaterThan(0);
      expect(activeRecording(session)).toBeUndefined();
    });

    it("keeps recording through browser_navigate's full page load", async () => {
      await run(session, "browser_record", { action: "start" });
      await run(session, "browser_navigate", { url: `${base}/next?token=s3cret` });
      const stopped = await run(session, "browser_record", { action: "stop" });
      const body = bodyOf(stopped);
      expect(body.stats.page_loads).toBe(1);
      expect(body.anomaly_count).toBe(0);
      const timeline = textOf(stopped, 1);
      expect(timeline).toMatch(/> mark "navigate http:\/\/127\.0\.0\.1:\d+\/next\?…"/);
      expect(timeline).toContain("* full page load /next?token=…");
      expect(JSON.stringify(stopped)).not.toContain("s3cret");
    });

    it("throttles the network until stop", async () => {
      await run(session, "browser_record", { action: "start", network: "fast-3g" });
      expect(await fetchMs()).toBeGreaterThanOrEqual(400);
      await run(session, "browser_record", { action: "stop" });
      expect(await fetchMs()).toBeLessThan(400);
    });

    it("releases throttling when max_duration_ms runs out and still returns the timeline", async () => {
      await run(session, "browser_record", { action: "start", network: "fast-3g", max_duration_ms: 1000 });
      await new Promise((r) => setTimeout(r, 3500));
      expect(activeRecording(session)?.expired).toBe(true);
      expect(await fetchMs()).toBeLessThan(400);
      const body = bodyOf(await run(session, "browser_record", { action: "stop" }));
      expect(body.ok).toBe(true);
      expect(body.note).toContain("max_duration_ms (1000) ran out");
    });

    it("explains misuse instead of failing silently", async () => {
      expect(bodyOf(await run(session, "browser_record", { action: "stop" }))).toMatchObject({ ok: false, code: "not_recording", outcome: "not_executed" });
      expect(bodyOf(await run(session, "browser_record", { action: "mark", label: "x" }))).toMatchObject({ code: "not_recording" });
      expect(bodyOf(await run(session, "browser_record", { action: "start", selectors: ["bad=[[["] }))).toMatchObject({ code: "invalid_input" });
      expect(activeRecording(session)).toBeUndefined();
      expect(bodyOf(await run(session, "browser_record", { action: "start" })).ok).toBe(true);
      expect(bodyOf(await run(session, "browser_record", { action: "start" }))).toMatchObject({ code: "recording_active" });
      expect(bodyOf(await run(session, "browser_record", { action: "mark", label: "hand-made" }))).toMatchObject({ ok: true });
      const stopped = await run(session, "browser_record", { action: "stop" });
      expect(textOf(stopped, 1)).toContain('> mark "hand-made"');
    });
  });
});
