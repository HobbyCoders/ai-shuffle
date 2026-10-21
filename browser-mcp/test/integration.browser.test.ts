/**
 * Optional integration test. Gated: it only runs when a system Chromium/Chrome
 * is discoverable. It launches that browser headless with a remote-debugging
 * port, connects over CDP exactly as the server does, and drives the core tools
 * against a static fixture. When no browser is found the whole suite is skipped
 * so `npm test` stays green in browser-less CI.
 */

import { describe, it, expect, beforeAll, afterAll, vi } from "vitest";
import { spawn, execSync, type ChildProcess } from "node:child_process";
import { createServer } from "node:net";
import { createServer as createHttpServer, type Server as HttpServer } from "node:http";
import { existsSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import { BrowserManager } from "../src/browser.js";
import { compilePolicy } from "../src/ssrf.js";
import { Session, raceWithTimeout } from "../src/session.js";
import { runNavigate } from "../src/tools/navigate.js";
import { runSnapshot } from "../src/tools/snapshot.js";
import { runAct } from "../src/tools/act.js";
import { runScreenshot, MAX_EDGE } from "../src/tools/screenshot.js";
import { runFind } from "../src/tools/find.js";
import { runReadText } from "../src/tools/read_text.js";
import { runEval } from "../src/tools/eval.js";
import { runConsole } from "../src/tools/console.js";
import { runNetwork, NETWORK_BODY_TIMEOUT_MS } from "../src/tools/network.js";
import { runTabs } from "../src/tools/tabs.js";
import { runResize } from "../src/tools/resize.js";
import { runUpload } from "../src/tools/upload.js";
import { runBatch } from "../src/tools/batch.js";
import { TOOL_MAP } from "../src/tools/index.js";
import type { McpToolResult } from "../src/tools/result.js";
import { WATCH_KEY, installMutationWatcher, collectMutationStats, classifyMutations } from "../src/dommutation.js";
import { observationRootStatus } from "../src/tools/observation_scope.js";
import { BrowserWorkspace } from "../src/workspace.js";
import { runWait } from "../src/tools/wait.js";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";

const NET_BODY_MARKER = "net-body-marker-12345";

/** Small HTTP app: an HTML page that logs to the console and fetches JSON. */
function startHttpApp(): Promise<{ server: HttpServer; url: string; holdHistoryStream: () => { request: Promise<void>; release: () => void } }> {
  // Gate the response, not a wall-clock delay: CI can take arbitrarily long to
  // dispatch history navigation, and modern Chromium caches no-store pages.
  let streamGate: { requested: () => void; released: Promise<void> } | undefined;
  function holdHistoryStream() {
    let requested!: () => void, release!: () => void;
    const request = new Promise<void>(resolve => { requested = resolve; });
    const released = new Promise<void>(resolve => { release = resolve; });
    streamGate = { requested, released };
    return { request, release: () => { streamGate = undefined; release(); } };
  }
  const html = `<!doctype html><html><head><title>net fixture</title></head>
<body><main><p id="out">pending</p></main>
<script>
console.log("http-fixture-log hi");
fetch("/api/data").then(r=>r.json()).then(d=>{document.getElementById("out").textContent=JSON.stringify(d);});
</script></body></html>`;
  const server = createHttpServer((req, res) => {
    if (req.url?.startsWith("/review/")) {
      const url = new URL(req.url, "http://fixture.test");
      if (url.pathname === "/review/history-stream") {
        res.writeHead(200, { "content-type": "text/html", "cache-control": "no-store" });
        const gate = streamGate;
        streamGate = undefined;
        res.write('<!doctype html><title>Streamed history</title><p>Early prose</p><script>window.persisted=false;addEventListener("pageshow",e=>window.persisted=e.persisted)</script>' + " ".repeat(2048));
        gate?.requested();
        void (gate?.released ?? Promise.resolve()).then(() => res.end('<button>Late streamed button</button><p>Stream finished</p>'));
        return;
      }
      if (url.pathname === "/review/nested-parent") {
        const child = new URL("/observation-text?text=DeepReachableMarker", `http://${req.headers.host}`).href;
        res.writeHead(200, { "content-type": "text/html" });
        res.end(`<p>Parent prose</p>${url.searchParams.has("tall") ? '<div style="height:2000px"></div>' : ""}<iframe src="${child}"></iframe>`);
        return;
      }
      const pages: Record<string, string> = {
        "/review/history-a": '<title>History A</title><button>History A</button><p id="clock">Clock</p><script>window.restores=0;addEventListener("pageshow",e=>{if(e.persisted)window.restores++});setInterval(()=>clock.textContent=String(Date.now()),100)</script>',
        "/review/history-b": '<title>History B</title><button>History B</button><script>window.restores=0;addEventListener("pageshow",e=>{if(e.persisted)window.restores++})</script>',
        "/review/single": '<title>Single</title><label>Size<select><option value="s">Small</option><option value="m">Medium</option></select></label>',
        "/review/repeated": '<title>Repeated</title><main aria-label="Repeated">' + '<button>Same</button>'.repeat(20) + '</main>',
        "/review/jsredir": '<title>Redirect</title><p>Redirecting</p><script>setTimeout(()=>location.href="/review/single",5)</script>',
        "/review/reload": '<title>Reload</title><form method="post" action="/review/reload"><button>Save</button></form>',
        "/review/slowreload": '<title>Reload</title><form method="post" action="/review/slowreload"><button>Save</button></form>',
        "/review/latereload": '<title>Reload</title><button onclick="setTimeout(()=>location.reload(),400)">Later</button>',
      };
      let html = pages[url.pathname];
      if (url.pathname === "/review/loading") {
        const media: Record<string, string> = {
          svg: '<svg width="20" height="20"><circle r="5" cx="10" cy="10"></circle></svg>',
          img: '<img width="20" height="20" alt="" src="data:image/svg+xml,%3Csvg xmlns=%22http://www.w3.org/2000/svg%22/%3E">',
          canvas: '<canvas width="20" height="20"></canvas>',
          hidden: '<button hidden>Hidden control</button>',
          spinner: '<svg width="20" height="20"><circle r="5" cx="10" cy="10"></circle></svg>',
        };
        const kind = url.searchParams.get("kind")!;
        html = `<title>Loading app</title><main id="root">${media[kind] ?? ""}${kind === "spinner" ? "" : "Loading your dashboard…"}</main><script>setTimeout(()=>root.innerHTML='<button>Hydrated</button>',600)</script>`;
      }
      const send = () => { res.writeHead(200, { "content-type": "text/html" }); res.end(html ?? "not found"); };
      if (url.pathname === "/review/slowreload" && req.method === "POST") setTimeout(send, 400);
      else send();
      return;
    }
    if (req.url === "/action-late" || req.url === "/action-pending") {
      setTimeout(() => { res.writeHead(200, { "content-type": "text/html" }); res.end('<button>Network result</button>'.repeat(15)); }, req.url === "/action-late" ? 700 : 2200);
      return;
    }
    if (req.url === "/never-observation") return; // deliberately no headers/body
    if (req.url === "/bodyless.svg") {
      res.writeHead(200, { "content-type": "image/svg+xml" });
      res.end('<svg xmlns="http://www.w3.org/2000/svg"><text>SVG text</text></svg>');
      return;
    }
    if (req.url?.startsWith("/observation-text?")) {
      res.writeHead(200, { "content-type": "text/html" });
      res.end(`<p>${new URL(req.url, "http://fixture").searchParams.get("text")}</p>`);
      return;
    }
    if (req.url === "/inner-observation") {
      res.writeHead(200, { "content-type": "text/html" });
      res.end('<p id="cross">Cross frame text</p>');
      return;
    }
    if (req.url === "/delayed-hydration") {
      res.writeHead(200, { "content-type": "text/html" });
      res.end('<title>Loading app</title><main id="app">Loading...</main><script>fetch("/hydrate").then(r=>r.text()).then(html=>document.getElementById("app").innerHTML=html)</script>');
      return;
    }
    if (req.url === "/hydrate") {
      setTimeout(() => { res.writeHead(200, { "content-type": "text/html" }); res.end('<button>Hydrated control</button>'); }, 350);
      return;
    }
    if (req.url === "/blank-app" || req.url === "/canvas-app" || req.url === "/prose-app") {
      res.writeHead(200, { "content-type": "text/html" });
      res.end('<title>App</title><body>' + (req.url === "/blank-app" ? '<script>throw new Error("mount failed")</script>' : req.url === "/canvas-app" ? '<canvas width="100" height="100"></canvas>' : '<p>This page contains plain prose without any actionable controls.</p>') + '</body>');
      return;
    }
    if (req.url === "/auth-redirect") {
      res.writeHead(302, { location: `http://localhost:${req.headers.host!.split(":").pop()}/login` });
      res.end();
      return;
    }
    if (req.url === "/login") {
      res.writeHead(200, { "content-type": "text/html" });
      res.end('<title>Sign in</title><button>Sign in</button>');
      return;
    }
    if (req.url === "/stream-page") {
      res.writeHead(200, { "content-type": "text/html" });
      res.end('<title>stream</title><p>streaming</p><script>fetch("/stream").then(r => r.body.getReader().read())</script>');
      return;
    }
    if (req.url === "/stream") {
      // An event stream that never ends: headers and one event, then silence.
      res.writeHead(200, { "content-type": "text/event-stream", "cache-control": "no-cache" });
      res.write("data: first\n\n");
      return;
    }
    if (req.url === "/menu-next") {
      res.writeHead(200, { "content-type": "text/html" });
      res.end("<!doctype html><title>Next page</title>");
      return;
    }
    if (req.url === "/menu") {
      res.writeHead(200, { "content-type": "text/html" });
      res.end(`<!doctype html><title>More menu fixture</title>
        <button aria-haspopup="menu" aria-expanded="false" id="more">More</button>
        <div id="popup"></div><main id="docs" hidden><h1>Docs</h1>
          <a href="/menu-next">Next page</a></main>
        <script>
          document.getElementById("more").onclick = () => {
            document.getElementById("more").setAttribute("aria-expanded", "true");
            document.getElementById("popup").innerHTML =
              '<div role="menu" aria-label="More workspace destinations">' +
              '<button role="menuitem" id="docs-item"><span>Docs</span></button></div>';
            document.getElementById("docs-item").onclick = () => {
              document.getElementById("popup").replaceChildren();
              document.getElementById("more").setAttribute("aria-expanded", "false");
              document.getElementById("docs").hidden = false;
            };
          };
        </script>`);
      return;
    }
    if (req.url === "/api/data") {
      res.writeHead(200, { "content-type": "application/json" });
      res.end(JSON.stringify({ marker: NET_BODY_MARKER }));
      return;
    }
    res.writeHead(200, { "content-type": "text/html" });
    res.end(html);
  });
  return new Promise((resolve) => {
    server.listen(0, "127.0.0.1", () => {
      const addr = server.address();
      const port = addr && typeof addr === "object" ? addr.port : 0;
      resolve({ server, url: `http://127.0.0.1:${port}/`, holdHistoryStream });
    });
  });
}

const delay = (ms: number) => new Promise((r) => setTimeout(r, ms));

function findBrowser(): string | null {
  if (process.platform === "win32") {
    for (const base of [process.env.PROGRAMFILES, process.env["PROGRAMFILES(X86)"], process.env.LOCALAPPDATA]) {
      if (!base) continue;
      for (const relative of ["Google/Chrome/Application/chrome.exe", "Microsoft/Edge/Application/msedge.exe"]) {
        const candidate = join(base, relative);
        if (existsSync(candidate)) return candidate;
      }
    }
    return null;
  }
  for (const cmd of ["chromium", "chromium-browser", "google-chrome-stable", "google-chrome"]) {
    try {
      const p = execSync(`command -v ${cmd}`, { stdio: ["ignore", "pipe", "ignore"] })
        .toString()
        .trim();
      if (p) return p;
    } catch {
      /* not found */
    }
  }
  return null;
}

function freePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const srv = createServer();
    srv.listen(0, "127.0.0.1", () => {
      const addr = srv.address();
      if (addr && typeof addr === "object") {
        const port = addr.port;
        srv.close(() => resolve(port));
      } else {
        srv.close(() => reject(new Error("no port")));
      }
    });
    srv.on("error", reject);
  });
}

async function waitForCdp(port: number, timeoutMs = 10000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const res = await fetch(`http://127.0.0.1:${port}/json/version`);
      if (res.ok) return;
    } catch {
      /* not up yet */
    }
    await new Promise((r) => setTimeout(r, 150));
  }
  throw new Error("CDP endpoint did not come up");
}

function firstText(result: McpToolResult): string {
  const t = result.content.find((c) => c.type === "text");
  return t && t.type === "text" ? t.text : "";
}

const browserPath = findBrowser();
const fixtureUrl = new URL("./fixtures/basic.html", import.meta.url).href;
const uploadFixtureUrl = new URL("./fixtures/upload.html", import.meta.url).href;

