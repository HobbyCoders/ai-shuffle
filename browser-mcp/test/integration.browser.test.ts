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
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import { BrowserManager } from "../src/browser.js";
import { compilePolicy } from "../src/ssrf.js";
import { Session } from "../src/session.js";
import { runNavigate } from "../src/tools/navigate.js";
import { runSnapshot } from "../src/tools/snapshot.js";
import { runAct } from "../src/tools/act.js";
import { runScreenshot } from "../src/tools/screenshot.js";
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
const fixtureUrl = "file://" + fileURLToPath(new URL("./fixtures/basic.html", import.meta.url));
const uploadFixtureUrl = "file://" + fileURLToPath(new URL("./fixtures/upload.html", import.meta.url));

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
      // dispatcher (index.ts) turns the throw into an errorResult. Assert the
      // friendly reason reaches the caller.
      await expect(runNavigate(guardedSession, { url: httpUrl })).rejects.toThrow(
        /blocked by SSRF policy:.*loopback/,
      );
    } finally {
      // Close only the page we opened; leave the shared browser to the suite.
      await guarded.cleanup().catch(() => {});
    }
  });
});
