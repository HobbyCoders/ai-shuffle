/**
 * Optional integration test. Gated: it only runs when a system Chromium/Chrome
 * is discoverable. It launches that browser headless with a remote-debugging
 * port, connects over CDP exactly as the server does, and drives the core tools
 * against a static fixture. When no browser is found the whole suite is skipped
 * so `npm test` stays green in browser-less CI.
 */

import { describe, it, expect, beforeAll, afterAll } from "vitest";
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
import { runNetwork } from "../src/tools/network.js";
import { runTabs } from "../src/tools/tabs.js";
import { runResize } from "../src/tools/resize.js";
import { runUpload } from "../src/tools/upload.js";
import { runBatch } from "../src/tools/batch.js";
import { TOOL_MAP } from "../src/tools/index.js";
import type { McpToolResult } from "../src/tools/result.js";
import { WATCH_KEY } from "../src/dommutation.js";
import { BrowserWorkspace } from "../src/workspace.js";
import { runWait } from "../src/tools/wait.js";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";

const NET_BODY_MARKER = "net-body-marker-12345";

/** Small HTTP app: an HTML page that logs to the console and fetches JSON. */
function startHttpApp(): Promise<{ server: HttpServer; url: string }> {
  const html = `<!doctype html><html><head><title>net fixture</title></head>
<body><main><p id="out">pending</p></main>
<script>
console.log("http-fixture-log hi");
fetch("/api/data").then(r=>r.json()).then(d=>{document.getElementById("out").textContent=JSON.stringify(d);});
</script></body></html>`;
  const server = createHttpServer((req, res) => {
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
      resolve({ server, url: `http://127.0.0.1:${port}/` });
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

  it("snapshot collapses the long list of rows", async () => {
    const res = await runSnapshot(session, { filter: "all" });
    const snapText = res.content[1] && res.content[1].type === "text" ? res.content[1].text : "";
    expect(snapText).toContain("more similar items");
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
    expect(firstText(res)).toMatch(/\d+x\d+ — coordinates/);
  });

  it("oversized viewport screenshot is downscaled to MAX_EDGE", async () => {
    const page = await session.ensurePage();
    await page.setViewportSize({ width: 1600, height: 1000 });
    try {
      const res = await runScreenshot(session, {});
      if (res.isError) throw new Error(firstText(res));
      const m = firstText(res).match(/(\d+)x(\d+) — coordinates/);
      expect(m).not.toBeNull();
      const [w, h] = [Number(m![1]), Number(m![2])];
      expect(Math.max(w, h)).toBeLessThanOrEqual(MAX_EDGE);
      // The stored mapping must let coordinate acts scale back to page pixels.
      const decoded = res.content.find((c) => c.type === "image");
      expect(decoded && decoded.type === "image" ? decoded.data.length : 0).toBeGreaterThan(0);
    } finally {
      await page.setViewportSize({ width: 1280, height: 720 });
    }
  });

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
    const res = await runFind(session, { query: "submit button" });
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

  it("STDIO MCP runtime authenticates bridge, reports target, and exposes status during human takeover", async () => {
    const reports: any[] = []; const paths: string[] = []; const queue: any[] = [];
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
      const nav = await client.callTool({name:"browser_navigate",arguments:{url:httpUrl}});
      expect(nav.isError).not.toBe(true);
      await until(()=>reports.some(r=>r.state.target_id && r.state.tabs.some((t:any)=>t.url===httpUrl)));
      const state = reports.at(-1).state;
      expect(runtime).toBeTruthy(); expect(state.runtime_id).toBe(runtime);
      queue.push({id:"human-take",runtime_id:runtime,tab_id:null,viewer_id:"human",op:"takeover",args:{},expires_at:Date.now()+30000});
      await until(()=>reports.some(r=>r.results.some((x:any)=>x.id==="human-take" && x.ok)));
      const status = await client.callTool({name:"browser_status",arguments:{}});
      expect(JSON.parse((status.content as any[])[0].text).control.owner).toBe("human");
      const denied = await client.callTool({name:"browser_resize",arguments:{preset:"mobile"}});
      expect(denied.isError).toBe(true); expect((denied.content as any[])[0].text).toContain("human_control");
      queue.push({id:"human-release",runtime_id:runtime,tab_id:null,viewer_id:"human",op:"release",args:{},expires_at:Date.now()+30000});
      await until(()=>reports.some(r=>r.results.some((x:any)=>x.id==="human-release" && x.ok)));
      const waited = await client.callTool({name:"browser_wait",arguments:{condition:"url",url:httpUrl,match:"exact",timeout_ms:1000}});
      expect(JSON.parse((waited.content as any[])[0].text).matched).toBe(true);
      expect(paths.every(path=>path.includes("/control/"))).toBe(true); // no legacy report race
    } finally {
      await client.close(); broker.closeAllConnections(); await new Promise<void>(resolve=>broker.close(()=>resolve()));
    }
  },30000);

});