describe.skipIf(!browserPath)("integration: core tools over CDP", () => {
  let proc: ChildProcess;
  let userDataDir: string;
  let manager: BrowserManager;
  let session: Session;
  let httpServer: HttpServer;
  let httpUrl: string;
  let holdHistoryStream: Awaited<ReturnType<typeof startHttpApp>>["holdHistoryStream"];
  let uploadDir: string;
  let uploadFilePath: string;
  let cdpEndpoint: string;

  beforeAll(async () => {
    // The guard blocks loopback by default; the test fixtures (HTTP server and
    // file:// pages) live on 127.0.0.1, so force-allow loopback for the harness.
    // This is exactly the operator opt-in that BROWSER_SSRF_ALLOW exists for.
    process.env.BROWSER_SSRF_ALLOW = "127.0.0.0/8,::1/128";
    const app = await startHttpApp();
    httpServer = app.server;
    httpUrl = app.url;
    holdHistoryStream = app.holdHistoryStream;
    uploadDir = mkdtempSync(join(tmpdir(), "browser-mcp-up-"));
    uploadFilePath = join(uploadDir, "upload-sample.txt");
    writeFileSync(uploadFilePath, "hello upload\n");
    const port = await freePort();
    userDataDir = mkdtempSync(join(tmpdir(), "browser-mcp-it-"));
    proc = spawn(
      browserPath!,
      [
        "--headless=new",
        "--no-sandbox",
        "--disable-gpu",
        // Linux/Ozone only, as in docker/start-chrome.sh (#1791). Other platforms
        // ignore this flag; the fixed-screen regression is skipped there (#2292).
        "--ozone-override-screen-size=1920,1080",
        `--remote-debugging-port=${port}`,
        `--user-data-dir=${userDataDir}`,
        "about:blank",
      ],
      { stdio: "ignore" },
    );
    await waitForCdp(port);
    cdpEndpoint = `http://127.0.0.1:${port}`;
    manager = new BrowserManager({ endpoint: cdpEndpoint });
    session = new Session(manager);
  }, 30000);

  afterAll(async () => {
    delete process.env.BROWSER_SSRF_ALLOW;
    try {
      await manager?.cleanup();
    } catch {
      /* ignore */
    }
    if (httpServer) {
      // Drop any keep-alive connection the browser held, else close() hangs.
      httpServer.closeAllConnections?.();
      await new Promise<void>((resolve) => httpServer.close(() => resolve()));
    }
    if (uploadDir) {
      try {
        rmSync(uploadDir, { recursive: true, force: true });
      } catch {
        /* ignore */
      }
    }
    // Wait for the browser process to actually exit before removing its profile
    // dir — SIGKILL is async and rmSync races the process releasing its files
    // (ENOTEMPTY on the still-open profile) if we don't.
    if (proc) {
      await new Promise<void>((resolve) => {
        proc.once("exit", () => resolve());
        proc.kill("SIGKILL");
        setTimeout(resolve, 3000);
      });
    }
    if (userDataDir) {
      for (let attempt = 0; attempt < 5; attempt++) {
        try {
          rmSync(userDataDir, { recursive: true, force: true });
          break;
        } catch {
          await new Promise((r) => setTimeout(r, 200));
        }
      }
    }
  });

  describe("action and settle regressions", () => {
    async function load(html: string) {
      await runNavigate(session, { url: httpUrl + "prose-app" });
      const page = await session.ensurePage();
      await page.setContent(html);
      session.notifyNavigation();
      return page;
    }
    async function ref(name: string) {
      const snap = await session.snapshot({ filter: "all" });
      const line = snap.text.split("\n").find(line => line.includes(`"${name}"`));
      const match = line?.match(/ref=([^\]\s]+)/);
      if (!match) throw new Error(`missing ${name}: ${snap.text}`);
      return match[1];
    }
    const body = (result: McpToolResult) => JSON.parse(firstText(result));
    it.each([
      ["open", false], ["open", true], ["closed", false], ["closed", true],
    ] as const)("FOLLOWUP %s shadow slotted clipped frames (absolute: %s) stay hidden from read_text", async (mode, absolute) => {
      const page = await load('<p>Visible prose</p><div id="host"><iframe srcdoc="<p>HiddenSlottedMarker</p>"></iframe></div>');
      await page.evaluate(({ mode, absolute }) => {
        if (absolute) document.querySelector("iframe")!.style.cssText = "position:absolute;top:0;left:0";
        document.getElementById("host")!.attachShadow({ mode }).innerHTML =
          '<div style="position:relative;overflow:hidden;height:var(--clip-height,0px)"><slot></slot></div>';
      }, { mode, absolute });
      const frame = page.mainFrame().childFrames()[0];
      await frame.getByText("HiddenSlottedMarker").waitFor({ state: "attached" });
      expect(await observationRootStatus({ frame, primary: false })).toBe("hidden");
      const result = await runReadText(session);
      expect(body(result)).toMatchObject({ frames_hidden: 1 });
      expect((result.content[1] as { text: string }).text).not.toContain("HiddenSlottedMarker");
      await page.evaluate(() => {
        document.getElementById("host")!.style.setProperty("--clip-height", "200px");
      });
      expect(await observationRootStatus({ frame, primary: false })).toBe("included");
      expect((await runReadText(session)).content[1]).toMatchObject({ text: expect.stringContaining("HiddenSlottedMarker") });
    });
    it.each(["visible", "inside", "outside"])("FOLLOWUP closed-shadow clipping %s a scrollport preserves only reachable text", async where => {
      const page = await load('<p>Main prose</p><div id="outer"><div style="height:100px;overflow:auto"><div style="height:800px"></div><div id="host"><iframe srcdoc="<p>ScrollSlotMarker</p>"></iframe></div></div></div>');
      await page.evaluate(where => {
        document.getElementById("host")!.attachShadow({ mode: "closed" }).innerHTML =
          `<div style="overflow:hidden;height:${where === "inside" ? 0 : 200}px"><slot></slot></div>`;
        if (where === "outside") document.getElementById("outer")!.attachShadow({ mode: "closed" }).innerHTML =
          '<div style="overflow:hidden;height:0"><slot></slot></div>';
      }, where);
      const frame = page.mainFrame().childFrames()[0];
      await frame.getByText("ScrollSlotMarker").waitFor({ state: "attached" });
      expect(await observationRootStatus({ frame, primary: false })).toBe(where === "visible" ? "included" : "hidden");
      const result = await runReadText(session);
      expect(body(result)).toMatchObject({ frames_hidden: where === "visible" ? 0 : 1 });
      const text = (result.content[1] as { text: string }).text;
      if (where === "visible") expect(text).toContain("ScrollSlotMarker");
      else expect(text).not.toContain("ScrollSlotMarker");
    });
    it("FOLLOWUP back and forward restore bfcache within the readiness budget and invalidate refs", async () => {
      const page = await session.ensurePage();
      page.setDefaultNavigationTimeout(3000);
      try {
        await runNavigate(session, { url: httpUrl + "review/history-a" });
        const beforeA = session.currentSnapshotId;
        await runNavigate(session, { url: httpUrl + "review/history-b" });
        const beforeB = session.currentSnapshotId;
        expect(beforeB).toBeGreaterThan(beforeA);
        const snapshot = session.snapshot.bind(session);
        const registryBeforeSnapshot: number[] = [];
        const snapshotSpy = vi.spyOn(session, "snapshot").mockImplementation(async (...args) => {
          registryBeforeSnapshot.push(session.refs.knownCount());
          return snapshot(...args);
        });
        const started = performance.now();
        const back = body(await runNavigate(session, { url: "back" }));
        snapshotSpy.mockRestore();
        expect(registryBeforeSnapshot).toEqual([0]);
        expect(back.url).toBe(httpUrl + "review/history-a");
        const backMs = Math.round(performance.now() - started);
        console.info("HISTORY_RESTORE", JSON.stringify({ direction: "back", ms: backMs, timings: back.timings }));
        expect(backMs).toBeLessThan(2500);
        expect(await page.evaluate(() => (window as unknown as { restores: number }).restores)).toBe(1);
        expect(back.snapshot_stale).toBe(true);
        // Playwright may reuse token strings after a new snapshot (including
        // the same cached elements). The old registry must be cleared BEFORE
        // publishing the restored snapshot, whose refs are now usable.
        expect(back.snapshot_id).toBeGreaterThan(beforeB);
        // Restoring a frozen ticker does not by itself prove a recent baseline.
        await page.waitForTimeout(300);
        const restoredA = await ref("History A");
        const restoredAction = body(await runAct(session, { kind: "click", ref: restoredA }));
        console.info("HISTORY_RESTORED_ACTION", JSON.stringify(restoredAction));
        expect(restoredAction).toMatchObject({ ok: true, snapshot_stale: false });
        const beforeForward = session.currentSnapshotId;
        const forwardSpy = vi.spyOn(session, "snapshot").mockImplementation(async (...args) => {
          expect(session.refs.knownCount()).toBe(0);
          return snapshot(...args);
        });
        const forwardStarted = performance.now();
        const forward = body(await runNavigate(session, { url: "forward" }));
        forwardSpy.mockRestore();
        const forwardMs = Math.round(performance.now() - forwardStarted);
        console.info("HISTORY_RESTORE", JSON.stringify({ direction: "forward", ms: forwardMs, timings: forward.timings }));
        expect(forwardMs).toBeLessThan(2500);
        expect(await page.evaluate(() => (window as unknown as { restores: number }).restores)).toBe(1);
        expect(forward).toMatchObject({ url: httpUrl + "review/history-b", snapshot_stale: true });
        expect(forward.snapshot_id).toBeGreaterThan(beforeForward);
        expect(body(await runAct(session, { kind: "click", ref: await ref("History B") })).snapshot_stale).toBe(false);
      } finally {
        vi.restoreAllMocks();
        page.setDefaultNavigationTimeout(30000);
      }
    });
    it("FOLLOWUP non-cached history waits for a streamed document before taking refs", async () => {
      const page = await session.ensurePage();
      await runNavigate(session, { url: httpUrl + "review/history-stream" });
      await runNavigate(session, { url: httpUrl + "review/history-b" });
      // Chromium can bfcache even no-store/unload/Web Lock pages now. Changing
      // an HTTP-only cookie evicts a no-store document explicitly, without
      // disabling bfcache for the separate cached-history regression above.
      await page.context().addCookies([{ name: "history-cache-bust", value: String(performance.now()), url: httpUrl, httpOnly: true }]);
      const gate = holdHistoryStream();
      const snapshot = vi.spyOn(session, "snapshot");
      const navigation = runNavigate(session, { url: "back" });
      try {
        await raceWithTimeout(gate.request, 5000, "history did not request the streamed document");
        await page.waitForFunction(() => document.title === "Streamed history" && document.readyState === "loading");
        expect(snapshot).not.toHaveBeenCalled();
        gate.release();
        const result = await navigation;
        expect(await page.evaluate(() => (window as unknown as { persisted: boolean }).persisted)).toBe(false);
        expect(await page.evaluate(() => document.readyState)).not.toBe("loading");
        expect((result.content[1] as { text: string }).text).toContain("Late streamed button");
        expect(body(result).ref_count).toBeGreaterThan(0);
      } finally {
        gate.release();
        await navigation.catch(() => {});
        snapshot.mockRestore();
      }
    });
    it.each([false, true])("FOLLOWUP history readiness honors the configured navigation timeout (context: %s)", async contextDefault => {
      const originalTab = session.currentTabId()!;
      const tab = await session.createTab(); // no page-level override to mask the context default
      const page = await session.ensurePage();
      await runNavigate(session, { url: httpUrl + "review/history-stream" });
      await runNavigate(session, { url: httpUrl + "review/history-b" });
      // Chromium can bfcache even no-store/unload/Web Lock pages now. Changing
      // an HTTP-only cookie evicts a no-store document explicitly, without
      // disabling bfcache for the separate cached-history regression above.
      await page.context().addCookies([{ name: "history-cache-bust", value: String(performance.now()), url: httpUrl, httpOnly: true }]);
      const gate = holdHistoryStream();
      const settings = contextDefault ? page.context() : page;
      settings.setDefaultNavigationTimeout(150);
      const snapshot = vi.spyOn(session, "snapshot");
      try {
        // The server cannot finish until cleanup. This must reject rather than
        // take refs from the early document or wait for the 30s default timeout.
        await expect(runNavigate(session, { url: "back" })).rejects.toThrow(/Timeout/);
        await raceWithTimeout(gate.request, 5000, "history did not request the streamed document");
        expect(snapshot).not.toHaveBeenCalled();
      } finally {
        gate.release();
        snapshot.mockRestore();
        settings.setDefaultNavigationTimeout(30000);
        await session.closeTab(tab);
        await session.selectTab(originalTab);
      }
    });
    it.each([
      [false, false], [true, false], [false, true], [true, true],
    ])("FOLLOWUP nested offscreen frames remain readable (cross-origin: %s, long top: %s)", async (cross, longTop) => {
      const src = new URL("review/nested-parent" + (longTop ? "" : "?tall=1"), httpUrl);
      if (cross) src.hostname = "localhost";
      const page = await load('<p>Top prose</p>' + (longTop ? '<div style="height:6000px"></div>' : "") + `<iframe style="height:150px" src="${src}"></iframe>`);
      const parent = page.mainFrame().childFrames()[0];
      await parent.frameLocator("iframe").getByText("DeepReachableMarker").waitFor({ state: "attached" });
      const child = parent.childFrames()[0];
      expect(await observationRootStatus({ frame: child, primary: false })).toBe("included");
      const result = await runReadText(session);
      expect(body(result)).toMatchObject({ frames_hidden: 0, frames_skipped: 0 });
      expect((result.content[1] as { text: string }).text).toContain("DeepReachableMarker");
    });
    it("FOLLOWUP frame sampling ignores a once-sampled slow top clock", async () => {
      const rows: { kind: string; dispatch: number; settle: number }[] = [];
      for (const slowTop of [false, true]) {
        const page = await load('<button>Sample action</button><p id="slow">Slow clock</p><iframe srcdoc="<p id=c>Frame clock</p>"></iframe>');
        const target = await ref("Sample action");
        const frame = page.mainFrame().childFrames()[0];
        for (let i = 0; i < 3; i++) {
          await frame.evaluate(() => {
            const w = window as unknown as { ticker: number };
            clearInterval(w.ticker);
            // A fast frame proves its baseline within the initial 100ms sample,
            // separating it from the old 300ms slow-top-clock regression.
            w.ticker = setInterval(() => { document.getElementById("c")!.textContent = String(Date.now()); }, 20);
          });
          if (slowTop) await page.evaluate(() => {
            const w = window as unknown as { slowTimer: number };
            clearInterval(w.slowTimer);
            setTimeout(() => {
              document.getElementById("slow")!.textContent = String(Date.now());
              w.slowTimer = setInterval(() => { document.getElementById("slow")!.textContent = String(Date.now()); }, 1000);
            }, 20);
          });
          const result = body(await runAct(session, { kind: "click", ref: target }));
          rows.push({ kind: slowTop ? "frame+slow-top" : "frame", dispatch: result.timings.dispatch_ms, settle: result.timings.settle_ms });
          expect(result).toMatchObject({ settle_capped: false, snapshot_stale: false });
        }
        await page.goto("about:blank");
      }
      console.info("FOLLOWUP_TIMINGS", JSON.stringify(rows));
      const median = (values: number[]) => values.sort((a, b) => a - b)[1];
      const metric = (kind: string, key: "dispatch" | "settle") => median(rows.filter(row => row.kind === kind).map(row => row[key]));
      expect(metric("frame+slow-top", "dispatch")).toBeLessThan(metric("frame", "dispatch") + 100);
      expect(metric("frame+slow-top", "settle")).toBeLessThan(metric("frame", "settle") + 100);
    });
    it("FOLLOWUP empty frame text scans are cached and invalidated by idle light/shadow mutations", async () => {
      const page = await load('<button>Cache action</button><iframe srcdoc="<div id=host></div>"></iframe>');
      const target = await ref("Cache action");
      const frame = page.mainFrame().childFrames()[0];
      await frame.evaluate(() => {
        document.body.insertAdjacentHTML("beforeend", '<div> </div>'.repeat(5000));
        document.getElementById("host")!.attachShadow({ mode: "open" }).innerHTML = '<span> </span>';
        const original = document.createTreeWalker.bind(document);
        const w = window as unknown as { scans: number };
        w.scans = 0;
        document.createTreeWalker = (...args) => { w.scans++; return original(...args); };
      });
      await runAct(session, { kind: "click", ref: target });
      const scans = () => frame.evaluate(() => (window as unknown as { scans: number }).scans);
      const first = await scans();
      expect(first).toBeGreaterThan(0);
      await runAct(session, { kind: "click", ref: target });
      expect(await scans()).toBe(first);
      for (const shadow of [false, true]) {
        const before = await scans();
        await frame.evaluate(shadow => {
          const root = shadow ? document.getElementById("host")!.shadowRoot! : document.body;
          const p = document.createElement("p"); p.textContent = "New frame prose"; root.append(p);
        }, shadow);
        await runAct(session, { kind: "click", ref: target });
        expect(await scans()).toBeGreaterThan(before);
      }
    });
    it("counts only emitted navigate refs after repeated children collapse", async () => {
      const result = await runNavigate(session, { url: httpUrl + "review/repeated" });
      const tree = (result.content[1] as { text: string }).text;
      const emitted = new Set([...tree.matchAll(/\[ref=((?:f\d+)?e\d+)\]/g)].map(match => match[1]));
      expect(tree).toContain("more similar items");
      expect(body(result).ref_count).toBe(emitted.size);
      const raw = await (await session.ensurePage()).locator("body").ariaSnapshot({ mode: "ai" });
      const all = [...raw.matchAll(/\[ref=((?:f\d+)?e\d+)\]/g)].map(match => match[1]);
      expect(emitted.size).toBeLessThan(all.length);
      expect(session.refs.isKnown(all.at(-1)!)).toBe(true);
    });
    it("does not resample a mature plain page across repeated actions", async () => {
      await load('<button>Plain</button><label>Name<input></label>');
      const target = await ref("Plain"), field = await ref("Name");
      for (const input of [
        { kind: "click", ref: target }, { kind: "click", ref: target },
        { kind: "fill", ref: field, value: "name" }, { kind: "type", ref: field, value: "x" },
        { kind: "key", key: "Tab" },
      ] as const) {
        const result = body(await runAct(session, input));
        expect(result).toMatchObject({ ok: true, text_churn_ignored: false });
        expect(result.timings.dispatch_ms).toBeLessThan(90);
      }
    });
    it("does not ignore a newly started ticker that has never mutated before dispatch", async () => {
      const page = await load('<button>Unobserved</button><span id="clock"></span>');
      const target = await ref("Unobserved");
      await page.evaluate(() => { setInterval(() => { document.getElementById("clock")!.textContent = String(Date.now()); }, 100); });
      const result = body(await runAct(session, { kind: "click", ref: target }));
      expect(result).toMatchObject({ ok: true, settle_capped: true, text_churn_ignored: false });
      expect(result.timings.dispatch_ms).toBeLessThan(90);
      await page.goto("about:blank");
    });
    it("does not retain replaced iframe documents while the agent is idle", async () => {
      const page = await load('<button>Idle</button><div id="frames"><iframe srcdoc="<p>Initial</p>"></iframe></div>');
      await runAct(session, { kind: "click", ref: await ref("Idle") }); // active frame watch -> passive
      const cdp = await page.context().newCDPSession(page);
      await cdp.send("Performance.enable");
      async function documents() {
        await cdp.send("HeapProfiler.collectGarbage");
        const metrics = (await cdp.send("Performance.getMetrics")).metrics;
        return metrics.find(metric => metric.name === "Documents")!.value;
      }
      const before = await documents();
      await page.evaluate(() => new Promise<void>(resolve => {
        let n = 0;
        const timer = setInterval(() => {
          document.getElementById("frames")!.innerHTML = `<iframe srcdoc="<p>Frame ${n}</p>"></iframe>`;
          if (++n === 20) { clearInterval(timer); resolve(); }
        }, 50);
      }));
      expect(await documents()).toBeLessThanOrEqual(before + 4);
      // The next active scan must still pick up the surviving frame.
      await page.evaluate(() => {
        document.querySelector("button")!.onclick = () => {
          document.querySelector("iframe")!.contentDocument!.body.innerHTML = '<button>Frame result</button>'.repeat(15);
        };
      });
      const result = await runAct(session, { kind: "click", ref: await ref("Idle") });
      expect(body(result)).toMatchObject({ dom_mutated: "major", snapshot_stale: true });
      expect(result.content[1]).toMatchObject({ text: expect.stringContaining("Frame result") });
      await cdp.detach();
    });
    it.each(["timer", "raf", "fetch"])("settles a %s text result on a ticker once non-baseline text is quiet", async kind => {
      const trigger = kind === "timer" ? "setTimeout(done,150)" : kind === "fetch" ? "fetch('/hydrate').then(r=>r.text()).then(done)" : "const start=performance.now();(function tick(){performance.now()-start>=150?done():requestAnimationFrame(tick)})()";
      const page = await load(`<button onclick="go()">Save label</button><p id="status">Waiting</p><span id="clock"></span><script>setInterval(()=>clock.textContent=String(Date.now()),100);function go(){const done=()=>{document.getElementById('status').textContent='Saved';window.done=true};${trigger}}</script>`);
      await page.waitForTimeout(250); // the clock, unlike the result, is already churning
      const result = body(await runAct(session, { kind: "click", ref: await ref("Save label") }));
      expect(result).toMatchObject({ ok: true, dom_mutated: "minor", settle_capped: false, text_churn_ignored: true });
      expect(await page.locator("#status").innerText()).toBe("Saved");
      expect(result.timings.settle_ms).toBeGreaterThanOrEqual(kind === "fetch" ? 650 : 450);
      expect(result.timings.settle_ms).toBeLessThan(1200);
      await page.goto("about:blank");
    });
    it.each(["plain", "top", "shadow", "frame", "staticshadow", "staticframe", "blankframe", "crossframe"])("avoids repeat sampling for spaced %s actions", async kind => {
      const shapes: Record<string, string> = {
        plain: '<p>Plain</p>',
        top: '<span id="clock"></span><script>setInterval(()=>clock.textContent=String(Date.now()),100)</script>',
        shadow: '<div id="host"></div><script>const r=host.attachShadow({mode:"open"});r.innerHTML="<span id=c></span>";setInterval(()=>r.getElementById("c").textContent=String(Date.now()),100)</script>',
        frame: '<iframe srcdoc="<span id=c></span><script>setInterval(()=>c.textContent=String(Date.now()),100)</script>"></iframe>',
        staticshadow: '<div id="host"></div><script>host.attachShadow({mode:"open"}).innerHTML="<span>Static</span>"</script>',
        staticframe: '<iframe srcdoc="<p>Static frame</p>"></iframe>',
        blankframe: '<iframe style="display:none"></iframe>',
        crossframe: `<iframe src="${new URL("inner-observation", httpUrl).href.replace("127.0.0.1", "localhost")}"></iframe>`,
      };
      const page = await load('<button>Shape action</button>' + shapes[kind]);
      await page.waitForTimeout(400);
      const rows = [];
      for (let i = 0; i < 3; i++) {
        await page.waitForTimeout(1000);
        const result = body(await runAct(session, { kind: "click", ref: await ref("Shape action") }));
        rows.push({ dispatch: Math.round(result.timings.dispatch_ms), settle: Math.round(result.timings.settle_ms), capped: result.settle_capped, dom: result.dom_mutated, stale: result.snapshot_stale });
      }
      console.info("ROOT_DISPATCH", JSON.stringify({ kind, rows }));
      await page.goto("about:blank");
      for (let i = 0; i < rows.length; i++) {
        expect(rows[i]).toMatchObject({ capped: false, stale: false });
        // Frame tickers prove their baseline afresh each action; static frames
        // sample once, and passive/empty roots do not pay repeated samples.
        if (kind === "frame") expect(rows[i].dispatch).toBeLessThan(450);
        else if (i > 0 || kind !== "staticframe") expect(rows[i].dispatch).toBeLessThan(90);
      }
    }, 15000);
    for (const where of ["top", "frame"]) it(`REVIEWSTALE ${where} once-churned node later streams`, async () => {
      const inner = '<p id=s>Init</p>';
      const page = await load('<button id=b>Go</button>' + (where === "frame" ? `<iframe srcdoc="${inner}"></iframe>` : inner));
      await page.waitForTimeout(400);
      const docOf = where === "frame" ? "document.querySelector('iframe').contentDocument" : "document";
      // a brief ticker that is running during action 1's sample, then stops
      await page.evaluate(`(()=>{const d=${docOf};let n=0;window.t=setInterval(()=>{d.getElementById('s').textContent='Connecting'+'.'.repeat(n++%4)},20)})()`);
      await page.waitForTimeout(150);
      const r1 = body(await runAct(session, { kind: "click", ref: await ref("Go") }));
      await page.evaluate(`clearInterval(window.t); ${docOf}.getElementById('s').textContent='Ready'`);
      await page.waitForTimeout(2000);
      await page.evaluate(`document.getElementById('b').onclick=()=>{const d=${docOf};let n=0;window.done=false;const t=setInterval(()=>{d.getElementById('s').textContent+=' tok'+n;if(++n>=15){clearInterval(t);d.body.insertAdjacentHTML('beforeend','<button>Answer</button>'.repeat(15));window.done=true}},60)}`);
      const started = Date.now();
      const response = await runAct(session, { kind: "click", ref: await ref("Go") });
      const r2 = body(response);
      const done = await page.evaluate(() => (window as unknown as { done: boolean }).done);
      console.log("REVIEWSTALE", where, JSON.stringify({ r1: { d: Math.round(r1.timings.dispatch_ms), s: Math.round(r1.timings.settle_ms), ign: r1.text_churn_ignored }, r2: { ms: Date.now() - started, s: Math.round(r2.timings.settle_ms), dom: r2.dom_mutated, stale: r2.snapshot_stale, capped: r2.settle_capped, ign: r2.text_churn_ignored, doneAtReturn: done } }));
      expect(r2).toMatchObject({ dom_mutated: "major", snapshot_stale: true, settle_capped: false });
      expect(done).toBe(true);
      expect(r2.text_churn_ignored).not.toBe(true);
      expect(response.content[1]).toMatchObject({ text: expect.stringContaining("Answer") });
      await page.goto("about:blank");
    }, 20000);
    it("recovers a late-starting frame clock after at most one capped action", async () => {
      const page = await load('<button>Late frame</button><iframe srcdoc="<p>Static</p><span id=clock></span>"></iframe>');
      await runAct(session, { kind: "click", ref: await ref("Late frame") }); // sample the static document once
      await page.mainFrame().childFrames()[0].evaluate(() => { setInterval(() => { document.getElementById("clock")!.textContent = String(Date.now()); }, 100); });
      const rows = [];
      for (let i = 0; i < 3; i++) {
        await page.waitForTimeout(1000);
        rows.push(body(await runAct(session, { kind: "click", ref: await ref("Late frame") })));
      }
      console.info("LATE_FRAME_CLOCK", JSON.stringify(rows.map(row => ({ capped: row.settle_capped, dispatch: row.timings.dispatch_ms, settle: row.timings.settle_ms }))));
      await page.goto("about:blank");
      expect(rows[0]).toMatchObject({ settle_capped: true, text_churn_ignored: false });
      for (const row of rows.slice(1)) expect(row).toMatchObject({ settle_capped: false, text_churn_ignored: true, dom_mutated: "minor", snapshot_stale: false });
      expect(rows[2].timings.dispatch_ms).toBeLessThan(450); // ticker evidence is per-action
    }, 15000);
    it("prunes detached passive shadow observers and observes roots attached while idle", async () => {
      const page = await load('<button>Shadow lifecycle</button><div id="hosts"></div>');
      await runAct(session, { kind: "click", ref: await ref("Shadow lifecycle") });
      const cdp = await page.context().newCDPSession(page);
      const nodes = async () => { await cdp.send("HeapProfiler.collectGarbage"); return (await cdp.send("Memory.getDOMCounters")).nodes; };
      const before = await nodes();
      await page.evaluate(() => new Promise<void>(resolve => {
        let n = 0;
        const interval = setInterval(() => {
          const hosts = document.getElementById("hosts")!;
          hosts.innerHTML = '<div></div>';
          hosts.firstElementChild!.attachShadow({ mode: "open" }).innerHTML = '<span>Static shadow</span>';
          // A never-inserted host must not acquire a retained passive observer.
          document.createElement("div").attachShadow({ mode: "open" }).innerHTML = '<p>Detached</p>';
          if (++n === 30) { clearInterval(interval); hosts.innerHTML = ''; resolve(); }
        }, 20);
      }));
      expect(await nodes()).toBeLessThanOrEqual(before + 25);
      await page.evaluate(() => {
        const root = document.getElementById("hosts")!.attachShadow({ mode: "open" });
        root.innerHTML = '<span id="clock"></span>';
        setInterval(() => { root.querySelector("span")!.textContent = String(Date.now()); }, 100);
      });
      await page.waitForTimeout(400);
      const result = body(await runAct(session, { kind: "click", ref: await ref("Shadow lifecycle") }));
      expect(result).toMatchObject({ settle_capped: false, text_churn_ignored: true, dom_mutated: "minor", snapshot_stale: false });
      expect(result.timings.dispatch_ms).toBeLessThan(450); // idle late attachment gets its first sample
      await cdp.detach();
      await page.goto("about:blank");
    });
    it("restores native attachShadow while idle and samples an idle late-attached clock", async () => {
      const page = await load('<button>Native shadow</button><div id="late"></div>');
      const native = () => page.evaluate(() => Element.prototype.attachShadow.toString().includes("[native code]"));
      expect(await native()).toBe(true);
      await runAct(session, { kind: "click", ref: await ref("Native shadow") });
      expect(await native()).toBe(true);
      await page.evaluate(() => {
        const root = document.getElementById("late")!.attachShadow({ mode: "open" });
        root.innerHTML = '<p>Idle clock</p>';
        setInterval(() => { root.querySelector("p")!.textContent = String(Date.now()); }, 60);
      });
      await page.waitForTimeout(400);
      const result = body(await runAct(session, { kind: "click", ref: await ref("Native shadow") }));
      expect(result).toMatchObject({ settle_capped: false, snapshot_stale: false, text_churn_ignored: true });
      expect(result.timings.dispatch_ms).toBeGreaterThanOrEqual(100);
      expect(await native()).toBe(true);
      await page.goto("about:blank");
    });
    it.each(["light", "shadow"])("skips whitespace-only frames but samples a script-filled %s blank frame when text arrives", async kind => {
      const page = await load('<button>Blank frame</button><iframe srcdoc="   "></iframe>');
      await page.waitForTimeout(400);
      const first = body(await runAct(session, { kind: "click", ref: await ref("Blank frame") }));
      expect(first.timings.dispatch_ms).toBeLessThan(90);
      await page.mainFrame().childFrames()[0].evaluate(kind => {
        const root = kind === "shadow" ? document.body.attachShadow({ mode: "open" }) : document.body;
        root.innerHTML = '<p id="clock">Clock</p>';
        setInterval(() => { root.querySelector("p")!.textContent = String(Date.now()); }, 60);
      }, kind);
      await page.waitForTimeout(400);
      const second = body(await runAct(session, { kind: "click", ref: await ref("Blank frame") }));
      expect(second).toMatchObject({ settle_capped: false, snapshot_stale: false, text_churn_ignored: true });
      expect(second.timings.dispatch_ms).toBeGreaterThanOrEqual(100);
      await page.goto("about:blank");
    });
    it("finishes a new frame sample discovered during the initial sample wait", async () => {
      // Test the browser-side sampling/settling algorithm with a virtual clock.
      // A Node->CDP dispatch can itself take >60ms on a shared runner, so a
      // timer scheduled before runAct did not guarantee arrival DURING sampling.
      // Isolate the clock from the session and its other pages.
      const context = await (await session.ensurePage()).context().browser()!.newContext();
      const page = await context.newPage();
      try {
        await page.setContent('<iframe srcdoc="<p>Initial</p>"></iframe><div id="spawn"></div>');
        await page.mainFrame().childFrames()[0].getByText("Initial").waitFor();
        await page.clock.install({ time: 0 });
        await page.clock.pauseAt(1000);
        // Let MutationObserver microtasks run between timer ticks, rather than
        // delivering an entire virtual second's records at the same timestamp.
        const advance = async (ms: number) => {
          for (let elapsed = 0; elapsed < ms; elapsed += 10) {
            await page.clock.runFor(10);
            await page.evaluate(() => {});
          }
        };
        const sampling = page.evaluate(installMutationWatcher, WATCH_KEY);
        await expect.poll(() => page.evaluate(key => (window as unknown as Record<string, { sampling: boolean }>)[key]?.sampling, WATCH_KEY)).toBe(true);
        await page.clock.runFor(60);
        await page.evaluate(() => {
          document.getElementById("spawn")!.innerHTML = '<iframe srcdoc="<span id=c>Clock</span>"></iframe>';
        });
        const frame = page.mainFrame().childFrames()[1];
        await frame.getByText("Clock", { exact: true }).waitFor();
        // First write at 140ms (after the original 100ms sample), second at
        // 220ms. Discovery must extend sampling; neither write is on a boundary.
        await frame.evaluate(() => { setInterval(() => { document.getElementById("c")!.textContent = String(Date.now()); }, 80); });
        await advance(250);
        await raceWithTimeout(sampling, 2000, "virtual initial frame sampling did not complete");
        // Drive the SAME collector used by Session.settle, keeping the ticker
        // running throughout. It must settle on baseline text, not hit its cap.
        const settling = page.evaluate(collectMutationStats, [WATCH_KEY, 300, 1500]);
        await expect.poll(() => page.evaluate(key => (window as unknown as Record<string, { deadline: number }>)[key].deadline, WATCH_KEY)).toBeGreaterThan(0);
        await advance(1600);
        const stats = await raceWithTimeout(settling, 2000, "virtual settling did not complete");
        expect(stats).toMatchObject({ settleCapped: false, textChurnIgnored: true });
        expect(classifyMutations(stats)).toBe("minor");
      } finally {
        await context.close();
      }
    });
    it.each(["top", "shadow", "frame", "mixed"])("baselines an idle %s ticker across spaced actions", async kind => {
      const top = '<span id="clock"></span><script>setInterval(()=>clock.textContent=String(Date.now()),100)</script>';
      const shadow = '<div id="host"></div><script>const r=host.attachShadow({mode:"open"});r.innerHTML="<span id=c></span>";setInterval(()=>r.getElementById("c").textContent=String(Date.now()),100)</script>';
      const frame = '<iframe srcdoc="<span id=c></span><script>setInterval(()=>c.textContent=String(Date.now()),100)</script>"></iframe>';
      const page = await load('<button>Spaced action</button>' + (kind === "top" ? top : kind === "shadow" ? shadow : kind === "frame" ? frame : top + shadow));
      await page.waitForTimeout(250);
      const rows = [];
      for (let i = 0; i < 3; i++) {
        await page.waitForTimeout(1000); // no frame ticker history is collected while idle
        const target = await ref("Spaced action");
        const start = Date.now();
        const result = body(await runAct(session, { kind: "click", ref: target }));
        rows.push({ ms: Date.now() - start, settle_ms: Math.round(result.timings.settle_ms), dispatch_ms: Math.round(result.timings.dispatch_ms),
          ok: result.ok, settle_capped: result.settle_capped, dom_mutated: result.dom_mutated, snapshot_stale: result.snapshot_stale, text_churn_ignored: result.text_churn_ignored });
      }
      console.info("SECONDARY_ROOT_TICKER", JSON.stringify({ kind, rows }));
      await page.goto("about:blank");
      for (const row of rows) expect(row).toMatchObject({ ok: true, settle_capped: false, dom_mutated: "minor", snapshot_stale: false, text_churn_ignored: true });
    }, 15000);
    it("settles a 100ms text ticker quickly without navigation or expiring refs", async () => {
      const page = await load('<button>Tick action</button><span id="clock"></span><script>setInterval(()=>clock.textContent=String(Date.now()),100)</script>');
      await page.waitForTimeout(200); // an already-running ticker, outside act timing
      const target = await ref("Tick action");
      const start = Date.now();
      const result = body(await runAct(session, { kind: "click", ref: target }));
      expect(result).toMatchObject({ ok: true, url_changed: false, settle_capped: false, snapshot_stale: false, dom_mutated: "minor", text_churn_ignored: true });
      expect(Date.now() - start).toBeLessThan(800);
      expect(result.timings.settle_ms).toBeGreaterThanOrEqual(300);
      expect(result.timings.settle_ms).toBeLessThan(500);
      expect(await (await session.resolveRef(target)).count()).toBe(1);
      await page.goto("about:blank"); // stop the ticker before the next test
    });
    const actionTextCases = [
      { name: "rAF result count", html: `<button onclick="go()">Start</button><p id="count">0 results</p><ul id="out"></ul><script>function go(){const start=performance.now();(function tick(){const n=Math.min(20,Math.floor((performance.now()-start)/45));count.textContent=n+' results';if(n<20)requestAnimationFrame(tick);else{out.innerHTML=Array.from({length:20},(_,i)=>'<li><a href=#'+i+'>Result '+i+'</a></li>').join('');window.done=true}})()}</script>`, expected: "Result 0" },
      { name: "Saving typewriter", html: `<button onclick="go()">Start</button><p id="progress"></p><div id="out"></div><script>function go(){let n=0;const timer=setInterval(()=>{progress.textContent='Saving'+'.'.repeat(n++%4);if(n>=15){clearInterval(timer);progress.textContent='Saved';out.innerHTML='<button>View result</button>'.repeat(15);window.done=true}},60)}</script>`, expected: "View result" },
      { name: "chat token stream into an action-created paragraph", html: `<button onclick="go()">Start</button><div id="out"></div><script>function go(){out.innerHTML='<div>Question</div><div><p id="answer"></p></div>';let n=0;const timer=setInterval(()=>{answer.textContent+='token '+n+++' ';if(n>=18){clearInterval(timer);out.insertAdjacentHTML('beforeend','<button>Regenerate</button><button>Copy</button>');window.done=true}},60)}</script>`, expected: "Regenerate" },
    ];
    for (const background of [false, true]) {
      it.each(actionTextCases)(`observes action-started $name through completion (background ticker: ${background})`, async ({ html, expected }) => {
        const page = await load(html + (background ? '<span id="clock"></span><script>setInterval(()=>clock.textContent=String(Date.now()),100)</script>' : ''));
        if (background) await page.waitForTimeout(200);
        const started = Date.now();
        const result = await runAct(session, { kind: "click", ref: await ref("Start") });
        expect(body(result)).toMatchObject({ ok: true, dom_mutated: "major", snapshot_stale: true, settle_capped: false, text_churn_ignored: background });
        expect(await page.evaluate(() => (window as unknown as { done: boolean }).done)).toBe(true);
        expect(Date.now() - started).toBeGreaterThanOrEqual(900);
        expect(result.content[1]).toMatchObject({ text: expect.stringContaining(expected) });
        // Snapshot lists intentionally collapse after five siblings. Verify all
        // twenty produced results on the page, not just the emitted preview.
        if (expected === "Result 0") expect(await page.getByRole("link").count()).toBe(20);
        await page.goto("about:blank");
      });
    }
    it("reuses pre-action ticker history after settle without retaining active timer hooks", async () => {
      const page = await load('<button>Tick again</button><span id="clock"></span><script>window.originalSetTimeout=window.setTimeout;setInterval(()=>clock.textContent=String(Date.now()),100)</script>');
      await page.waitForTimeout(200);
      const target = await ref("Tick again");
      for (let i = 0; i < 3; i++) {
        const started = Date.now();
        expect(body(await runAct(session, { kind: "click", ref: target }))).toMatchObject({ text_churn_ignored: true, settle_capped: false, snapshot_stale: false });
        expect(Date.now() - started).toBeLessThan(i === 0 ? 800 : 500);
        expect(await page.evaluate(() => window.setTimeout === (window as unknown as { originalSetTimeout: typeof window.setTimeout }).originalSetTimeout)).toBe(true);
      }
      await page.goto("about:blank");
    });
    it("handles repeated 5ms script redirects without poisoning later navigations", async () => {
      for (let i = 0; i < 5; i++) {
        const result = body(await runNavigate(session, { url: httpUrl + "review/jsredir" }));
        expect(result.ok).toBe(true);
        expect(result.url).toBe(httpUrl + "review/single");
        expect(result.ref_count).toBeGreaterThan(0);
      }
      expect(body(await runNavigate(session, { url: httpUrl + "review/single" })).ok).toBe(true);
    });
    it.each(["svg", "img", "canvas", "hidden", "spinner"])("does not mistake a %s loading shell for hydration", async kind => {
      const started = Date.now();
      const result = await runNavigate(session, { url: httpUrl + "review/loading?kind=" + kind });
      expect(body(result).ref_count).toBeGreaterThan(0);
      expect(result.content[1]).toMatchObject({ text: expect.stringContaining("Hydrated") });
      expect(Date.now() - started).toBeGreaterThanOrEqual(550);
      expect(Date.now() - started).toBeLessThan(2200);
    });
    it("reports ignored text churn on same-document control-free navigation as well as actions", async () => {
      const page = await load('<p id="clock">Clock</p><script>setInterval(()=>clock.textContent=String(Date.now()),100)</script>');
      await page.waitForTimeout(200);
      const result = body(await runNavigate(session, { url: page.url() + "#clock" }));
      expect(result).toMatchObject({ ok: true, ref_count: 0, text_churn_ignored: true });
      expect(result.timings.settle_ms).toBeLessThan(800);
      await (await session.ensurePage()).goto("about:blank");
    });
    it("caps unobserved text churn in a fresh cross-document control-free navigation", async () => {
      const page = await session.ensurePage();
      const before = await page.evaluate(() => performance.timeOrigin);
      const url = 'data:text/html,' + encodeURIComponent('<p id="clock">Fresh clock</p><script>setInterval(()=>clock.textContent=String(Date.now()),100)</script>');
      // Navigate exposes the churn flag/timing, not settle_capped. Observe its
      // real quiet-window result without replacing the producing implementation.
      const observation = vi.spyOn(session, "settle");
      try {
        const result = body(await runNavigate(session, { url }));
        expect(await page.evaluate(() => performance.timeOrigin)).not.toBe(before);
        expect(observation).toHaveBeenCalledTimes(1);
        expect(await observation.mock.results[0].value).toMatchObject({ settleCapped: true, textChurnIgnored: false });
        expect(result).toMatchObject({ ok: true, ref_count: 0, text_churn_ignored: false });
        expect(result.timings.settle_ms).toBeGreaterThanOrEqual(1400);
      } finally {
        observation.mockRestore();
        await page.goto("about:blank");
      }
    });
    it("gives prose with CSS-only animation a short quiet wait rather than a 1.5s control wait", async () => {
      const page = await session.ensurePage();
      const url = 'data:text/html,' + encodeURIComponent('<style>@keyframes spin{to{transform:rotate(360deg)}}i{display:inline-block;animation:spin .1s infinite}</style><p>Rendered article content, without any actionable controls.</p><i>◌</i>');
      const result = body(await runNavigate(session, { url }));
      expect(result.ref_count).toBe(0);
      expect(result.timings.settle_ms).toBeGreaterThanOrEqual(300);
      expect(result.timings.settle_ms).toBeLessThan(600);
      expect(await page.locator("p").innerText()).toContain("Rendered article");
    });
    it.each(["reload", "slowreload", "latereload"])("refreshes refs after a same-URL %s", async path => {
      await runNavigate(session, { url: httpUrl + "review/" + path });
      const page = await session.ensurePage();
      const before = await page.evaluate(() => performance.timeOrigin);
      const result = await runAct(session, { kind: "click", ref: await ref(path === "latereload" ? "Later" : "Save") });
      expect(body(result)).toMatchObject({ url_changed: false, dom_mutated: "major", snapshot_stale: true, settle_capped: false });
      if (path === "latereload") expect(body(result).observation_failed).toBe(true);
      expect(result.content[1]).toMatchObject({ text: expect.stringContaining(path === "latereload" ? "Later" : "Save") });
      expect(await page.evaluate(() => performance.timeOrigin)).not.toBe(before);
    });
    it("does not claim none when a 3s blocked main thread prevents observation", async () => {
      const page = await load(`<button onclick="setTimeout(()=>{const t=Date.now();while(Date.now()-t<3000);out.innerHTML='<button>Busy result</button>'.repeat(20)},0)">Busy</button><div id="out"></div>`);
      const result = await runAct(session, { kind: "click", ref: await ref("Busy") });
      expect(body(result)).toMatchObject({ ok: true, dom_mutated: "major", snapshot_stale: true });
      expect(result.content[1]).toMatchObject({ text: expect.stringContaining("Busy result") });
      expect(await page.locator("#out button").count()).toBe(20);
    }, 10000);
    it("ignores a 5s toast dismissal without skipping a short 700ms render", async () => {
      await load(`<button onclick="out.textContent='Saved';setTimeout(()=>out.textContent='',5000)">Toast</button><div id="out"></div>`);
      const result = body(await runAct(session, { kind: "click", ref: await ref("Toast") }));
      expect(result).toMatchObject({ settle_capped: false, pending_timers: 0, dom_mutated: "minor" });
      expect(result.timings.settle_ms).toBeLessThan(500);
      await load(`<button onclick="setTimeout(()=>out.textContent='',5000);setTimeout(()=>out.innerHTML='<button>Late result</button>'.repeat(20),700)">Both timers</button><div id="out"></div>`);
      expect(body(await runAct(session, { kind: "click", ref: await ref("Both timers") }))).toMatchObject({ dom_mutated: "major", snapshot_stale: true, settle_capped: false });
    });
    it("still caps attribute churn and wide text churn rather than treating them as a stable ticker", async () => {
      const page = await load('<button>Churn</button><div id="out"></div><script>setInterval(()=>out.dataset.tick=String(Date.now()),100)</script>');
      const result = body(await runAct(session, { kind: "click", ref: await ref("Churn") }));
      expect(result).toMatchObject({ settle_capped: true, url_changed: false, dom_mutated: "minor" });
      await page.goto("about:blank");
      await load('<button>Churn</button><div id="out">' + '<span>old</span>'.repeat(6) + '</div><script>setInterval(()=>document.querySelectorAll("span").forEach(el=>el.textContent=String(Date.now())),100)</script>');
      expect(body(await runAct(session, { kind: "click", ref: await ref("Churn") })).settle_capped).toBe(true);
      await page.goto("about:blank");
    }, 10000);
    it("omits evidence when scrolling removes the virtualised source row", async () => {
      const page = await load(`<div role="region" aria-label="List" id="sc" style="height:100px;overflow:auto"><div id="inner" style="height:3000px;position:relative"></div></div><script>function render(){const start=Math.floor(sc.scrollTop/20);inner.innerHTML='';for(let i=start;i<start+6;i++){const row=document.createElement('div');row.textContent='Row '+i;row.role='button';row.style.cssText='position:absolute;top:'+(i*20)+'px';inner.appendChild(row)}}sc.onscroll=render;render()</script>`);
      const target = await ref("Row 0");
      const start = Date.now();
      const result = body(await runAct(session, { kind: "scroll", ref: target, direction: "down", amount: 1000 }));
      expect(result.ok).toBe(true);
      expect(result.scroll).toBeUndefined();
      expect(Date.now() - start).toBeLessThan(2000);
      expect(await page.getByRole("button", { name: "Row 0", exact: true }).count()).toBe(0);
    });
    it("bounds fill evidence on replaced inputs and select pre-checks on removed refs", async () => {
      const page = await load(`<label>Size<select id="size"><option value="s">Small</option><option value="m">Medium</option></select></label><label>Name<input oninput="const clone=this.cloneNode();clone.value=this.value;this.replaceWith(clone)"></label>`);
      const input = await ref("Name"), select = await ref("Size");
      let start = Date.now();
      const filled = body(await runAct(session, { kind: "fill", ref: input, value: "hello" }));
      expect(filled.ok).toBe(true);
      expect(filled.actual_value).toBeUndefined();
      expect(Date.now() - start).toBeLessThan(1500);
      expect(await page.getByRole("textbox", { name: "Name" }).inputValue()).toBe("hello");
      await page.locator("#size").evaluate(el => el.remove());
      start = Date.now();
      expect(body(await runAct(session, { kind: "select", ref: select, value: "m" }))).toMatchObject({ code: "stale_ref", outcome: "not_executed" });
      expect(Date.now() - start).toBeLessThan(3500);
    });
    it("reports a covered ARIA combobox as not executed, but uses unknown after opening it", async () => {
      const page = await load('<button role="combobox" aria-label="Covered combo" aria-controls="choices" onclick="choices.hidden=false">Choose</button><div id="choices" role="listbox" hidden><div role="option">Known</div></div><div id="overlay" style="position:fixed;inset:0"></div>');
      const target = await ref("Covered combo");
      expect(body(await runAct(session, { kind: "select", ref: target, value: "Known" }))).toMatchObject({ code: "not_actionable", outcome: "not_executed" });
      await page.locator("#overlay").evaluate(el => el.remove());
      const missing = body(await runAct(session, { kind: "select", ref: target, value: "Missing" }));
      expect(missing).toMatchObject({ code: "outcome_unknown", outcome: "unknown" });
      expect(await page.locator("#choices").isVisible()).toBe(true);
    }, 10000);
    it("echoes only capped native fill mismatches, not matching values or contenteditable text", async () => {
      await load('<label>Plain<input></label><label>Mask<input oninput="this.value+=\'x\'.repeat(700)"></label><div contenteditable="true" role="textbox" aria-label="Editor"></div>');
      const plain = body(await runAct(session, { kind: "fill", ref: await ref("Plain"), value: "match" }));
      expect(plain.value_matches).toBe(true);
      expect(plain.actual_value).toBeUndefined();
      const mask = body(await runAct(session, { kind: "fill", ref: await ref("Mask"), value: "a" }));
      expect(mask).toMatchObject({ value_matches: false, actual_value_truncated: true });
      expect(mask.actual_value).toHaveLength(500);
      const editor = body(await runAct(session, { kind: "fill", ref: await ref("Editor"), value: "a\nb" }));
      expect(editor.ok).toBe(true);
      expect(editor.actual_value).toBeUndefined();
      expect(editor.note).toBeUndefined();
    });
    it("waits for a timer-rendered result 700ms after the click", async () => {
      const page = await load('<button onclick="setTimeout(()=>out.innerHTML=\'<button>Result</button>\'.repeat(20),700)">Late</button><div id="out"></div>');
      const result = body(await runAct(session, { kind: "click", ref: await ref("Late") }));
      expect(result).toMatchObject({ dom_mutated: "major", snapshot_stale: true, pending_timers: 0 });
      expect(await page.locator("#out button").count()).toBe(20);
    });
    it("starts the quiet window after dispatch, not watcher installation", async () => {
      await load('<button>Ready</button>');
      await session.beginWatch();
      await delay(400); // slow dispatch/actionability
      const start = Date.now();
      expect(await session.settle()).toMatchObject({ domMutated: "none" });
      expect(Date.now() - start).toBeGreaterThanOrEqual(290);
    });
    it("waits for action-started network work and reports pending work at the cap", async () => {
      await load(`<button onclick="fetch('/action-late').then(r=>r.text()).then(h=>out.innerHTML=h)">Fetch</button><div id="out"></div>`);
      const result = body(await runAct(session, { kind: "click", ref: await ref("Fetch") }));
      expect(result).toMatchObject({ dom_mutated: "major", snapshot_stale: true, pending_requests: 0 });
      await load(`<button onclick="fetch('/action-pending').then(r=>r.text()).then(h=>out.innerHTML=h)">Fetch</button><div id="out"></div>`);
      const pending = body(await runAct(session, { kind: "click", ref: await ref("Fetch") }));
      expect(pending.settle_capped).toBe(true);
      expect(pending.pending_requests).toBeGreaterThan(0);
    });
    it.each(["frame", "shadow"])("observes structural changes inside an open %s", async kind => {
      const page = await load('<div id="host"></div><iframe srcdoc="<button>Frame action</button><div id=out></div>"></iframe>');
      await page.evaluate(kind => {
        if (kind === "shadow") {
          const root = document.querySelector("#host")!.attachShadow({ mode: "open" });
          root.innerHTML = '<button>Shadow action</button><div id="out"></div>';
          root.querySelector("button")!.onclick = () => { root.querySelector("#out")!.innerHTML = '<button>New</button>'.repeat(20); };
        } else {
          const doc = document.querySelector("iframe")!.contentDocument!;
          doc.querySelector("button")!.onclick = () => { doc.querySelector("#out")!.innerHTML = '<button>New</button>'.repeat(20); };
        }
      }, kind);
      const result = body(await runAct(session, { kind: "click", ref: await ref(kind === "shadow" ? "Shadow action" : "Frame action") }));
      expect(result).toMatchObject({ dom_mutated: "major", snapshot_stale: true });
    });
    it("observes an open shadow root attached during the action", async () => {
      await load(`<button onclick="host.attachShadow({mode:'open'}).innerHTML='<button>New shadow control</button>'.repeat(20)">Attach shadow</button><div id="host"></div>`);
      expect(body(await runAct(session, { kind: "click", ref: await ref("Attach shadow") }))).toMatchObject({ dom_mutated: "major", snapshot_stale: true });
    });
    it("uses aliases, rejects unknown keys and malformed dates as input errors", async () => {
      const page = await load('<label>Entry<input id="entry" onkeydown="this.dataset.key=event.key"></label><label>Date<input type="date"></label><button>Not select</button>');
      const entry = await ref("Entry");
      expect(body(await runAct(session, { kind: "key", ref: entry, key: "Return" })).ok).toBe(true);
      expect(await page.locator("#entry").getAttribute("data-key")).toBe("Enter");
      expect(body(await runAct(session, { kind: "key", ref: entry, key: "Esc" })).ok).toBe(true);
      for (const input of [
        { kind: "key", ref: entry, key: "NoSuchKey" },
        { kind: "fill", ref: await ref("Date"), value: "10/20/2026" },
        { kind: "select", ref: await ref("Not select"), value: "x" },
      ] as const) expect(body(await runAct(session, input)).code).toBe("invalid_input");
    });
    it.each(["click", "dblclick", "hover"] as const)("holds Ctrl for %s and releases it afterwards", async kind => {
      const page = await load('<button onmousedown="this.dataset.ctrl=event.ctrlKey" onmousemove="this.dataset.ctrl=event.ctrlKey">Modified</button>');
      expect(body(await runAct(session, { kind, ref: await ref("Modified"), modifiers: ["Ctrl"] })).ok).toBe(true);
      expect(await page.locator("button").getAttribute("data-ctrl")).toBe("true");
      // Acknowledge listener installation before sending the key. Starting an
      // unresolved evaluate and press concurrently can lose the event entirely.
      await page.evaluate(() => {
        document.addEventListener("keydown", event => { document.body.dataset.nextCtrl = String(event.ctrlKey); }, { once: true });
      });
      await page.keyboard.press("x");
      expect(await page.locator("body").getAttribute("data-next-ctrl")).toBe("false");
    });
    it("selects multiple native options by value or label and validates bad values", async () => {
      const page = await load('<label>Toppings<select multiple><option value="h">Ham</option><option value="o">Olives</option></select></label>');
      const target = await ref("Toppings");
      expect(body(await runAct(session, { kind: "select", ref: target, values: ["Ham", "o"] })).ok).toBe(true);
      expect(await page.locator("select").evaluate(el => Array.from((el as HTMLSelectElement).selectedOptions).map(o => o.value))).toEqual(["h", "o"]);
      const bad = body(await runAct(session, { kind: "select", ref: target, value: "Ham,Olives" }));
      expect(bad).toMatchObject({ code: "invalid_input", outcome: "not_executed" });
      expect(bad.error).toContain("values:");
    });
    it("adds to a native selection with Ctrl-click rather than replacing it", async () => {
      const page = await load('<select multiple aria-label="Toppings"><option>Ham</option><option>Olives</option></select>');
      await runAct(session, { kind: "click", ref: await ref("Ham") });
      expect(body(await runAct(session, { kind: "click", ref: await ref("Olives"), modifiers: ["Ctrl"] })).ok).toBe(true);
      expect(await page.locator("select").evaluate(el => Array.from((el as HTMLSelectElement).selectedOptions).map(o => o.value))).toEqual(["Ham", "Olives"]);
    });
    it("selects an exact label from ARIA comboboxes and listboxes", async () => {
      const page = await load('<button role="combobox" aria-label="Country" aria-controls="list" onclick="list.hidden=false">Country</button><div role="listbox" aria-label="Choices" id="list" hidden><div role="option" onclick="document.body.dataset.selected=this.textContent">China</div></div>');
      expect(body(await runAct(session, { kind: "select", ref: await ref("Country"), value: "China" })).ok).toBe(true);
      expect(await page.locator("body").getAttribute("data-selected")).toBe("China");
      expect(body(await runAct(session, { kind: "select", ref: await ref("Choices"), value: "China" })).ok).toBe(true);
    });
    it("drags between refs without a screenshot, including hold/drop delay", async () => {
      const page = await load('<button style="position:absolute;left:10px;top:10px">Source</button><button style="position:absolute;left:250px;top:10px" onmouseup="this.dataset.drop=1">Destination</button>');
      const result = body(await runAct(session, { kind: "drag", ref: await ref("Source"), to_ref: await ref("Destination"), hold_ms: 20, drop_delay_ms: 20 }));
      expect(result.ok).toBe(true);
      expect(await page.getByRole("button", { name: "Destination" }).getAttribute("data-drop")).toBe("1");
    });
    it("drags through multiple screenshot waypoints and rejects odd paths", async () => {
      const page = await load('<button style="position:absolute;left:10px;top:10px" onmousedown="window.moves=[]">Source</button><script>document.onmousemove=e=>{if(e.buttons===1)moves.push([e.clientX,e.clientY])}</script>');
      const source = await ref("Source");
      const shot = JSON.parse(firstText(await runScreenshot(session, {})));
      const result = body(await runAct(session, { kind: "drag", ref: source, screenshot_id: shot.screenshot_id, path: [100, 100, 150, 60, 200, 120] }));
      expect(result.ok).toBe(true);
      const moves = await page.evaluate(() => (window as unknown as { moves: number[][] }).moves);
      expect(moves).toContainEqual([100, 100]);
      expect(moves).toContainEqual([150, 60]);
      expect(moves.at(-1)).toEqual([200, 120]);
      // Invalid input is rejected before any mouse down (source is a ref).
      expect(body(await runAct(session, { kind: "drag", ref: source, path: [1] })).code).toBe("invalid_input");
    });
    it("reports scroll position/at-end and masked fill differences", async () => {
      await load('<div role="region" aria-label="Scroller" style="height:100px;width:150px;overflow:auto"><div style="height:900px">content</div></div><label>Mask<input oninput="this.value=this.value.replace(/[^0-9]/g,\'\')"></label>');
      const scroller = await ref("Scroller");
      const scroll = body(await runAct(session, { kind: "scroll", ref: scroller, direction: "down", amount: 2000 }));
      expect(scroll.scroll.y).toBeGreaterThan(0);
      expect(scroll.scroll.at_end).toBe(true);
      const fill = body(await runAct(session, { kind: "fill", ref: await ref("Mask"), value: "12a3" }));
      expect(fill).toMatchObject({ actual_value: "123", value_matches: false });
      expect(fill.note).toContain("kind=type");
    });
  });

  it("expands every scoped child and keeps collapsed refs registered", async () => {
    const page = await session.ensurePage();
    await page.goto("about:blank");
    await page.setContent('<main aria-label="Repeated">' + '<button>Same</button>'.repeat(12) + '</main>');
    const initial = await runSnapshot(session, {});
    const tree = initial.content.filter(c => c.type === "text").map(c => c.text).join("\n");
    expect(tree).toContain("more similar items");
    const parent = /scope_ref=(e\d+)/.exec(tree)![1];
    // Hidden refs are already known, without needing another snapshot first.
    const raw = await page.locator("body").ariaSnapshot({ mode: "ai" });
    const hidden = [...raw.matchAll(/button "Same" \[ref=(e\d+)\]/g)].at(-1)![1];
    const locator = await session.resolveRef(hidden);
    expect(await locator.count()).toBe(1);
    const expanded = await runSnapshot(session, { scope_ref: parent });
    const expandedTree = expanded.content.filter(c => c.type === "text").map(c => c.text).join("\n");
    expect(expandedTree.match(/button "Same"/g)).toHaveLength(12);
    expect(expandedTree).not.toContain("more similar items");
    const capped = await runSnapshot(session, { filter: "interactive", max_chars: 300 });
    const cappedTree = capped.content.slice(1).filter(c => c.type === "text").map(c => c.text).join("\n");
    expect(JSON.parse(firstText(capped)).ref_count).toBe((cappedTree.match(/\[ref=/g) ?? []).length);
    expect(cappedTree).not.toContain("filter=interactive");
  });

  it("find locates past-budget controls by query and structured live locators, with explicit ambiguity", async () => {
    const page = await session.ensurePage();
    await page.goto("about:blank");
    await page.setContent(Array.from({ length: 1500 }, (_, i) => `<div><button>Product ${i}</button></div>`).join("") +
      '<button>Duplicate</button><button>Duplicate</button><label>Email label<input placeholder="Email placeholder" data-testid="email-id"></label>');
    const capped = await runSnapshot(session, { filter: "interactive" });
    expect(JSON.parse(firstText(capped)).truncated).toBe(true);
    expect(capped.content.map(c => c.type === "text" ? c.text : "").join("\n")).not.toContain('"Product 1499"');
    for (const input of [{ query: "Product 1499" }, { role: "button", name: "Product 1499" },
      { label: "Email label" }, { placeholder: "Email placeholder" }, { test_id: "email-id" }, { text: "Product 1499" }]) {
      const result = await runFind(session, input);
      expect(JSON.parse(firstText(result))).toMatchObject({ ok: true, match_count: 1, method: "locator", degraded: false });
      const listing = result.content[1] as { text: string };
      const ref = /^(e\d+) /m.exec(listing.text)![1];
      expect(await (await session.resolveRef(ref)).count()).toBe(1);
    }
    expect(JSON.parse(firstText(await runFind(session, { role: "button", name: "Duplicate" })))).toMatchObject({ ok: true, ambiguous: true, match_count: 2 });
    expect(JSON.parse(firstText(await runFind(session, { query: "Bottom button" })))).toMatchObject({ ok: true, match_count: 0 });
  });

  it("read_text and waits include frame and shadow text, with subtree scopes", async () => {
    const page = await session.ensurePage();
    await page.goto(new URL("prose-app", httpUrl).href);
    // A real cross-origin document, not merely a mocked consumer of frame text.
    await page.setContent(`<main aria-label="Inside"><p>Scoped prose</p><div id="host"></div>
      <iframe src="${new URL("inner-observation", httpUrl).href.replace("127.0.0.1", "localhost")}"></iframe></main><p>Outside prose</p>
      <iframe srcdoc="<p id='same'>Same frame text</p>"></iframe>`);
    await page.locator("#host").evaluate(host => { host.attachShadow({ mode: "open" }).innerHTML = '<p id="shadow">Open shadow prose</p><button>Shadow action</button>'; });
    expect(JSON.parse(firstText(await runWait(session, { condition: "text", text: "Cross frame text" })))).toMatchObject({ ok: true, matched: true });
    for (const target of [{ condition: "text", text: "Same frame text" }, { condition: "element", selector: "#cross" },
      { condition: "text", text: "Open shadow prose" }, { condition: "element", selector: "#shadow" }] as const) {
      expect(JSON.parse(firstText(await runWait(session, target)))).toMatchObject({ ok: true, matched: true });
    }
    const all = await runReadText(session);
    const allText = all.content.filter(c => c.type === "text").map(c => c.text).join("\n");
    for (const part of ["Cross frame text", "Same frame text", "Open shadow prose", "Outside prose"]) expect(allText).toContain(part);
    const snap = await runSnapshot(session, {});
    const scope = /main "Inside"[^\n]*\[ref=(e\d+)\]/.exec((snap.content[1] as { text: string }).text)![1];
    const scoped = await runReadText(session, { scope_ref: scope });
    const scopedText = scoped.content.filter(c => c.type === "text").map(c => c.text).join("\n");
    expect(scopedText).toContain("Cross frame text");
    expect(scopedText).toContain("Open shadow prose");
    expect(scopedText).not.toContain("Outside prose");
    expect(scopedText).not.toContain("Same frame text");
    expect(JSON.parse(firstText(await runWait(session, { condition: "text", text: "Same frame text", scope_ref: scope, timeout_ms: 20 })))).toMatchObject({ ok: false, timed_out: true });
    expect(JSON.parse(firstText(await runWait(session, { condition: "text", text: "Cross frame text", state: "hidden", timeout_ms: 20 })))).toMatchObject({ ok: false, timed_out: true });
    expect(JSON.parse(firstText(await runWait(session, { condition: "element", selector: "#cross", state: "detached", timeout_ms: 20 })))).toMatchObject({ ok: false, timed_out: true });
  });

  it.each([
    ["fixed portal", '<div style="overflow:hidden;height:0"><div style="position:fixed;top:10px;left:10px">FRAME</div></div>'],
    ["fixed iframe", '<div style="overflow:hidden;height:0">FRAME</div>', 'position:fixed;top:10px;left:10px'],
    ["absolute iframe", '<div style="position:relative"><div style="overflow:hidden;height:20px">FRAME</div></div>', 'position:absolute;top:30px'],
  ])("follow-up: %s escapes overflow outside its containing blocks", async (_name, layout, position) => {
    const page = await session.ensurePage(); await page.goto(httpUrl);
    const src = new URL("observation-text?text=PortalProse", httpUrl).href.replace("127.0.0.1", "localhost");
    await page.setContent(layout.replace("FRAME", `<iframe style="${position ?? ""}" src="${src}"></iframe>`));
    expect(await page.locator("iframe").isVisible()).toBe(true);
    const read = await runReadText(session);
    expect(JSON.parse(firstText(read))).toMatchObject({ ok: true, frames_hidden: 0, frames_skipped: 0 });
    expect((read.content[1] as { text: string }).text).toContain("PortalProse");
  });

  it.each([
    "transform:translateZ(0)", "filter:blur(0px)", "perspective:100px",
    "contain:layout", "contain:paint", "contain:strict", "contain:content",
    "will-change:transform", "will-change:filter", "will-change:perspective",
    "backdrop-filter:blur(1px)", "translate:0 0", "scale:1", "rotate:0deg",
    "transform-style:preserve-3d", "content-visibility:auto",
  ])("follow-up: %s makes fixed descendants subject to its overflow", async containingBlock => {
    const page = await session.ensurePage(); await page.goto(httpUrl);
    await page.setContent(`<p>Main</p><div style="${containingBlock};overflow:hidden;height:0"><iframe style="position:fixed;top:10px;left:10px" srcdoc="<p>ClippedFixedProse</p>"></iframe></div>`);
    // Confirm Chromium actually clips it before asserting the reader's result.
    expect(await page.locator("iframe").evaluate(el => {
      const box = el.getBoundingClientRect();
      return document.elementFromPoint(box.left + box.width / 2, box.top + box.height / 2) === el;
    })).toBe(false);
    const read = await runReadText(session);
    expect(JSON.parse(firstText(read))).toMatchObject({ ok: true, frames_hidden: 1, frames_skipped: 0 });
    expect((read.content[1] as { text: string }).text).not.toContain("ClippedFixedProse");
  });

  it("follow-up: scoped reader classifies each frame in one traversal", async () => {
    const page = await session.ensurePage(); await page.goto(httpUrl);
    await page.setContent('<main aria-label="Scoped article"><p>Main</p><iframe srcdoc="<p>ScopedFrameProse</p>"></iframe></main>');
    const snap = await runSnapshot(session, {});
    const ref = /main "Scoped article"[^\n]*\[ref=(e\d+)\]/.exec((snap.content[1] as { text: string }).text)![1];
    const child = page.frames().find(frame => frame !== page.mainFrame())!;
    const spy = vi.spyOn(child, "frameElement");
    try {
      const read = await runReadText(session, { scope_ref: ref });
      expect((read.content[1] as { text: string }).text).toContain("ScopedFrameProse");
      expect(spy).toHaveBeenCalledTimes(1);
    } finally { spy.mockRestore(); }
  });

  it.each([
    ["display none", "display:none", "", false],
    ["visibility hidden", "visibility:hidden", "", false],
    ["zero size", "width:0;height:0;border:0", "", false],
    ["hidden parent", "", "display:none", false],
    // Playwright visibility deliberately ignores opacity and viewport position.
    ["transparent", "opacity:0", "", true],
    ["off-document", "position:absolute;left:-10000px", "", true],
  ] as const)("follow-up: waits honor ancestor iframe visibility for %s", async (_name, style, parentStyle, visible) => {
    const page = await session.ensurePage(); await page.goto(httpUrl);
    const src = new URL("observation-text?text=WaitFrameProse", httpUrl).href.replace("127.0.0.1", "localhost");
    await page.setContent(`<p>Main</p><div style="${parentStyle}"><iframe style="${style}" src="${src}"></iframe></div>`);
    expect(await page.locator("iframe").isVisible()).toBe(visible);
    const child = page.frames().find(frame => frame !== page.mainFrame())!;
    await child.locator("p").evaluate(el => { el.id = "wait-frame-target"; });
    for (const target of [{ condition: "text", text: "WaitFrameProse" }, { condition: "element", selector: "#wait-frame-target" }] as const) {
      expect(JSON.parse(firstText(await runWait(session, { ...target, timeout_ms: 400 })))).toMatchObject({ matched: visible });
      expect(JSON.parse(firstText(await runWait(session, { ...target, state: "hidden", timeout_ms: 150 })))).toMatchObject({ matched: !visible });
      expect(JSON.parse(firstText(await runWait(session, { ...target, state: "attached", timeout_ms: 400 })))).toMatchObject({ matched: true });
    }
  });

  it("follow-up: nested frame waits require every ancestor iframe to be visible", async () => {
    const page = await session.ensurePage(); await page.goto(httpUrl);
    const src = new URL("observation-text?text=NestedFrameProse", httpUrl).href.replace("127.0.0.1", "localhost");
    await page.setContent(`<iframe style="visibility:hidden" srcdoc="<iframe src='${src}'></iframe>"></iframe>`);
    expect(JSON.parse(firstText(await runWait(session, { condition: "text", text: "NestedFrameProse", timeout_ms: 400 })))).toMatchObject({ matched: false, timed_out: true });
    expect(JSON.parse(firstText(await runWait(session, { condition: "text", text: "NestedFrameProse", state: "hidden", timeout_ms: 400 })))).toMatchObject({ matched: true });
    expect(JSON.parse(firstText(await runWait(session, { condition: "text", text: "NestedFrameProse", state: "attached", timeout_ms: 400 })))).toMatchObject({ matched: true });
  });

  it.each([
    ["below fold", '<div style="height:3000px"></div>FRAME'],
    ["transparent overlay", '<div style="position:relative;width:320px">FRAME<div style="position:absolute;inset:0;background:transparent"></div></div>'],
    ["click shield", '<div style="position:relative;width:320px">FRAME<div style="position:absolute;inset:0;opacity:0;pointer-events:auto"></div></div>'],
    ["pointer events none", '<div style="pointer-events:none">FRAME</div>'],
    ["inner scrollport", '<div style="overflow:auto;width:340px;height:100px"><div style="height:800px"></div>FRAME</div>'],
    ["sticky header", '<header style="position:sticky;top:0;height:200px;background:white;z-index:5">Header</header><div style="margin-top:-160px">FRAME</div>'],
  ])("geometry: %s frames remain readable and hidden waits cannot falsely pass", async (_name, layout) => {
    const page = await session.ensurePage(); await page.goto(httpUrl);
    const src = new URL("observation-text?text=ReachableFrame", httpUrl).href.replace("127.0.0.1", "localhost");
    await page.setContent('<p>Main content</p>' + layout.replace("FRAME", `<iframe style="display:block;width:300px;height:150px" src="${src}"></iframe>`));
    const child = page.frames().find(frame => frame !== page.mainFrame())!;
    expect(await child.getByText("ReachableFrame").filter({ visible: true }).count()).toBe(1);
    const read = await runReadText(session);
    expect(JSON.parse(firstText(read))).toMatchObject({ ok: true, frames_hidden: 0, frames_skipped: 0 });
    expect((read.content[1] as { text: string }).text).toContain("ReachableFrame");
    expect(JSON.parse(firstText(await runWait(session, { condition: "text", text: "ReachableFrame", timeout_ms: 1000 })))).toMatchObject({ matched: true });
    expect(JSON.parse(firstText(await runWait(session, { condition: "text", text: "ReachableFrame", state: "hidden", timeout_ms: 200 })))).toMatchObject({ matched: false, timed_out: true });
    await page.evaluate(() => scrollTo(0, document.documentElement.scrollHeight));
    expect((await runReadText(session)).content.map(c => c.type === "text" ? c.text : "").join("\n")).toContain("ReachableFrame");
  });

  it("geometry: busy counts in one tab cannot starve reads or waits in another", async () => {
    const pageA = await session.ensurePage(); await pageA.goto(httpUrl);
    const tabA = session.currentTabId()!;
    const src = new URL("inner-observation", httpUrl).href.replace("127.0.0.1", "localhost");
    await pageA.setContent('<p>Tab A</p>' + `<iframe src="${src}"></iframe>`.repeat(8));
    const children = pageA.frames().filter(frame => frame !== pageA.mainFrame());
    await Promise.all(children.map(frame => frame.evaluate(() => {
      addEventListener("message", () => { const until = Date.now() + 20000; while (Date.now() < until) { /* busy renderer */ } });
    })));
    let tabB: string | undefined;
    try {
      await pageA.evaluate(() => { document.querySelectorAll("iframe").forEach(frame => frame.contentWindow!.postMessage("busy", "*")); });
      await pageA.waitForTimeout(100);
      expect(JSON.parse(firstText(await runWait(session, { condition: "text", text: "Missing", state: "hidden", timeout_ms: 1100 })))).toMatchObject({ matched: false, timed_out: true });
      tabB = await session.createTab(); await session.selectTab(tabB);
      const pageB = await session.ensurePage(); await pageB.goto(httpUrl);
      await pageB.setContent(`<p>Tab B</p><iframe src="${new URL("observation-text?text=HealthyTabBFrame", httpUrl)}"></iframe>`);
      const read = await runReadText(session);
      expect(JSON.parse(firstText(read))).toMatchObject({ ok: true, frames_skipped: 0 });
      expect((read.content[1] as { text: string }).text).toContain("HealthyTabBFrame");
      expect(JSON.parse(firstText(await runWait(session, { condition: "text", text: "HealthyTabBFrame", timeout_ms: 1000 })))).toMatchObject({ matched: true });
      expect(JSON.parse(firstText(await runWait(session, { condition: "text", text: "Missing", state: "hidden", timeout_ms: 1000 })))).toMatchObject({ matched: true });
    } finally {
      if (tabB) await session.closeTab(tabA);
      else await pageA.goto("about:blank");
    }
  });

  it("geometry: a read waits within its own budget for a preceding main-frame count", async () => {
    const page = await session.ensurePage(); await page.goto(httpUrl); await page.setContent("<p>MainBusy</p>");
    await page.evaluate(() => { setTimeout(() => { const until = Date.now() + 2500; while (Date.now() < until) { /* busy renderer */ } }, 0); });
    try {
      expect(JSON.parse(firstText(await runWait(session, { condition: "text", text: "MainBusy", state: "hidden", timeout_ms: 400 })))).toMatchObject({ timed_out: true });
      const read = await runReadText(session);
      expect(JSON.parse(firstText(read))).toMatchObject({ ok: true });
      expect((read.content[1] as { text: string }).text).toContain("MainBusy");
    } finally { await page.evaluate(() => true); }
  });

  it("geometry: a new wait cannot reuse a preceding wait's late absence result", async () => {
    const page = await session.ensurePage(); await page.goto(httpUrl); await page.setContent("<p>Main</p>");
    await page.evaluate(() => { setTimeout(() => { const until = Date.now() + 1500; while (Date.now() < until) { /* busy renderer */ } }, 0); });
    expect(JSON.parse(firstText(await runWait(session, { condition: "text", text: "Spinner", state: "hidden", timeout_ms: 200 })))).toMatchObject({ timed_out: true });
    const add = page.evaluate(() => { const p = document.createElement("p"); p.textContent = "Spinner"; document.body.append(p); });
    const result = await runWait(session, { condition: "text", text: "Spinner", state: "hidden", timeout_ms: 2200 });
    await add;
    expect(await page.getByText("Spinner").count()).toBe(1);
    expect(JSON.parse(firstText(result))).toMatchObject({ matched: false, timed_out: true });
  });

  it.each([10000, 60000])("delta: large %i-node pages preserve main prose and never satisfy false negative waits", async count => {
    const page = await session.ensurePage(); await page.goto("about:blank");
    await page.evaluate(count => {
      document.body.innerHTML = '<p>Loading spinner</p>' + Array.from({ length: count }, (_, i) => `<div><span>Item ${i} lorem ipsum</span></div>`).join("");
    }, count);
    expect(JSON.parse(firstText(await runWait(session, { condition: "text", text: "Loading spinner", state: "hidden", timeout_ms: 1200 })))).toMatchObject({ matched: false, timed_out: true });
    expect(JSON.parse(firstText(await runWait(session, { condition: "text", text: "Loading spinner", timeout_ms: 4000 })))).toMatchObject({ ok: true, matched: true });
    const read = await runReadText(session);
    expect(JSON.parse(firstText(read))).toMatchObject({ ok: true, chars: 30000, truncated: true });
    expect((read.content[1] as { text: string }).text).toContain("Loading spinner");
  });

  it.each(["hidden", "detached", "visible"] as const)("delta: malformed selectors are invalid_input for %s waits", async state => {
    const page = await session.ensurePage(); await page.goto("about:blank"); await page.setContent("<p>Ready</p>");
    for (const selector of ["div[[[", "xpath=//["]) {
      expect(JSON.parse(firstText(await runWait(session, { condition: "element", selector, state, timeout_ms: 1000 })))).toMatchObject({ ok: false, code: "invalid_input" });
    }
  });

  it.each(["hidden", "detached"] as const)("delta: busy cross-origin frames are unknown for %s waits without queued evaluations", async state => {
    const page = await session.ensurePage(); await page.goto(httpUrl);
    await page.setContent(`<p>Main</p><iframe src="${new URL("inner-observation", httpUrl).href.replace("127.0.0.1", "localhost")}"></iframe>`);
    const frame = page.frames().find(frame => frame !== page.mainFrame())!;
    await frame.getByText("Cross frame text").waitFor();
    const spy = vi.spyOn(frame, "getByText");
    await frame.evaluate(() => { setTimeout(() => { const until = Date.now() + 2500; while (Date.now() < until) { /* busy renderer */ } }, 0); });
    try {
      expect(JSON.parse(firstText(await runWait(session, { condition: "text", text: "Cross frame text", state, timeout_ms: 1100 })))).toMatchObject({ matched: false, timed_out: true });
      expect(spy.mock.calls.length).toBeLessThanOrEqual(1);
    } finally { spy.mockRestore(); await frame.evaluate(() => true); }
    expect(JSON.parse(firstText(await runWait(session, { condition: "text", text: "Cross frame text", timeout_ms: 2000 })))).toMatchObject({ matched: true });
  });

  it("delta: main read failure is an explicit error, not an empty or blank-app result", async () => {
    const page = await session.ensurePage(); await page.goto("about:blank"); await page.setContent("<p>Main is present</p>");
    await page.evaluate(() => { setTimeout(() => { const until = Date.now() + 6000; while (Date.now() < until) { /* busy renderer */ } }, 0); });
    try {
      const body = JSON.parse(firstText(await runReadText(session)));
      expect(body).toMatchObject({ ok: false, code: "observation_failed" });
      expect(body.chars).toBeUndefined(); expect(body.warning).toBeUndefined();
    } finally { await page.evaluate(() => true); }
  });

  it.each([15, 30, 300])("delta: %i frames cannot starve main text and skipped children are reported", async count => {
    const page = await session.ensurePage(); await page.goto(httpUrl);
    await page.setContent('<p>Main survives frame fanout</p>' + Array.from({ length: count }, (_, i) =>
      `<iframe style="width:50px;height:50px;border:0" src="${new URL("observation-text?text=F" + i, httpUrl).href.replace("127.0.0.1", i % 2 ? "localhost" : "127.0.0.1")}"></iframe>`).join(""));
    const read = await runReadText(session);
    const body = JSON.parse(firstText(read));
    expect(body).toMatchObject({ ok: true });
    expect(body.chars).toBeGreaterThan(20);
    expect((read.content[1] as { text: string }).text).toContain("Main survives frame fanout");
    expect(body.frames_skipped).toBeGreaterThanOrEqual(Math.max(0, count - 48));
    expect(body.frames_skipped).toBeLessThanOrEqual(count);
    expect(JSON.parse(firstText(await runWait(session, { condition: "text", text: "Main survives frame fanout", timeout_ms: 2000 })))).toMatchObject({ matched: true });
    const last = page.frames().find(frame => frame.url().endsWith("text=F" + (count - 1)))!;
    await page.locator("iframe").last().scrollIntoViewIfNeeded();
    expect(await last.getByText("F" + (count - 1), { exact: true }).filter({ visible: true }).count()).toBe(1);
    // Main-text starvation is asserted above with the unchanged 2s budget.
    // Finding the LAST child instead traverses every preceding frame (six at a
    // time, across CDP). Allow the API's full bounded budget for 300 frames;
    // that traversal is a correctness check, not a shared-runner benchmark.
    expect(JSON.parse(firstText(await runWait(session, { condition: "text", text: "F" + (count - 1), match: "exact", timeout_ms: 15000 })))).toMatchObject({ matched: true });
    expect(JSON.parse(firstText(await runWait(session, { condition: "text", text: "Missing frame text", timeout_ms: 100 })))).toMatchObject({ timed_out: true });
  }, 30000);

  it.each([
    ['offscreen', 'style="position:absolute;left:-10000px;width:400px;height:300px"', ""],
    ['overflow wrapper', '', 'width:0;height:0;overflow:hidden'],
    ['clip path', 'style="clip-path:inset(100%)"', ""],
    ['legacy clip', 'style="position:absolute;clip:rect(0,0,0,0)"', ""],
    ['behind page', 'style="position:absolute;top:0;left:0;z-index:-1"', ""],
    ['content visibility', '', 'content-visibility:hidden'],
    ['padding consumes viewport', 'style="width:300px;height:150px;border:20px solid;box-sizing:border-box;padding:55px"', ""],
  ])("delta: %s injection is excluded from reads while waits use Playwright visibility", async (_name, attributes, wrapper) => {
    const page = await session.ensurePage(); await page.goto(httpUrl);
    const src = new URL("observation-text?text=HiddenInjection", httpUrl).href.replace("127.0.0.1", "localhost");
    await page.setContent(`<p>Main content</p><div style="${wrapper}"><iframe ${attributes} src="${src}"></iframe></div><iframe srcdoc="<p>Visible control frame</p>"></iframe>`);
    const read = await runReadText(session);
    const text = (read.content[1] as { text: string }).text;
    expect(text).toContain("Main content"); expect(text).toContain("Visible control frame");
    expect(text).not.toContain("HiddenInjection");
    expect(JSON.parse(firstText(read))).toMatchObject({ frames_hidden: 1, frames_skipped: 0 });
    const child = page.frames().find(frame => frame.url() === src)!;
    const visible = await page.locator(`iframe[src="${src}"]`).isVisible() &&
      await child.getByText("HiddenInjection").filter({ visible: true }).count() > 0;
    expect(JSON.parse(firstText(await runWait(session, { condition: "text", text: "HiddenInjection", timeout_ms: 400 })))).toMatchObject({ matched: visible });
    expect(JSON.parse(firstText(await runWait(session, { condition: "text", text: "HiddenInjection", state: "hidden", timeout_ms: 150 })))).toMatchObject({ matched: !visible });
  });

  it("review: churn, SVG and never-answering frames cannot break main-frame observations", async () => {
    const page = await session.ensurePage();
    await page.goto(new URL("prose-app", httpUrl).href);
    await page.setContent(`<p>Main frame remains readable</p><iframe src="${new URL("bodyless.svg", httpUrl)}"></iframe>
      <iframe src="${new URL("never-observation", httpUrl)}"></iframe>`, { waitUntil: "domcontentloaded" });
    await page.evaluate(() => {
      (window as unknown as { churn: number }).churn = window.setInterval(() => {
        const f = document.createElement("iframe"); f.srcdoc = "<p>Transient text</p>";
        document.body.appendChild(f); setTimeout(() => f.remove(), 10);
      }, 20);
    });
    try {
      const start = Date.now();
      for (let i = 0; i < 5; i++) {
        const read = await runReadText(session);
        expect((read.content[1] as { text: string }).text).toContain("Main frame remains readable");
        expect(JSON.parse(firstText(read)).ok).toBe(true);
      }
      expect(Date.now() - start).toBeLessThan(2500);
      expect(JSON.parse(firstText(await runWait(session, { condition: "text", text: "never appears", timeout_ms: 250 })))).toMatchObject({ ok: false, timed_out: true });
      expect(JSON.parse(firstText(await runWait(session, { condition: "text", text: "Main frame remains readable", timeout_ms: 250 })))).toMatchObject({ ok: true });
    } finally { await page.goto("about:blank"); }
  });

  it("review: hidden, transparent, tiny and nested-hidden frame text is omitted and visible origins are labelled", async () => {
    const page = await session.ensurePage();
    await page.goto(new URL("prose-app", httpUrl).href);
    const frameUrl = (text: string) => new URL("observation-text?text=" + text, httpUrl).href.replace("127.0.0.1", "localhost");
    await page.setContent(`<p>Main content</p><iframe style="display:none" src="${frameUrl("HiddenPayload")}"></iframe>
      <iframe style="width:1px;height:1px;opacity:0" src="${frameUrl("TinyPayload")}"></iframe>
      <iframe style="width:1px;height:1px" src="${frameUrl("SizedPayload")}"></iframe>
      <div style="opacity:0"><iframe src="${frameUrl("TransparentPayload")}"></iframe></div>
      <iframe src="${frameUrl("VisiblePayload")}"></iframe>`);
    const result = await runReadText(session);
    const text = (result.content[1] as { text: string }).text;
    expect(text).toContain("Main content");
    expect(text).toContain(`[frame ${new URL(frameUrl("VisiblePayload")).origin}]\nVisiblePayload`);
    for (const hidden of ["HiddenPayload", "TinyPayload", "SizedPayload", "TransparentPayload"]) expect(text).not.toContain(hidden);
  });

  it("review: ambiguous find lists usable candidate refs, continues batches and supports index narrowing", async () => {
    const page = await session.ensurePage(); await page.goto("about:blank");
    await page.setContent('<main><p>First row</p><button>Delete</button><p>Second row</p><button>Delete</button></main>');
    const found = await runFind(session, { role: "button", name: "Delete" });
    expect(JSON.parse(firstText(found))).toMatchObject({ ok: true, ambiguous: true, match_count: 2 });
    expect((found.content[1] as { text: string }).text).toContain("Ambiguous");
    expect((found.content[1] as { text: string }).text).toContain("Do not act until one is chosen");
    expect(JSON.parse(firstText(found)).hint).toContain("parent row or container (not the button itself)");
    const refs = [...(found.content[1] as { text: string }).text.matchAll(/^(e\d+) button/gm)].map(m => m[1]);
    expect(refs).toHaveLength(2);
    for (const ref of refs) expect(await (await session.resolveRef(ref)).count()).toBe(1);
    const narrowed = await runFind(session, { role: "button", name: "Delete", index: 1 });
    expect(JSON.parse(firstText(narrowed))).toMatchObject({ ok: true, match_count: 1 });
    expect((narrowed.content[1] as { text: string }).text).toContain(refs[1]);
    const batch = await runBatch(session, { actions: [JSON.stringify({ tool: "browser_find", input: { role: "button", name: "Delete" } }), JSON.stringify({ tool: "browser_read_text", input: {} })] }, name => TOOL_MAP.get(name));
    expect(JSON.parse(firstText(batch))).toMatchObject({ ok: true, count: 2, not_run: 0 });
  });

  it("review: selector waits include body/html/root and cheaply handle thousands of hidden matches", async () => {
    const page = await session.ensurePage(); await page.goto("about:blank");
    await page.setContent('<body class="loaded">' + '<span hidden class="many">hidden</span>'.repeat(3000) + '<span class="many">visible</span>');
    await page.evaluate(() => { document.documentElement.classList.add("dark"); document.documentElement.dataset.ready = "yes"; });
    for (const selector of ["body.loaded", "html[data-ready]", ":root.dark"]) {
      expect(JSON.parse(firstText(await runWait(session, { condition: "element", selector, timeout_ms: 250 })))).toMatchObject({ ok: true });
    }
    for (const state of ["visible", "attached"] as const) {
      const started = Date.now();
      expect(JSON.parse(firstText(await runWait(session, { condition: "element", selector: ".many", state, timeout_ms: 400 })))).toMatchObject({ ok: true });
      expect(Date.now() - started).toBeLessThan(500);
    }
    expect(JSON.parse(firstText(await runWait(session, { condition: "element", selector: "[hidden]", state: "attached", timeout_ms: 100 })))).toMatchObject({ ok: true });
  });

  it("review: semantic find gets capped interactive context and handles icon-only descriptive queries", async () => {
    const page = await session.ensurePage(); await page.goto("about:blank");
    await page.setContent('<button>☰</button><button>🛒</button><p>PrivateProseMarker</p>' + Array.from({ length: 1800 }, (_, i) => `<button>Long control description number ${i}</button>`).join(""));
    const originalFetch = globalThis.fetch;
    const requests: string[] = [];
    const mock = vi.spyOn(globalThis, "fetch").mockImplementation(async (url, init) => {
      if (String(url) !== "https://api.anthropic.com/v1/messages") return originalFetch(url, init);
      const body = JSON.parse(String(init?.body));
      const context: string = body.messages[0].content;
      requests.push(context);
      const icon = context.startsWith("Query: cart icon") ? "🛒" : "☰";
      const ref = new RegExp(`button "${icon}"[^\\n]*\\[ref=(e\\d+)\\]`).exec(context)![1];
      return new Response(JSON.stringify({ content: [{ type: "text", text: JSON.stringify([{ ref }]) }] }), { status: 200 });
    });
    vi.stubEnv("ANTHROPIC_API_KEY", "fixture-not-a-real-key");
    vi.stubEnv("ANTHROPIC_AUTH_TOKEN", "");
    vi.stubEnv("ANTHROPIC_BASE_URL", "https://api.anthropic.com");
    try {
      for (const query of ["hamburger menu", "cart icon", "the special button"]) {
        const result = await runFind(session, { query });
        expect(JSON.parse(firstText(result))).toMatchObject({ ok: true, degraded: false, match_count: 1 });
        const ref = /^(e\d+) /m.exec((result.content[1] as { text: string }).text)![1];
        expect(await (await session.resolveRef(ref)).count()).toBe(1);
      }
      expect(requests).toHaveLength(3);
      for (const message of requests) {
        const tree = message.split("Accessibility tree:\n")[1];
        expect(tree.length).toBeLessThanOrEqual(40000);
        expect(tree).not.toContain("PrivateProseMarker");
        expect(tree).toContain("truncated");
      }
    } finally { mock.mockRestore(); vi.unstubAllEnvs(); }
  });

  it("review: find containers return just themselves, unknown roles are invalid, and ref-like names do not inflate counts", async () => {
    const page = await session.ensurePage(); await page.goto("about:blank");
    await page.setContent('<div data-testid="container"><button>Child one</button><button>Child two</button></div><div>Container label<input aria-label="Nested input"></div><button aria-label="Fake [ref=e99999]">Real</button><iframe srcdoc="<button>Only frame control</button>"></iframe>');
    const found = await runFind(session, { test_id: "container" });
    expect(JSON.parse(firstText(found))).toMatchObject({ ok: true, match_count: 1 });
    expect((found.content[1] as { text: string }).text).not.toContain('button "Child');
    const textContainer = await runFind(session, { text: "Container label" });
    expect(JSON.parse(firstText(textContainer))).toMatchObject({ ok: true, match_count: 1 });
    expect((textContainer.content[1] as { text: string }).text).not.toContain("Nested input");
    // Documented exact lookup boundary; frame refs remain available to local search.
    expect(JSON.parse(firstText(await runFind(session, { role: "button", name: "Only frame control" })))).toMatchObject({ ok: true, match_count: 0 });
    expect(JSON.parse(firstText(await runFind(session, { query: "Only frame control" })))).toMatchObject({ ok: true, match_count: 1 });
    const invalid = await runFind(session, { role: "not-a-role" });
    expect(invalid.isError).toBe(true); expect(firstText(invalid)).toContain("invalid_input");
    const snap = await runSnapshot(session, {});
    const raw = await page.locator("body").ariaSnapshot({ mode: "ai" });
    const actual = [...raw.matchAll(/\[ref=(?:f\d+)?e\d+\](?: |:|$)/gm)];
    // The name's closing quote distinguishes its fake token from real attributes.
    expect(JSON.parse(firstText(snap)).ref_count).toBe(actual.length);
    expect(session.refs.isKnown("e99999")).toBe(false);
  });

  it("applies the workspace viewport before first navigation, new tabs and CDP recovery", async () => {
    const viewportManager = new BrowserManager({ endpoint: cdpEndpoint });
    const viewportSession = new Session(viewportManager);
    const ws = new BrowserWorkspace(viewportSession);
    const assertViewport = async (width: number, height: number, mode = "fixed") => {
      await ws.refresh();
      expect(ws.state().viewport).toEqual({ mode, width, height });
      const page = await viewportSession.ensurePage();
      expect(await page.evaluate(() => ({ width: window.innerWidth, height: window.innerHeight })))
        .toEqual({ width, height });
      return page;
    };
    try {
      // No browser_resize: the persistent context otherwise uses Chromium's
      // native window dimensions, not the workspace's 1280x800 default.
      expect(JSON.parse(firstText(await runNavigate(viewportSession, { url: fixtureUrl }))).ok).toBe(true);
      const first = await assertViewport(1280, 800);
      await viewportSession.setViewport(913, 617);
      viewportSession.viewportMode = "fit";
      await viewportSession.createTab();
      const second = await assertViewport(913, 617, "fit");
      expect(second.context()).toBe(first.context());
      await second.close();
      await first.close();
      await runNavigate(viewportSession, { url: fixtureUrl });
      const recreated = await assertViewport(913, 617, "fit");
      // Browser.close over CDP detaches this client, leaving Chromium alive.
      await recreated.context().browser()!.close();
      expect(viewportSession.hasPage()).toBe(false);
      expect(JSON.parse(firstText(await runNavigate(viewportSession, { url: fixtureUrl }))).ok).toBe(true);
      await assertViewport(913, 617, "fit");
    } finally {
      ws.stop();
      await viewportManager.cleanup();
    }
  }, 30000);

  it("waits for delayed HTTP hydration before returning navigation refs", async () => {
    const started = Date.now();
    const result = await runNavigate(session, { url: httpUrl + "delayed-hydration" });
    expect(JSON.parse(firstText(result)).ref_count).toBeGreaterThan(0);
    expect(result.content[1]).toMatchObject({ text: expect.stringContaining("Hydrated control") });
    expect(Date.now() - started).toBeGreaterThanOrEqual(300);
  });

  it.each(["blank", "canvas", "prose"])("bounds readiness and describes a %s page without inventing failure", async kind => {
    const started = Date.now();
    const body = JSON.parse(firstText(await runNavigate(session, { url: httpUrl + kind + "-app" })));
    expect(Date.now() - started).toBeLessThan(4500);
    expect(body.ref_count).toBe(0);
    if (kind !== "blank") {
      expect(body.timings.settle_ms).toBeLessThan(500);
      expect(body.timings.snapshot_ms).toBeGreaterThanOrEqual(0);
    }
    expect(body.warning).toContain("no actionable refs yet");
    expect(body.warning).not.toContain("likely a blank/errored app");
    if (kind === "blank") expect(body.warning).toContain("No body text or visible media was observed");
    else expect(body.warning).not.toContain("No body text or visible media was observed");
  });

  it("reports a real cross-origin auth redirect without treating it as a blank app", async () => {
    const body = JSON.parse(firstText(await runNavigate(session, { url: httpUrl + "auth-redirect" })));
    expect(body.redirected_to_origin).toBe(new URL(httpUrl.replace("127.0.0.1", "localhost")).origin);
    expect(body.ref_count).toBeGreaterThan(0);
    expect(body.warning).toBeUndefined();
  });

  it("sends consecutive targetless keys without a screenshot but still rejects stale coordinates", async () => {
    const page = await session.ensurePage();
    await page.setContent('<input id="keys">');
    await page.locator("#keys").focus();
    session.invalidateScreenshot();
    for (const key of ["a", "b"]) {
      const result = await session.runAgentExclusive(() => runAct(session, { kind: "key", key }));
      expect(JSON.parse(firstText(result)).ok).toBe(true);
    }
    expect(await page.locator("#keys").inputValue()).toBe("ab");
    const stale = await runAct(session, { kind: "key", key: "c", coordinate: [10, 10] });
    expect(stale.isError).toBe(true);
    expect(firstText(stale)).toContain("stale_screenshot");
    expect(await page.locator("#keys").inputValue()).toBe("ab");
  });

  it("navigate returns url/title and an interactive snapshot with refs", async () => {
    const res = await runNavigate(session, { url: fixtureUrl });
    const body = JSON.parse(firstText(res));
    expect(body.ok).toBe(true);
    expect(body.title).toBe("browser-mcp fixture");
    const snapText = res.content[1] && res.content[1].type === "text" ? res.content[1].text : "";
    expect(snapText).toContain('button "Submit"');
    expect(snapText).toMatch(/\[ref=e\d+\]/);
  });

  it("snapshot preserves all distinct list rows", async () => {
    const res = await runSnapshot(session, { filter: "all" });
    const snapText = res.content[1] && res.content[1].type === "text" ? res.content[1].text : "";
    expect(snapText).not.toContain("more similar items");
    for (let i = 1; i <= 20; i++) expect(snapText).toContain(`link "Item ${i}"`);
  });

  it("act(fill) then act(click) mutates the DOM and reflects the value", async () => {
    // Find the textbox and button refs from a fresh snapshot.
    const snap = await runSnapshot(session, { filter: "interactive" });
    const snapText = snap.content[1] && snap.content[1].type === "text" ? snap.content[1].text : "";
    const boxRef = /textbox[^\n]*\[ref=(e\d+)\]/.exec(snapText)?.[1];
    const btnRef = /button "Submit"[^\n]*\[ref=(e\d+)\]/.exec(snapText)?.[1];
    expect(boxRef).toBeTruthy();
    expect(btnRef).toBeTruthy();

    const fillRes = await runAct(session, { kind: "fill", ref: boxRef!, value: "Ada" });
    expect(JSON.parse(firstText(fillRes)).ok).toBe(true);

    const clickRes = await runAct(session, { kind: "click", ref: btnRef! });
    const clickBody = JSON.parse(firstText(clickRes));
    expect(clickBody.ok).toBe(true);
    expect(["minor", "major"]).toContain(clickBody.dom_mutated);
  });

  it("screenshot returns a JPEG image and pixel-dimension text", async () => {
    const res = await runScreenshot(session, {});
    const img = res.content.find((c) => c.type === "image");
    expect(img && img.type === "image" ? img.mimeType : "").toBe("image/jpeg");
    const meta = JSON.parse(firstText(res));
    expect(meta).toMatchObject({ screenshot_id: expect.stringMatching(/^s\d+$/), tab_id: session.currentTabId(), kind: "viewport", scale: 1 });
    expect(meta.width).toBeGreaterThan(0);
    expect(typeof meta.captured_at).toBe("string");
  });

  it("oversized viewport screenshot is downscaled to MAX_EDGE", async () => {
    const page = await session.ensurePage();
    await page.setViewportSize({ width: 1600, height: 1000 });
    try {
      const res = await runScreenshot(session, {});
      if (res.isError) throw new Error(firstText(res));
      const meta = JSON.parse(firstText(res));
      const [w, h] = [meta.width as number, meta.height as number];
      expect(Math.max(w, h)).toBeLessThanOrEqual(MAX_EDGE);
      // The stored mapping must let coordinate acts scale back to page pixels.
      const decoded = res.content.find((c) => c.type === "image");
      expect(decoded && decoded.type === "image" ? decoded.data.length : 0).toBeGreaterThan(0);
    } finally {
      await page.setViewportSize({ width: 1280, height: 720 });
    }
  });

  it("maps coordinates back through downscaling, device scale factor 2 and scroll", async () => {
    const page = await session.ensurePage();
    try {
      await session.setViewport(1600, 1000);
      await page.setContent('<style>body{margin:0;height:3000px}</style>' +
        '<button id="b" style="position:absolute;left:1180px;top:880px;width:40px;height:40px" onclick="this.dataset.hit=(+(this.dataset.hit||0))+1">x</button>' +
        '<button id="c" style="position:absolute;left:100px;top:700px;width:40px;height:40px" onclick="this.dataset.hit=1">y</button>');
      const shot = await runScreenshot(session, {});
      const meta = JSON.parse(firstText(shot));
      expect(meta).toMatchObject({ width: 1280, height: 800, scale: 0.8 });
      const res = await runAct(session, { kind: "click", coordinate: [1200 * 0.8, 900 * 0.8], screenshot_id: meta.screenshot_id });
      expect(JSON.parse(firstText(res)).ok).toBe(true);
      expect(await page.locator("#b").getAttribute("data-hit")).toBe("1");

      const cdp = await session.currentCDP();
      await cdp.send("Emulation.setDeviceMetricsOverride", { width: 800, height: 600, deviceScaleFactor: 2, mobile: false });
      expect(await page.evaluate(() => window.devicePixelRatio)).toBe(2);
      await page.evaluate(() => window.scrollTo(0, 500));
      const hiDpi = JSON.parse(firstText(await runScreenshot(session, {})));
      // Captured in CSS pixels: the image is not doubled, and the origin is the scroll offset.
      expect(hiDpi).toMatchObject({ width: 800, height: 600, scale: 1, origin: [0, 500] });
      const hit = await runAct(session, { kind: "click", coordinate: [120, 220], screenshot_id: hiDpi.screenshot_id });
      expect(JSON.parse(firstText(hit)).ok).toBe(true);
      expect(await page.locator("#c").getAttribute("data-hit")).toBe("1");
      await cdp.send("Emulation.clearDeviceMetricsOverride");
    } finally {
      await session.setViewport(1281, 720);
      await session.setViewport(1280, 720);
    }
  }, 30000);

  it("never lands a coordinate on a page that changed since the screenshot, even at the same scroll", async () => {
    const page = await session.ensurePage();
    await page.setContent('<canvas id="cv" width="300" height="100" style="position:absolute;left:0;top:200px"></canvas>' +
      '<button id="open" style="position:absolute;left:10px;top:10px;width:100px;height:40px" ' +
      'onclick="document.getElementById(\'m\').style.display=\'block\'">open</button>' +
      '<div id="m" style="display:none;position:fixed;inset:0;background:#fff">' +
      '<button id="del" style="position:absolute;left:10px;top:10px;width:100px;height:40px" onclick="this.dataset.hit=1">delete</button></div>');
    const shot = JSON.parse(firstText(await runScreenshot(session, {})));
    // A click that changes nothing keeps the screenshot usable (no extra round trip).
    expect(JSON.parse(firstText(await runAct(session, { kind: "click", coordinate: [50, 250] }))).dom_mutated).toBe("none");
    const again = JSON.parse(firstText(await runAct(session, { kind: "click", coordinate: [50, 250], screenshot_id: shot.screenshot_id })));
    expect(again).toMatchObject({ ok: true, screenshot_refreshed: true });
    // Opening the modal changes the DOM: the old screenshot no longer describes the page.
    const opened = JSON.parse(firstText(await runAct(session, { kind: "click", coordinate: [50, 30] })));
    expect(opened.dom_mutated).not.toBe("none");
    const blind = await runAct(session, { kind: "click", coordinate: [50, 30] });
    expect(blind.isError).toBe(true);
    expect(JSON.parse(firstText(blind))).toMatchObject({ code: "stale_screenshot", outcome: "not_executed" });
    expect(blind.content.some((c) => c.type === "image")).toBe(true);
    expect(await page.locator("#del").getAttribute("data-hit")).toBeNull();
  });

  it("rejects coordinates aimed from an older screenshot than the latest", async () => {
    const page = await session.ensurePage();
    await page.setContent('<button id="t" style="position:absolute;left:0;top:0;width:60px;height:30px" onclick="this.dataset.hit=1">t</button>');
    const first = JSON.parse(firstText(await runScreenshot(session, {})));
    await runScreenshot(session, { region: [0, 0, 200, 200] });
    const res = await runAct(session, { kind: "click", coordinate: [10, 10], screenshot_id: first.screenshot_id });
    expect(JSON.parse(firstText(res))).toMatchObject({ code: "stale_screenshot", outcome: "not_executed" });
    expect(await page.locator("#t").getAttribute("data-hit")).toBeNull();
  });

  it("registers and acts on refs inside an iframe", async () => {
    const page = await session.ensurePage();
    await page.setContent('<button>Top</button><iframe srcdoc="<button onclick=&quot;document.body.dataset.hit=1&quot;>Inner</button>"></iframe>');
    await page.frames()[1]?.waitForSelector("button");
    const snap = await runSnapshot(session, {});
    const ref = /button "Inner" \[ref=(f\d+e\d+)\]/.exec(snap.content[1].type === "text" ? snap.content[1].text : "")?.[1];
    expect(ref).toBeTruthy();
    const res = await runAct(session, { kind: "click", ref: ref! });
    expect(JSON.parse(firstText(res)).ok).toBe(true);
    expect(await page.frames()[1].evaluate(() => document.body.dataset.hit)).toBe("1");
  });

  it("sets checkbox state idempotently with check/uncheck", async () => {
    const page = await session.ensurePage();
    await page.setContent('<label><input type="checkbox" id="cb"> Agree</label>');
    const snap = await session.snapshot({ filter: "interactive" });
    const ref = /checkbox "Agree" \[ref=(e\d+)\]/.exec(snap.text)?.[1];
    for (const kind of ["check", "check", "uncheck"] as const) {
      expect(JSON.parse(firstText(await runAct(session, { kind, ref: ref! }))).ok).toBe(true);
    }
    expect(await page.locator("#cb").isChecked()).toBe(false);
  });

  it("pauses on a confirm dialog, gates other tools, and returns the paused action's result when answered", async () => {
    const page = await session.ensurePage();
    await page.setContent('<button id="c" onclick="document.title=String(confirm(\'Delete it?\'))">Delete</button>' +
      '<button id="p" onclick="document.title=String(prompt(\'Name?\', \'x\'))">Name</button>' +
      '<button id="a" onclick="alert(\'Saved\'); document.title=\'after-alert\'">Alert</button>');
    const snap = await session.snapshot({ filter: "interactive" });
    const refOf = (name: string) => new RegExp(`button "${name}" \\[ref=(e\\d+)\\]`).exec(snap.text)![1];
    const run = (name: string, input: Record<string, unknown>) => session.runAgentExclusive(() => TOOL_MAP.get(name)!.run(session, input));

    const paused = JSON.parse(firstText(await run("browser_act", { kind: "click", ref: refOf("Delete") })));
    expect(paused).toMatchObject({ ok: true, paused_by_dialog: true, dialog: { type: "confirm", message: "Delete it?" } });
    const gated = JSON.parse(firstText(await run("browser_snapshot", {})));
    expect(gated).toMatchObject({ code: "dialog_open", outcome: "not_executed" });
    expect(JSON.parse(firstText(await run("browser_status", {}))).pending_dialog).toMatchObject({ type: "confirm" });
    const answered = JSON.parse(firstText(await run("browser_act", { kind: "accept_dialog" })));
    expect(answered).toMatchObject({ dialog_handled: "accepted", ok: true, url_changed: false });
    expect(await page.title()).toBe("true");
    expect(session.pendingDialog).toBeUndefined();

    expect(JSON.parse(firstText(await run("browser_act", { kind: "click", ref: refOf("Name") }))).dialog).toMatchObject({ type: "prompt", default_value: "x" });
    expect(JSON.parse(firstText(await run("browser_act", { kind: "accept_dialog", value: "Ada" }))).dialog_handled).toBe("accepted");
    expect(await page.title()).toBe("Ada");

    const alerted = JSON.parse(firstText(await run("browser_act", { kind: "click", ref: refOf("Alert") })));
    expect(alerted.events).toEqual([{ type: "dialog", tab_id: session.currentTabId(), dialog_type: "alert", message: "Saved", handled: "accepted" }]);
    expect(await page.title()).toBe("after-alert");
  }, 30000);

  it("dismisses a pending dialog when a viewer takes control (the panel cannot show it)", async () => {
    const page = await session.ensurePage();
    await page.setContent('<button id="c" onclick="document.title=String(confirm(\'Leave?\'))">Ask</button>');
    const snap = await session.snapshot({ filter: "interactive" });
    const ref = /button "Ask" \[ref=(e\d+)\]/.exec(snap.text)![1];
    const ws = new BrowserWorkspace(session);
    try {
      const paused = JSON.parse(firstText(await session.runAgentExclusive(() => TOOL_MAP.get("browser_act")!.run(session, { kind: "click", ref }))));
      expect(paused.paused_by_dialog).toBe(true);
      const takeover = await ws.receive({ id: "dlg-1", runtime_id: ws.runtimeId, viewer_id: "v", tab_id: null, op: "takeover", args: {}, expires_at: Date.now() + 30000 });
      expect(takeover.ok).toBe(true);
      expect(session.pendingDialog).toBeUndefined();
      expect(await page.title()).toBe("false");
      expect((await ws.receive({ id: "dlg-2", runtime_id: ws.runtimeId, viewer_id: "v", tab_id: null, op: "release", args: {}, expires_at: Date.now() + 30000 })).ok).toBe(true);
    } finally {
      ws.stop();
      session.agentAdmission = undefined; session.onWorkspaceChange = undefined; session.workspaceStatus = undefined;
    }
  }, 30000);

  it("forgets the paused call when the dialog's tab closes, so a later answer never returns it", async () => {
    await session.ensurePage();
    const first = session.currentTabId()!;
    await session.createTab();
    const page = await session.ensurePage();
    const second = session.currentTabId()!;
    await page.setContent('<button onclick="confirm(\'Close me?\')">Ask</button>');
    const snap = await session.snapshot({ filter: "interactive" });
    const ref = /button "Ask" \[ref=(e\d+)\]/.exec(snap.text)![1];
    const run = (name: string, input: Record<string, unknown>) => session.runAgentExclusive(() => TOOL_MAP.get(name)!.run(session, input));
    expect(JSON.parse(firstText(await run("browser_act", { kind: "click", ref }))).paused_by_dialog).toBe(true);
    await session.closeTab(second);
    expect(session.pendingDialog).toBeUndefined();
    await session.selectTab(first);
    const other = await session.ensurePage();
    // A dialog nobody's call opened (a page timer): its answer has no paused call.
    await other.evaluate(() => { setTimeout(() => confirm("Timer?"), 0); });
    const deadline = Date.now() + 5000;
    while (!session.pendingDialog && Date.now() < deadline) await delay(20);
    expect(session.pendingDialog?.message).toBe("Timer?");
    const answered = JSON.parse(firstText(await run("browser_act", { kind: "dismiss_dialog" })));
    expect(Object.keys(answered).sort()).toEqual(["dialog_handled", "ok", "snapshot_stale"]);
  }, 30000);

  it("registers a tab the page opens and reports downloads, without switching the current tab", async () => {
    const page = await session.ensurePage();
    const before = session.currentTabId();
    await page.setContent('<a id="n" target="_blank" href="about:blank#popup">Open</a>' +
      '<a id="d" download="report.txt" href="data:text/plain,hello">Get report</a>');
    const snap = await session.snapshot({ filter: "interactive" });
    const refOf = (name: string) => new RegExp(`link "${name}" \\[ref=(e\\d+)\\]`).exec(snap.text)![1];
    const opened = JSON.parse(firstText(await runAct(session, { kind: "click", ref: refOf("Open") })));
    const newTab = (opened.events ?? []).find((e: { type: string }) => e.type === "new_tab");
    expect(newTab).toMatchObject({ type: "new_tab", tab_id: expect.stringMatching(/^tab\d+$/) });
    expect(session.currentTabId()).toBe(before);
    expect((await session.listTabs()).map((t) => t.id)).toContain(newTab.tab_id);
    await session.closeTab(newTab.tab_id);

    const got = JSON.parse(firstText(await runAct(session, { kind: "click", ref: refOf("Get report") })));
    expect(got.events).toEqual([expect.objectContaining({ type: "download", filename: "report.txt" })]);
    const deadline = Date.now() + 5000;
    while (session.downloads().at(-1)?.state === "in_progress" && Date.now() < deadline) await delay(50);
    expect(session.downloads().at(-1)).toMatchObject({ filename: "report.txt", state: "done", path: expect.any(String) });
  }, 30000);

  it("acting on a bogus ref yields a stale_ref error", async () => {
    const res = await runAct(session, { kind: "click", ref: "e99999" });
    expect(res.isError).toBe(true);
    expect(firstText(res)).toContain("stale_ref");
  });

  it("M1: acting on a detached ref fast-fails as stale_ref (no 30s hang)", async () => {
    await runNavigate(session, { url: fixtureUrl });
    const snap = await runSnapshot(session, { filter: "interactive" });
    const snapText = snap.content[1] && snap.content[1].type === "text" ? snap.content[1].text : "";
    const btnRef = /button "Submit"[^\n]*\[ref=(e\d+)\]/.exec(snapText)?.[1];
    expect(btnRef).toBeTruthy();

    // Detach the element WITHOUT navigating: the ref still resolves to a live
    // locator whose element is gone, so the action would block for Playwright's
    // 30s default before failing.
    const page = await session.ensurePage();
    await page.evaluate(() => document.getElementById("go")?.remove());

    const t0 = Date.now();
    const res = await runAct(session, { kind: "click", ref: btnRef! });
    const elapsed = Date.now() - t0;
    expect(res.isError).toBe(true);
    expect(firstText(res)).toContain("stale_ref");
    // Must fail via the short ref timeout (~2s), nowhere near the 30s default.
    expect(elapsed).toBeLessThan(10000);
  });

  it("M4: a failed action tears down the mutation watcher", async () => {
    await runNavigate(session, { url: fixtureUrl });
    const res = await runAct(session, { kind: "click", ref: "e99999" });
    expect(res.isError).toBe(true);
    const page = await session.ensurePage();
    const cleaned = await page.evaluate(
      (key) => (window as unknown as Record<string, unknown>)[key] === undefined,
      WATCH_KEY,
    );
    expect(cleaned).toBe(true);
  });

  it("M2: region screenshot of a scrolled section -> coordinate click hits the right element", async () => {
    await runNavigate(session, { url: fixtureUrl });
    const page = await session.ensurePage();

    // Scroll the deep target into view and read its document-space rect.
    const info = await page.evaluate(() => {
      const el = document.getElementById("deep") as HTMLElement;
      el.scrollIntoView({ block: "center" });
      const r = el.getBoundingClientRect();
      return {
        docX: r.left + window.scrollX,
        docY: r.top + window.scrollY,
        w: r.width,
        h: r.height,
        scrollY: window.scrollY,
      };
    });
    // We really did scroll down (the round trip must survive a non-zero scroll).
    expect(info.scrollY).toBeGreaterThan(0);

    const pad = 20;
    const shot = await runScreenshot(session, {
      region: [info.docX - pad, info.docY - pad, info.w + pad * 2, info.h + pad * 2],
    });
    expect(shot.content.find((c) => c.type === "image")).toBeTruthy();

    // Button centre in the region-image pixel space (scale 1, origin = crop).
    const clickCoord = [pad + info.w / 2, pad + info.h / 2];
    const res = await runAct(session, { kind: "click", coordinate: clickCoord });
    expect(JSON.parse(firstText(res)).ok).toBe(true);

    const out = await page.evaluate(
      () => document.getElementById("deepout")?.textContent ?? "",
    );
    expect(out).toBe("deep-clicked");
  });

  it("M3: two overlapping calls through the session queue both succeed", async () => {
    await runNavigate(session, { url: fixtureUrl });
    const [a, b] = await Promise.all([
      session.runExclusive(() => runSnapshot(session, { filter: "interactive" })),
      session.runExclusive(() => runSnapshot(session, { filter: "interactive" })),
    ]);
    expect(JSON.parse(firstText(a)).ok).toBe(true);
    expect(JSON.parse(firstText(b)).ok).toBe(true);
  });

  it("find (degraded path) locates the Submit button and its ref is clickable", async () => {
    // No auth env → the semantic sub-call is skipped and the deterministic text
    // scorer runs. No model is called in CI.
    delete process.env.ANTHROPIC_AUTH_TOKEN;
    delete process.env.ANTHROPIC_API_KEY;
    delete process.env.ANTHROPIC_BASE_URL;

    await runNavigate(session, { url: fixtureUrl });
    const res = await runFind(session, { query: "the submit button" });
    const body = JSON.parse(firstText(res));
    expect(body.ok).toBe(true);
    expect(body.degraded).toBe(true);
    expect(body.match_count).toBeGreaterThanOrEqual(1);

    const listing = res.content[1] && res.content[1].type === "text" ? res.content[1].text : "";
    expect(listing).toContain("semantic search unavailable");
    // The Submit button appears in the terse listing; pull its ref back out.
    const submitRef = /(e\d+) button "Submit"/.exec(listing)?.[1];
    expect(submitRef).toBeTruthy();

    // A ref returned by find is act-able directly.
    const click = await runAct(session, { kind: "click", ref: submitRef! });
    expect(JSON.parse(firstText(click)).ok).toBe(true);
    const page = await session.ensurePage();
    const out = await page.evaluate(() => document.getElementById("out")?.textContent ?? "");
    expect(out).toContain("clicked");
  });

  // ---- tier 2 ---------------------------------------------------------------

  it("read_text returns title/url and the page prose", async () => {
    await runNavigate(session, { url: fixtureUrl });
    const res = await runReadText(session);
    const body = JSON.parse(firstText(res));
    expect(body.ok).toBe(true);
    expect(body.title).toBe("browser-mcp fixture");
    const textBlock = res.content[1] && res.content[1].type === "text" ? res.content[1].text : "";
    expect(textBlock).toContain("intro paragraph");
  });

  it("eval returns the last expression value and describes errors", async () => {
    await runNavigate(session, { url: fixtureUrl });

    const sum = JSON.parse(firstText(await runEval(session, { code: "1 + 2" })));
    expect(sum.ok).toBe(true);
    expect(sum.value).toBe("3");
    expect(sum.type).toBe("number");

    // Multi-statement body with top-level await and an explicit return.
    const title = JSON.parse(
      firstText(await runEval(session, { code: "const t = document.title; return t;" })),
    );
    expect(title.value).toBe('"browser-mcp fixture"');

    // A DOM node is described, not serialized as {}.
    const node = JSON.parse(firstText(await runEval(session, { code: "document.body" })));
    expect(node.value).toMatch(/\[BODY/);

    const err = await runEval(session, { code: "throw new Error('kaboom')" });
    expect(err.isError).toBe(true);
    expect(firstText(err)).toContain("kaboom");
  });

  it("a failed fetch in eval reports the response's status and content-type", async () => {
    await runNavigate(session, { url: httpUrl });
    // The classic auth-wall shape: an HTML page parsed as JSON. The thrown
    // message says nothing about HTTP, so the recorded response has to.
    const failed = await runEval(session, {
      code: "const r = await fetch('/'); return await r.json();",
    });
    expect(failed.isError).toBe(true);
    const body = JSON.parse(firstText(failed));
    expect(body.ok).toBe(false);
    expect(Array.isArray(body.recent_requests)).toBe(true);
    const html = body.recent_requests.find((r: { url: string }) => !r.url.includes("/api/"));
    expect(html.status).toBe(200);
    expect(html.content_type).toContain("text/html");
    expect(typeof html.id).toBe("string");
    // Leave the shared page's buffer as we found it: the two-phase network test
    // reads the oldest recorded /api/data response.
    session.clearNetwork();
  });

  it("eval bounds a pathological wide+deep object via the work budget", async () => {
    await runNavigate(session, { url: fixtureUrl });

    // A cheap-to-BUILD but explosive-to-DESCRIBE object: each level is an array
    // of 100 references to the SAME lower level (a DAG, ~600 real allocations).
    // The describer clears its cycle guard on exit, so it re-walks the shared
    // subtree down every path — on the order of 100^depth nodes. Without the
    // node/char budget this would pin the page for a very long time; with it the
    // walk must bail quickly, return truncated, and stay within the size cap.
    const started = Date.now();
    const res = JSON.parse(
      firstText(
        await runEval(session, {
          code: "let n = 1; for (let i = 0; i < 6; i++) { const a = []; for (let j = 0; j < 100; j++) a.push(n); n = a; } return n;",
        }),
      ),
    );
    const elapsed = Date.now() - started;
    expect(res.ok).toBe(true);
    expect(res.truncated).toBe(true);
    expect(res.value.length).toBeLessThanOrEqual(10000);
    // Bounded work ⇒ fast; without the budget this walk would not complete.
    expect(elapsed).toBeLessThan(4000);
  });

  it("console filters buffered messages by pattern and only_errors", async () => {
    await runNavigate(session, { url: fixtureUrl });

    const errors = JSON.parse(
      firstText(await runConsole(session, { pattern: "fixture-console", only_errors: true })),
    );
    expect(errors.ok).toBe(true);
    expect(errors.entries.length).toBeGreaterThanOrEqual(1);
    expect(errors.entries.every((e: { level: string }) => e.level === "error")).toBe(true);
    expect(errors.entries.some((e: { text: string }) => e.text.includes("boom"))).toBe(true);

    // A pattern that matches nothing yields zero entries.
    const none = JSON.parse(
      firstText(await runConsole(session, { pattern: "no-such-console-line-xyz" })),
    );
    expect(none.matched).toBe(0);

    // clear empties the buffer.
    await runConsole(session, { pattern: "fixture-console", clear: true });
    const afterClear = JSON.parse(firstText(await runConsole(session, { pattern: "fixture-console" })));
    expect(afterClear.matched).toBe(0);
  });

  it("network lists requests then returns a response body (two-phase)", async () => {
    await runNavigate(session, { url: httpUrl });

    // The page fetches /api/data after load; poll until it is recorded.
    let apiId: string | undefined;
    for (let i = 0; i < 20 && !apiId; i++) {
      const listed = JSON.parse(
        firstText(await runNetwork(session, { url_pattern: "/api/data" })),
      );
      apiId = listed.requests[0]?.id;
      if (!apiId) await delay(100);
    }
    expect(apiId).toBeTruthy();

    const bodyRes = await runNetwork(session, { request_id: apiId! });
    const meta = JSON.parse(firstText(bodyRes));
    expect(meta.ok).toBe(true);
    const bodyText = bodyRes.content[1] && bodyRes.content[1].type === "text" ? bodyRes.content[1].text : "";
    expect(bodyText).toContain(NET_BODY_MARKER);

    // Unknown id is a clean error.
    const missing = await runNetwork(session, { request_id: "req999999" });
    expect(missing.isError).toBe(true);
  });

  it("network: the body of a never-ending event stream is a bounded error, not a hung session", async () => {
    await runNavigate(session, { url: `${httpUrl}stream-page` });
    let streamId: string | undefined;
    for (let i = 0; i < 30 && !streamId; i++) {
      const listed = JSON.parse(firstText(await runNetwork(session, { url_pattern: "/stream$" })));
      streamId = listed.requests[0]?.id;
      if (!streamId) await delay(100);
    }
    expect(streamId).toBeTruthy();
    const t0 = Date.now();
    const res = await session.runExclusive(() => runNetwork(session, { request_id: streamId! }));
    expect(Date.now() - t0).toBeLessThan(NETWORK_BODY_TIMEOUT_MS + 3000);
    expect(res.isError).toBe(true);
    expect(firstText(res)).toContain("still streaming");
    // The session queue is free again.
    const after = await session.runExclusive(() => runEval(session, { code: "1 + 1" }));
    expect(JSON.parse(firstText(after)).value).toBe("2");
  });

  it("act(type) on a ref types a long value in full instead of timing out as stale_ref", async () => {
    await runNavigate(session, { url: fixtureUrl });
    const snap = await runSnapshot(session, { filter: "interactive" });
    const snapText = snap.content[1] && snap.content[1].type === "text" ? snap.content[1].text : "";
    const boxRef = /textbox[^\n]*\[ref=(e\d+)\]/.exec(snapText)?.[1];
    expect(boxRef).toBeTruthy();
    const value = "x".repeat(3000);
    const res = await runAct(session, { kind: "type", ref: boxRef!, value });
    expect(firstText(res)).not.toContain("stale_ref");
    expect(JSON.parse(firstText(res)).ok).toBe(true);
    const page = await session.ensurePage();
    expect((await page.inputValue("#name")).length).toBe(3000);
  }, 60_000);

  it("tabs: create/list/select/close lifecycle, cannot close the last tab", async () => {
    await runNavigate(session, { url: fixtureUrl });
    const firstTab = session.currentTabId()!;

    const created = JSON.parse(firstText(await runTabs(session, { op: "create" })));
    const newTab = created.tab_id as string;
    expect(newTab).not.toBe(firstTab);
    expect(session.currentTabId()).toBe(newTab);

    const listed = JSON.parse(firstText(await runTabs(session, { op: "list" })));
    expect(listed.tabs.length).toBeGreaterThanOrEqual(2);
    expect(listed.tabs.find((t: { id: string; current: boolean }) => t.current)?.id).toBe(newTab);

    const selected = JSON.parse(firstText(await runTabs(session, { op: "select", tab_id: firstTab })));
    expect(selected.current).toBe(firstTab);

    const closed = JSON.parse(firstText(await runTabs(session, { op: "close", tab_id: newTab })));
    expect(closed.tabs.some((t: { id: string }) => t.id === newTab)).toBe(false);

    // Down to one tab: closing it is rejected.
    const last = session.currentTabId()!;
    const rejected = await runTabs(session, { op: "close", tab_id: last });
    expect(rejected.isError).toBe(true);
    expect(firstText(rejected)).toContain("cannot close the last tab");
  });

  it("resize applies presets and explicit dimensions", async () => {
    await runNavigate(session, { url: fixtureUrl });

    const preset = JSON.parse(firstText(await runResize(session, { preset: "mobile" })));
    expect([preset.width, preset.height]).toEqual([375, 812]);
    const page = await session.ensurePage();
    const inner = await page.evaluate(() => ({ w: window.innerWidth, h: window.innerHeight }));
    expect(inner.w).toBe(375);

    const explicit = JSON.parse(firstText(await runResize(session, { width: 900, height: 650 })));
    expect([explicit.width, explicit.height]).toEqual([900, 650]);
  });

  it("resize reports the page's own viewport, not the request", async () => {
    await runNavigate(session, { url: fixtureUrl });
    const page = await session.ensurePage();
    const body = JSON.parse(firstText(await runResize(session, { width: 412, height: 700 })));

    const inner = await page.evaluate(() => ({
      w: window.innerWidth, h: window.innerHeight, dpr: window.devicePixelRatio,
    }));
    expect(body.requested).toEqual({ width: 412, height: 700 });
    expect([body.width, body.height]).toEqual([inner.w, inner.h]);
    expect(body.device_scale_factor).toBe(inner.dpr);

    // A viewer resize behind the agent's back is reported, not papered over.
    await page.setViewportSize({ width: 369, height: 714 });
    const stale = await session.appliedViewport();
    expect([stale.width, stale.height]).toEqual([369, 714]);

    // …and the agent's pinned size is restored on its next admitted call.
    await session.runAgentExclusive(async () => undefined);
    const restored = await page.evaluate(() => window.innerWidth);
    expect(restored).toBe(412);
  });

  it("keeps the browser's screen size while the viewport is pinned (#1791)", async ({ skip }) => {
    if (process.platform !== "linux") {
      skip("#2292: --ozone-override-screen-size is only honoured on Linux/Ozone");
    }
    const screenOf = async () => (await session.ensurePage()).evaluate(() => ({
      screen: [window.screen.width, window.screen.height], inner: [window.innerWidth, window.innerHeight],
    }));
    await runNavigate(session, { url: fixtureUrl });
    await runResize(session, { width: 1280, height: 800 });
    expect(await screenOf()).toEqual({ screen: [1920, 1080], inner: [1280, 800] });

    await runResize(session, { width: 412, height: 700 });
    expect(await screenOf()).toEqual({ screen: [1920, 1080], inner: [412, 700] });

    await runTabs(session, { op: "create" });
    await runNavigate(session, { url: fixtureUrl });
    expect(await screenOf()).toEqual({ screen: [1920, 1080], inner: [412, 700] });

    // A viewer resize behind the agent's back drops the override; the pin restores it.
    const page = await session.ensurePage();
    await page.setViewportSize({ width: 369, height: 714 });
    await session.runAgentExclusive(async () => undefined);
    expect(await screenOf()).toEqual({ screen: [1920, 1080], inner: [412, 700] });
  });

  it("screenshot brings a backgrounded tab forward before capturing", async () => {
    await runNavigate(session, { url: fixtureUrl });
    const first = session.currentTabId()!;
    const opened = JSON.parse(firstText(await runTabs(session, { op: "create" })));
    await runNavigate(session, { url: fixtureUrl });
    // Back to the first tab: Chrome still has the newest one in the foreground.
    await runTabs(session, { op: "select", tab_id: first });
    try {
      const shot = await runScreenshot(session, {});
      expect(shot.isError).toBeUndefined();
      expect(shot.content.find((c) => c.type === "image")).toBeTruthy();
      const page = await session.ensurePage();
      expect(await page.evaluate(() => document.visibilityState)).toBe("visible");
    } finally {
      await runTabs(session, { op: "close", tab_id: opened.tab_id });
    }
  });

  it("upload: env-gate, not-a-file-input guard, and allowlisted happy path", async () => {
    // Non-file-input ref (a textbox) is rejected clearly once the dir is set.
    await runNavigate(session, { url: fixtureUrl });
    const basicSnap = await runSnapshot(session, { filter: "interactive" });
    const basicText =
      basicSnap.content[1] && basicSnap.content[1].type === "text" ? basicSnap.content[1].text : "";
    const boxRef = /textbox[^\n]*\[ref=(e\d+)\]/.exec(basicText)?.[1];
    expect(boxRef).toBeTruthy();

    // BROWSER_UPLOAD_DIR unset → rejected before touching the page.
    delete process.env.BROWSER_UPLOAD_DIR;
    const denied = await runUpload(session, { ref: boxRef!, paths: [uploadFilePath] });
    expect(denied.isError).toBe(true);
    expect(firstText(denied)).toContain("BROWSER_UPLOAD_DIR");

    process.env.BROWSER_UPLOAD_DIR = uploadDir;
    const notFile = await runUpload(session, { ref: boxRef!, paths: [uploadFilePath] });
    expect(notFile.isError).toBe(true);
    expect(firstText(notFile)).toContain("not a file input");

    // Real file input: an in-dir file uploads and lands on the element.
    await runNavigate(session, { url: uploadFixtureUrl });
    const upSnap = await runSnapshot(session, { filter: "all" });
    const upText = upSnap.content[1] && upSnap.content[1].type === "text" ? upSnap.content[1].text : "";
    // The file input renders as a button carrying the label's accessible name.
    const fileRef = /button "Attachment"[^\n]*\[ref=(e\d+)\]/.exec(upText)?.[1];
    expect(fileRef).toBeTruthy();

    const ok = await runUpload(session, { ref: fileRef!, paths: [uploadFilePath] });
    expect(JSON.parse(firstText(ok)).ok).toBe(true);
    const page = await session.ensurePage();
    const name = await page.evaluate(() => {
      const el = document.getElementById("file") as HTMLInputElement | null;
      return el && el.files && el.files[0] ? el.files[0].name : "";
    });
    expect(name).toBe("upload-sample.txt");

    // Out-of-dir path is rejected.
    const escape = await runUpload(session, { ref: fileRef!, paths: ["/etc/hostname"] });
    expect(escape.isError).toBe(true);
    delete process.env.BROWSER_UPLOAD_DIR;
  });

  it("upload: accepts a 27 MiB file and preserves its bytes in the browser", async () => {
    const previousDir = process.env.BROWSER_UPLOAD_DIR;
    const path = join(uploadDir, "large-bundle.aab");
    const size = 27 * 1024 * 1024;
    writeFileSync(path, Buffer.alloc(size, 0x61));
    process.env.BROWSER_UPLOAD_DIR = uploadDir;
    try {
      await runNavigate(session, { url: uploadFixtureUrl });
      const snapshot = await runSnapshot(session, { filter: "all" });
      const text = snapshot.content
        .filter((item) => item.type === "text")
        .map((item) => item.text)
        .join("\n");
      const ref = /button "Attachment"[^\n]*\[ref=(e\d+)\]/.exec(text)?.[1];
      expect(ref).toBeTruthy();
      const result = await runUpload(session, { ref: ref!, paths: [path] });
      expect(result.isError, firstText(result)).not.toBe(true);
      expect(JSON.parse(firstText(result))).toEqual({ ok: true, ref, count: 1 });
      const page = await session.ensurePage();
      const received = await page.evaluate(async () => {
        const file = (document.getElementById("file") as HTMLInputElement).files![0];
        const bytes = new Uint8Array(await file.arrayBuffer());
        return { name: file.name, size: file.size, intact: bytes.every((byte) => byte === 0x61) };
      });
      expect(received).toEqual({ name: "large-bundle.aab", size, intact: true });
    } finally {
      if (previousDir === undefined) delete process.env.BROWSER_UPLOAD_DIR;
      else process.env.BROWSER_UPLOAD_DIR = previousDir;
      rmSync(path, { force: true });
    }
    // Outlive the scaled setInputFiles timeout (84 s for 27 MiB) so a slow host fails with its message.
  }, 120000);

  it("batch runs sequentially and stops on the first error", async () => {
    await runNavigate(session, { url: fixtureUrl });
    const lookup = (name: string) => {
      const def = TOOL_MAP.get(name);
      return def ? { inputSchema: def.inputSchema, run: def.run } : undefined;
    };

    const happy = JSON.parse(
      firstText(
        await runBatch(
          session,
          {
            actions: [
              '{"tool":"browser_snapshot","input":{"filter":"interactive"}}',
              '{"tool":"browser_read_text","input":{}}',
              '{"tool":"browser_eval","input":{"code":"2*21"}}',
            ],
          },
          lookup,
        ),
      ),
    );
    expect(happy.ok).toBe(true);
    expect(happy.count).toBe(3);
    expect(happy.results.every((r: { ok: boolean }) => r.ok)).toBe(true);

    const stopEarly = JSON.parse(
      firstText(
        await runBatch(
          session,
          {
            actions: [
              '{"tool":"browser_eval","input":{"code":"1"}}',
              '{"tool":"browser_act","input":{"kind":"click","ref":"e999999"}}',
              '{"tool":"browser_eval","input":{"code":"2"}}',
            ],
          },
          lookup,
        ),
      ),
    );
    expect(stopEarly.ok).toBe(false);
    expect(stopEarly.count).toBe(2); // third action never ran
    expect(stopEarly.results[1].ok).toBe(false);
  });

  // ---- SSRF egress guard ----------------------------------------------------

  it("A6: guard aborts a loopback navigation and surfaces the friendly reason", async () => {
    // A fresh manager whose policy has NO loopback allow-override: the DEFAULT
    // guard blocks 127.0.0.1, proving request interception actually aborts and
    // navigate translates net::ERR_BLOCKED_BY_CLIENT into a clear reason.
    const guarded = new BrowserManager({
      endpoint: cdpEndpoint,
      ssrfPolicy: compilePolicy({} as NodeJS.ProcessEnv),
    });
    const guardedSession = new Session(guarded);
    try {
      // navigate rethrows on a blocked request exactly like a net error; the MCP
      // dispatcher (index.ts) turns the throw into an errorResult. The message
      // must lead with what to do and still carry the policy reason.
      const failure = await runNavigate(guardedSession, { url: httpUrl }).then(
        () => undefined,
        (err: Error) => err,
      );
      expect(failure?.message ?? "").toMatch(/^Bind the server to 0\.0\.0\.0/);
      expect(failure?.message ?? "").toMatch(/Policy detail:.*loopback/);
    } finally {
      // Close only the page we opened; leave the shared browser to the suite.
      await guarded.cleanup().catch(() => {});
    }
  });
  it("SSRF: a redirect hop to a blocked host is refused (navigation and fetch), and preflights reach the server", async () => {
    // 127.0.0.2 stands in for an internal service: this manager allows only
    // 127.0.0.1, so the redirect target is blocked while the origin is not.
    const hits: string[] = [];
    const internal = createHttpServer((req, res) => {
      hits.push(`${req.method} ${req.url}`);
      res.writeHead(200, { "content-type": "text/html" });
      res.end("<title>INTERNAL SECRET</title>");
    });
    await new Promise<void>((resolve) => internal.listen(0, "127.0.0.2", resolve));
    const internalPort = (internal.address() as { port: number }).port;
    const origin = createHttpServer((req, res) => {
      if (req.url === "/hop") {
        res.writeHead(302, { location: `http://127.0.0.2:${internalPort}/meta` });
        res.end();
        return;
      }
      hits.push(`origin ${req.method} ${req.url}`);
      if (req.method === "OPTIONS") {
        res.writeHead(204);
        res.end();
        return;
      }
      res.writeHead(200, { "content-type": "text/html" });
      res.end("<title>origin</title><p>origin page</p>");
    });
    await new Promise<void>((resolve) => origin.listen(0, "127.0.0.1", resolve));
    const originUrl = `http://127.0.0.1:${(origin.address() as { port: number }).port}`;
    const guarded = new BrowserManager({
      endpoint: cdpEndpoint,
      ssrfPolicy: compilePolicy({ BROWSER_SSRF_ALLOW: "127.0.0.1/32" } as NodeJS.ProcessEnv),
    });
    const guardedSession = new Session(guarded);
    try {
      const failure = await runNavigate(guardedSession, { url: `${originUrl}/hop` }).then(
        () => undefined,
        (err: Error) => err,
      );
      expect(failure?.message ?? "").toMatch(/127\.0\.0\.2/);
      const page = await guardedSession.ensurePage();
      // The blocked navigation commits Chromium's error page asynchronously;
      // a goto racing it is "interrupted by another navigation".
      for (let attempt = 0; ; attempt++) {
        try {
          await page.goto(`${originUrl}/`);
          break;
        } catch (err) {
          if (attempt >= 3) throw err;
          await delay(200);
        }
      }
      const fetched = await page.evaluate(
        (url) => fetch(url).then((r) => `status ${r.status}`, (e) => `error ${e}`),
        `${originUrl}/hop`,
      );
      expect(fetched).toMatch(/^error/);
      expect(hits.filter((h) => !h.startsWith("origin"))).toEqual([]);

      // A non-simple cross-origin request must be preceded by a real preflight
      // at the server (Playwright's context.route used to fake a permissive one).
      const crossOrigin = originUrl.replace("127.0.0.1", "localhost");
      await page.evaluate(
        (url) => fetch(url, { method: "PUT", headers: { "content-type": "application/json" }, body: "{}" }).catch(() => null),
        `${crossOrigin}/api/put`,
      );
      expect(hits).toContain("origin OPTIONS /api/put");
      expect(hits).not.toContain("origin PUT /api/put");
    } finally {
      await guarded.cleanup().catch(() => {});
      internal.closeAllConnections?.();
      origin.closeAllConnections?.();
      await new Promise<void>((resolve) => internal.close(() => resolve()));
      await new Promise<void>((resolve) => origin.close(() => resolve()));
    }
  });

  it("masks password field values in snapshots", async () => {
    const page = await session.ensurePage();
    await page.setContent('<label>User <input value="alice"></label><label>Pass <input type="password" id="pw"></label>');
    await page.fill("#pw", "hunter2-integration");
    const snap = await session.snapshot();
    expect(snap.text).toContain("alice");
    expect(snap.text).not.toContain("hunter2-integration");
  });

  it("shared workspace: real tabs, viewport, CDP input, stale coordinates, waits and returned control", async () => {
    const ws = new BrowserWorkspace(session);
    let n = 0;
    const send = (op: string, args = {}, tab_id: string | null = session.currentTabId() ?? null) => ws.receive({
      id: `fixture-${++n}`, runtime_id: ws.runtimeId, viewer_id: "fixture-viewer", tab_id, op, args, expires_at: Date.now() + 30000,
    });
    try {
      expect((await send("takeover", {}, null)).ok).toBe(true);
      await expect(session.runAgentExclusive(() => runTabs(session,{op:"create"}))).rejects.toThrow("human_control");
      await expect(session.runAgentExclusive(() => runAct(session, {kind:"key",key:"Escape"}))).rejects.toThrow("human_control");
      expect((await send("tabs",{action:"create",url:httpUrl},null)).ok).toBe(true);
      const created = session.currentTabId()!;
      expect((await send("viewport",{mode:"fit",width:375,height:812})).ok).toBe(true);
      await ws.refresh();
      expect(ws.state()).toMatchObject({active_tab_id:created,viewport:{mode:"fit",width:375,height:812}});
      expect(ws.state().target_id).toBeTruthy();
      const page = await session.ensurePage();
      await page.setContent('<input id="entry"><button id="button">ready</button>');
      await page.locator("#entry").focus();
      expect((await send("input",{method:"Input.insertText",params:{text:"shared hello"}})).ok).toBe(true);
      expect(await page.locator("#entry").inputValue()).toBe("shared hello");
      session.setLastScreenshot({scale:1,offsetX:0,offsetY:0});
      expect((await send("viewport",{mode:"fixed",width:1280,height:800})).ok).toBe(true);
      await expect(session.toViewportPoint([10,10])).rejects.toThrow("stale_screenshot");
      expect((await send("release",{},null)).ok).toBe(true);
      for (const input of [{condition:"url",url:httpUrl,match:"exact"}, {condition:"text",text:"ready"}, {condition:"element",selector:"#entry",state:"attached"}] as const) {
        const result = await session.runAgentExclusive(() => runWait(session,input));
        expect(JSON.parse(firstText(result)).matched).toBe(true);
      }
      const snap = await session.snapshot({filter:"interactive"});
      expect(snap.refs.length).toBeGreaterThan(0);
      expect(JSON.parse(firstText(await runWait(session,{condition:"element",ref:snap.refs[0],state:"visible"}))).matched).toBe(true);
      await session.runAgentExclusive(() => runResize(session,{preset:"tablet"}));
      await ws.refresh(); expect(ws.state().viewport).toEqual({mode:"fixed",width:768,height:1024});
      expect((await send("takeover",{},null)).ok).toBe(true);
      expect((await send("navigate",{url:httpUrl},"stale-tab")).ok).toBe(false);
      expect((await send("command",{command:"reload"})).ok).toBe(true);
      expect((await send("tabs",{action:"close",tab_id:created})).ok).toBe(true);
      await ws.refresh(); expect(ws.state().active_tab_id).not.toBe(created);
      expect((await send("release",{},null)).ok).toBe(true);
    } finally {
      ws.stop();
      session.agentAdmission = undefined; session.onWorkspaceChange = undefined; session.workspaceStatus = undefined;
    }
  }, 30000);

  it("navigation buttons traverse history, reload content, and interrupt a pending document", async () => {
    let requests = 0;
    let slowRequested!: () => void;
    const slowStarted = new Promise<void>(resolve => { slowRequested = resolve; });
    const server = createHttpServer((req, res) => {
      if (req.url === "/slow") { slowRequested(); return; }
      res.setHeader("content-type", "text/html");
      res.end(`<main>Page ${req.url}, visit ${++requests}</main>`);
    });
    await new Promise<void>(resolve => server.listen(0, "127.0.0.1", resolve));
    const address = server.address() as { port: number };
    const url = `http://127.0.0.1:${address.port}`;
    const ws = new BrowserWorkspace(session);
    let n = 0;
    const send = (op: string, args = {}) => ws.receive({
      id: `navigation-${++n}`, runtime_id: ws.runtimeId, viewer_id: "navigation-viewer",
      tab_id: session.currentTabId() ?? null, op, args, expires_at: Date.now() + 30000,
    });
    let navigation: ReturnType<BrowserWorkspace["receive"]> | undefined;
    try {
      expect((await send("takeover")).ok).toBe(true);
      expect((await send("tabs", { action: "create" })).ok).toBe(true);
      const page = await session.ensurePage();
      expect((await send("navigate", { url: `${url}/one` })).ok).toBe(true);
      expect((await send("navigate", { url: `${url}/two` })).ok).toBe(true);
      const back = await send("command", { command: "back" });
      expect(back, JSON.stringify({ back, url: page.url(), readyState: await page.evaluate(() => document.readyState) })).toMatchObject({ ok: true });
      expect(page.url()).toBe(`${url}/one`);
      expect((await send("command", { command: "forward" })).ok).toBe(true);
      expect(page.url()).toBe(`${url}/two`);
      const beforeReload = await page.locator("main").textContent();
      expect((await send("command", { command: "reload" })).ok).toBe(true);
      expect(page.url()).toBe(`${url}/two`);
      expect(await page.locator("main").textContent()).not.toBe(beforeReload);
      navigation = send("navigate", { url: `${url}/slow` });
      await raceWithTimeout(slowStarted, 5000, "slow navigation did not reach the server");
      const stopped = await raceWithTimeout(send("command", { command: "stop" }), 2000, "Stop did not interrupt navigation");
      expect(stopped.ok).toBe(true);
      expect((await raceWithTimeout(navigation, 2000, "navigation did not settle after Stop")).ok).toBe(false);
      expect((await send("navigate", { url: `${url}/recovered` })).ok).toBe(true);
      expect(page.url()).toBe(`${url}/recovered`);
      expect((await page.locator("main").textContent())).toContain("/recovered");
    } finally {
      server.closeAllConnections();
      await new Promise<void>(resolve => server.close(() => resolve()));
      if (navigation) await navigation;
      ws.stop();
      session.agentAdmission = undefined; session.onWorkspaceChange = undefined; session.workspaceStatus = undefined;
    }
  }, 30000);

  describe("fresh More menuitem refs (#1545)", () => {
    const treeOf = (result: McpToolResult): string =>
      result.content.filter(c => c.type === "text").map(c => c.text).join("\n");
    const emitted = (result: McpToolResult, role: string, name: string): string => {
      expect(result.isError, treeOf(result)).not.toBe(true);
      const ref = new RegExp(`${role} "${name}"[^\n]*\\[ref=(e\\d+)\\]`).exec(treeOf(result))?.[1];
      expect(ref, treeOf(result)).toBeTruthy();
      return ref!;
    };
    /** Real producer: open More by its ref, then snapshot the open popup. */
    async function openMoreAndSnapshot(): Promise<{ docs: string; snapshot: McpToolResult }> {
      const nav = await runNavigate(session, { url: `${httpUrl}menu` });
      expect(nav.isError, firstText(nav)).not.toBe(true);
      const more = emitted(await runSnapshot(session, { filter: "interactive" }), "button", "More");
      const opened = await runAct(session, { kind: "click", ref: more });
      expect(opened.isError, firstText(opened)).not.toBe(true);
      const snapshot = await runSnapshot(session, { filter: "interactive", max_chars: 8000 });
      return { docs: emitted(snapshot, "menuitem", "Docs"), snapshot };
    }
    const docsOpened = async (): Promise<boolean> =>
      (await session.ensurePage()).evaluate(() =>
        !(document.querySelector("#docs") as HTMLElement).hidden && !document.querySelector("#docs-item"));

    it("keeps a fresh ref across same-document URL rewrites", async () => {
      const { docs } = await openMoreAndSnapshot();
      // An SPA router rewriting the URL between snapshot and act: Playwright
      // reports each of these as framenavigated, yet the document, the open
      // popup and the menuitem are all unchanged.
      await (await session.ensurePage()).evaluate(() => {
        history.replaceState(history.state, "", "?panel=more");
        history.pushState({}, "", "#more");
        location.hash = "menu";
      });
      const res = await runAct(session, { kind: "click", ref: docs });
      expect(res.isError, firstText(res)).not.toBe(true);
      expect(await docsOpened()).toBe(true);
      // The click really removed the menuitem: its ref is now stale.
      const again = await runAct(session, { kind: "click", ref: docs });
      expect(firstText(again)).toContain("stale_ref");
    });

    it("still rejects every ref once a new document commits", async () => {
      const { docs } = await openMoreAndSnapshot();
      const page = await session.ensurePage();
      // Same URL, new document: only the document identity can tell.
      await Promise.all([page.waitForEvent("load"), page.evaluate(() => location.reload())]);
      const res = await runAct(session, { kind: "click", ref: docs });
      expect(res.isError).toBe(true);
      expect(firstText(res)).toContain("stale_ref");
    });

    it("reports a covered menuitem as not_actionable, and the same ref works once uncovered", async () => {
      const { docs } = await openMoreAndSnapshot();
      const page = await session.ensurePage();
      await page.evaluate(() => {
        const scrim = document.createElement("div");
        scrim.id = "scrim";
        scrim.style.cssText = "position:fixed;inset:0;background:transparent";
        document.body.append(scrim);
      });
      const blocked = await runAct(session, { kind: "click", ref: docs });
      expect(blocked.isError).toBe(true);
      expect(firstText(blocked)).toContain(`not_actionable: ${docs} is still valid`);
      expect(firstText(blocked)).toContain("intercepts pointer events");
      expect(firstText(blocked)).not.toContain("stale_ref");
      await page.evaluate(() => document.getElementById("scrim")?.remove());
      const res = await runAct(session, { kind: "click", ref: docs });
      expect(res.isError, firstText(res)).not.toBe(true);
      expect(await docsOpened()).toBe(true);
    });

    it("keeps full-snapshot refs resolvable after a scoped snapshot", async () => {
      const { docs, snapshot } = await openMoreAndSnapshot();
      const more = emitted(snapshot, "button", "More");
      const menu = /menu "More workspace destinations"[^\n]*\[ref=(e\d+)\]/.exec(treeOf(snapshot))?.[1];
      expect(menu, treeOf(snapshot)).toBeTruthy();
      const scoped = await runSnapshot(session, { scope_ref: menu! });
      expect(emitted(scoped, "menuitem", "Docs")).toBe(docs);
      expect(treeOf(scoped)).not.toContain('button "More"');
      // More lies outside the scoped subtree but came from the latest full
      // snapshot of this page, so it must still act.
      const res = await runAct(session, { kind: "hover", ref: more });
      expect(res.isError, firstText(res)).not.toBe(true);
      const click = await runAct(session, { kind: "click", ref: docs });
      expect(click.isError, firstText(click)).not.toBe(true);
      expect(await docsOpened()).toBe(true);
    });
  });

  it("STDIO MCP acts on fresh More menuitem refs and rejects detached and navigated refs", async () => {
    const transport = new StdioClientTransport({
      command: process.execPath,
      args: [fileURLToPath(new URL("../dist/index.js", import.meta.url))],
      env: {
        PATH: process.env.PATH ?? "",
        CHROME_CDP_PORT: new URL(cdpEndpoint).port,
        BROWSER_SSRF_ALLOW: "127.0.0.0/8,::1/128",
      },
      stderr: "pipe",
    });
    const client = new Client({ name: "menuitem-fixture", version: "1" });
    const call = async (name: string, args: Record<string, unknown>) =>
      await client.callTool({ name, arguments: args }) as McpToolResult;
    const emittedRef = (result: McpToolResult, role: string, name: string): string => {
      expect(result.isError, firstText(result)).not.toBe(true);
      const tree = result.content.filter(c => c.type === "text").map(c => c.text).join("\n");
      const ref = new RegExp(`${role} "${name}"[^\n]*\\[ref=(e\\d+)\\]`).exec(tree)?.[1];
      expect(ref, tree).toBeTruthy();
      return ref!;
    };
    try {
      await client.connect(transport);
      const nav = await call("browser_navigate", { url: `${httpUrl}menu` });
      expect(nav.isError, firstText(nav)).not.toBe(true);
      for (const filter of ["interactive", "all"] as const) {
        const more = emittedRef(await call("browser_snapshot", { filter }), "button", "More");
        expect((await call("browser_act", { kind: "click", ref: more })).isError).not.toBe(true);
        // The real producer emits this ref; no registry seeding or guessed token.
        const snapshot = await call("browser_snapshot", { filter, max_chars: 8000 });
        const docs = emittedRef(snapshot, "menuitem", "Docs");
        const action = await call("browser_act", { kind: "click", ref: docs });
        expect(action.isError, `${firstText(action)}\n${snapshot.content.map(c => c.type === "text" ? c.text : "").join("\n")}`).not.toBe(true);
        const state = await call("browser_eval", {
          code: 'return !document.querySelector("#docs").hidden && !document.querySelector("#docs-item");',
        });
        expect(state.isError, firstText(state)).not.toBe(true);
        expect(JSON.parse(firstText(state)).value).toBe("true");
        // The click actually removed the old menuitem, not just its ref record.
        const detached = await call("browser_act", { kind: "click", ref: docs });
        expect(detached.isError).toBe(true);
        expect(firstText(detached)).toContain("stale_ref");
      }
      const snapshot = await call("browser_snapshot", { filter: "interactive" });
      const more = emittedRef(snapshot, "button", "More");
      const next = emittedRef(snapshot, "link", "Next page");
      const navigation = await call("browser_act", { kind: "click", ref: next });
      expect(navigation.isError, firstText(navigation)).not.toBe(true);
      expect(JSON.parse(firstText(navigation)).url_changed).toBe(true);
      const stale = await call("browser_act", { kind: "click", ref: more });
      expect(stale.isError).toBe(true);
      expect(firstText(stale)).toContain("stale_ref");
    } finally {
      await client.close();
    }
  });

  it("STDIO MCP runtime authenticates bridge, reports target, and exposes status during human takeover", async () => {
    const reports: any[] = []; const paths: string[] = []; const queue: any[] = [];
    const uploadRoot = mkdtempSync(join(tmpdir(), "runtime-preflight-"));
    const uploadPath = join(uploadRoot, "bundle.aab"); writeFileSync(uploadPath, "preflight-only");
    const parentUploadDir = process.env.BROWSER_UPLOAD_DIR;
    delete process.env.BROWSER_UPLOAD_DIR;
    let runtime = "";
    const broker = createHttpServer(async (req,res) => {
      let raw = ""; for await (const part of req) raw += part;
      const body = JSON.parse(raw); paths.push(req.url!);
      if (body.token !== "fixture-token" || body.session_id !== "fixture-session") {res.writeHead(403);res.end();return;}
      res.setHeader("content-type","application/json");
      if (req.url?.endsWith("/control/register")) {runtime=body.runtime_id;res.end(JSON.stringify({ok:true}));}
      else if (req.url?.endsWith("/control/poll")) res.end(JSON.stringify({commands:queue.splice(0,16)}));
      else if (req.url?.endsWith("/control/report")) {reports.push(body);res.writeHead(204);res.end();}
      else {res.writeHead(404);res.end();}
    });
    await new Promise<void>(resolve => broker.listen(0,"127.0.0.1",resolve));
    const address = broker.address() as {port:number};
    const transport = new StdioClientTransport({command:process.execPath,args:[fileURLToPath(new URL("../dist/index.js",import.meta.url))],env:{
      PATH:process.env.PATH ?? "", CHROME_CDP_PORT:new URL(cdpEndpoint).port,
      BROWSER_UPLOAD_DIR:uploadRoot, AIS_BROWSER_MODE:"runtime", _AIS_BROWSER_RUNTIME_ID:"owned-preflight-child",
      BROWSER_SSRF_ALLOW:"127.0.0.0/8,::1/128", AIS_SESSION_ID:"fixture-session", AIS_PANEL_TOKEN:"fixture-token",
      AIS_PANEL_REGISTER_URL:`http://127.0.0.1:${address.port}/api/v1/browser/panel/register`,
    },stderr:"pipe"});
    const client = new Client({name:"workspace-fixture",version:"1"});
    const until = async (predicate:()=>boolean) => {
      const deadline = Date.now()+7000;
      while (!predicate()) {if(Date.now()>deadline) throw new Error("fixture condition timeout");await delay(25);}
    };
    try {
      await client.connect(transport);
      const tools = await client.listTools();
      expect(tools.tools.some(t=>t.name==="browser_wait")).toBe(true);
      const preflight = await client.callTool({name:"browser_status",arguments:{upload_paths:[uploadPath]}});
      const eligibility = JSON.parse((preflight.content as any[])[0].text);
      expect(process.env.BROWSER_UPLOAD_DIR).toBeUndefined();
      expect(eligibility).toMatchObject({ runtime_id:"owned-preflight-child", tabs:[], target_id:null,
        uploads:{ source:"browser_runtime", configured:true, enabled:true, check:{eligible:true,total_bytes:14} } });
      expect(JSON.stringify(eligibility)).not.toContain(uploadRoot);
      const nav = await client.callTool({name:"browser_navigate",arguments:{url:httpUrl}});
      expect(nav.isError).not.toBe(true);
      await until(()=>reports.some(r=>r.state.target_id && r.state.tabs.some((t:any)=>t.url===httpUrl)));
      const state = reports.at(-1).state;
      expect(runtime).toBeTruthy(); expect(state.runtime_id).toBe(runtime);
      queue.push({id:"human-take",runtime_id:runtime,tab_id:null,viewer_id:"human",op:"takeover",args:{},expires_at:Date.now()+30000});
      await until(()=>reports.some(r=>r.results.some((x:any)=>x.id==="human-take" && x.ok)));
      const status = await client.callTool({name:"browser_status",arguments:{upload_paths:[uploadPath]}});
      const humanStatus = JSON.parse((status.content as any[])[0].text);
      expect(humanStatus.control.owner).toBe("human");
      expect(humanStatus.uploads.check.eligible).toBe(true);
      expect(reports.every(r=>!("uploads" in r.state))).toBe(true);
      const denied = await client.callTool({name:"browser_resize",arguments:{preset:"mobile"}});
      expect(denied.isError).toBe(true); expect((denied.content as any[])[0].text).toContain("human_control");
      queue.push({id:"human-release",runtime_id:runtime,tab_id:null,viewer_id:"human",op:"release",args:{},expires_at:Date.now()+30000});
      await until(()=>reports.some(r=>r.results.some((x:any)=>x.id==="human-release" && x.ok)));
      const waited = await client.callTool({name:"browser_wait",arguments:{condition:"url",url:httpUrl,match:"exact",timeout_ms:1000}});
      expect(JSON.parse((waited.content as any[])[0].text).matched).toBe(true);
      expect(paths.every(path=>path.includes("/control/"))).toBe(true); // no legacy report race
    } finally {
      await client.close(); broker.closeAllConnections(); await new Promise<void>(resolve=>broker.close(()=>resolve()));
      rmSync(uploadRoot, { recursive:true, force:true });
      if (parentUploadDir === undefined) delete process.env.BROWSER_UPLOAD_DIR;
      else process.env.BROWSER_UPLOAD_DIR = parentUploadDir;
    }
  },30000);

});
