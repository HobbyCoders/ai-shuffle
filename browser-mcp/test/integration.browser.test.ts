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
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import { BrowserManager } from "../src/browser.js";
import { Session } from "../src/session.js";
import { runNavigate } from "../src/tools/navigate.js";
import { runSnapshot } from "../src/tools/snapshot.js";
import { runAct } from "../src/tools/act.js";
import { runScreenshot } from "../src/tools/screenshot.js";
import type { McpToolResult } from "../src/tools/result.js";
import { WATCH_KEY } from "../src/dommutation.js";

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

describe.skipIf(!browserPath)("integration: core tools over CDP", () => {
  let proc: ChildProcess;
  let userDataDir: string;
  let manager: BrowserManager;
  let session: Session;

  beforeAll(async () => {
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
    manager = new BrowserManager({ endpoint: `http://127.0.0.1:${port}` });
    session = new Session(manager);
  }, 30000);

  afterAll(async () => {
    try {
      await manager?.cleanup();
    } catch {
      /* ignore */
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
});
