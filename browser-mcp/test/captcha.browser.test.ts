/**
 * Withheld-captcha detection against a real Chromium (#2378 in the tracker).
 * Gated like integration.browser.test.ts: skipped when no system browser is
 * found. Fixtures are data: URLs, so nothing leaves the machine; a "rendered"
 * widget is an iframe whose src names a reCAPTCHA anchor without loading it.
 */

import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { spawn, execSync, type ChildProcess } from "node:child_process";
import { createServer } from "node:net";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { stopBrowser } from "./browser-process.js";
import { BrowserManager } from "../src/browser.js";
import { Session } from "../src/session.js";
import { runNavigate } from "../src/tools/navigate.js";
import { runSnapshot } from "../src/tools/snapshot.js";
import { runAct } from "../src/tools/act.js";
import { CAPTCHA_AGENT_NOTE, CAPTCHA_PANEL_NOTICE } from "../src/captcha.js";
import type { McpToolResult } from "../src/tools/result.js";

function findBrowser(): string | null {
  if (process.env.CHROMIUM_PATH) return process.env.CHROMIUM_PATH;
  if (process.platform === "win32") return null;
  for (const cmd of ["chromium", "chromium-browser", "google-chrome-stable", "google-chrome"]) {
    try {
      const p = execSync(`command -v ${cmd}`, { stdio: ["ignore", "pipe", "ignore"] }).toString().trim();
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
      const port = addr && typeof addr === "object" ? addr.port : 0;
      srv.close(() => (port ? resolve(port) : reject(new Error("no port"))));
    });
    srv.on("error", reject);
  });
}

async function waitForCdp(port: number): Promise<void> {
  const deadline = Date.now() + 10000;
  while (Date.now() < deadline) {
    try {
      if ((await fetch(`http://127.0.0.1:${port}/json/version`)).ok) return;
    } catch {
      /* not up yet */
    }
    await new Promise((r) => setTimeout(r, 150));
  }
  throw new Error("CDP endpoint did not come up");
}

function body(result: McpToolResult): Record<string, unknown> {
  const t = result.content.find((c) => c.type === "text");
  return JSON.parse(t && t.type === "text" ? t.text : "{}");
}

const page = (html: string) => "data:text/html," + encodeURIComponent(`<!doctype html><title>t</title>${html}`);

/** Google Groups' shape: a dialog that says captcha, and nothing in it. */
const WITHHELD = page(
  '<div role="dialog" style="width:300px;height:120px"><h2>Captcha required</h2><div class="body"></div><button>Cancel</button></div>',
);
/** A widget placeholder whose script adds the challenge frame after a moment. */
const LATE = page(
  '<div class="g-recaptcha" data-sitekey="k" style="width:304px;height:78px"></div>' +
    '<script>setTimeout(()=>{const f=document.createElement("iframe");f.src="about:blank#recaptcha/api2/anchor";document.querySelector(".g-recaptcha").appendChild(f)},700)</script>',
);
/** A button that opens the empty dialog, as "Create group" did. */
const OPENS = page(
  '<button onclick="document.body.insertAdjacentHTML(\'beforeend\',\'<div role=dialog style=width:300px;height:120px>Please complete the captcha</div>\')">Create</button>',
);
const PLAIN = page("<p>No captcha here</p><button>Go</button>");

const browserPath = findBrowser();

describe.skipIf(!browserPath)("withheld captcha detection", () => {
  let proc: ChildProcess;
  let userDataDir: string;
  let manager: BrowserManager;
  let session: Session;

  beforeAll(async () => {
    const port = await freePort();
    userDataDir = mkdtempSync(join(tmpdir(), "browser-mcp-captcha-"));
    proc = spawn(
      browserPath!,
      ["--headless=new", "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage", `--remote-debugging-port=${port}`, `--user-data-dir=${userDataDir}`, "about:blank"],
      { stdio: "ignore", detached: process.platform !== "win32" },
    );
    await waitForCdp(port);
    manager = new BrowserManager({ endpoint: `http://127.0.0.1:${port}` });
    session = new Session(manager);
  }, 30000);

  afterAll(async () => {
    await manager?.cleanup().catch(() => {});
    await stopBrowser(proc);
    if (userDataDir) {
      rmSync(userDataDir, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 });
    }
  });

  it("a captcha dialog the site left empty tells the agent and the panel", async () => {
    const result = body(await runNavigate(session, { url: WITHHELD }));
    expect(result.captcha_unavailable).toBe(true);
    expect(result.captcha_note).toBe(CAPTCHA_AGENT_NOTE);
    expect(session.captchaNotice).toBe(CAPTCHA_PANEL_NOTICE);
    expect((await session.workspaceMetadata()).notice).toBe(CAPTCHA_PANEL_NOTICE);

    const snap = body(await runSnapshot(session, {}));
    expect(snap.captcha_unavailable).toBe(true);
  });

  it("a widget that loads within the grace period is not reported", async () => {
    const result = body(await runNavigate(session, { url: LATE }));
    expect(result.captcha_unavailable).toBeUndefined();
    expect(session.captchaNotice).toBeNull();
  });

  it("a page without a captcha clears the notice", async () => {
    await runNavigate(session, { url: WITHHELD });
    expect(session.captchaNotice).toBe(CAPTCHA_PANEL_NOTICE);
    const result = body(await runNavigate(session, { url: PLAIN }));
    expect(result.captcha_unavailable).toBeUndefined();
    expect(session.captchaNotice).toBeNull();
    expect("notice" in (await session.workspaceMetadata())).toBe(false);
  });

  it("an action that opens an empty captcha dialog reports it", async () => {
    await runNavigate(session, { url: OPENS });
    const snap = await session.snapshot({ filter: "interactive" });
    const ref = snap.refs[0];
    expect(ref).toBeTruthy();
    const result = body(await runAct(session, { kind: "click", ref }));
    expect(result.ok).toBe(true);
    expect(result.captcha_unavailable).toBe(true);
    expect(session.captchaNotice).toBe(CAPTCHA_PANEL_NOTICE);
  });
});
