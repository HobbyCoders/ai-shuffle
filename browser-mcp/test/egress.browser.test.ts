/**
 * Egress proxy against a real Chromium, launched exactly as production does
 * (launchBehindProxy). Gated on a discoverable browser, like
 * integration.browser.test.ts. No public network: 127.0.0.1 plays a public
 * address (allow-listed) and 127.0.0.2 an internal one (default-blocked); both
 * listen on the SAME port so a rebind keeps the page's origin.
 */

import { describe, it, expect, beforeAll, afterAll } from "vitest";
import { execSync } from "node:child_process";
import { createHash } from "node:crypto";
import { createServer as createHttpServer, type Server as HttpServer } from "node:http";
import { existsSync, mkdtempSync, rmSync } from "node:fs";
import { createServer as createNetServer, type Socket } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { chromium, type Browser, type Page } from "playwright-core";

import { launchBehindProxy, type EgressResolver, type LaunchedBrowser } from "../src/egress.js";
import { compilePolicy } from "../src/ssrf.js";
import { EGRESS_REFUSED_NET_ERRORS, RETRYABLE_NET_ERRORS } from "../src/loopback.js";

function findBrowser(): string | null {
  if (process.platform === "win32") {
    for (const base of [process.env.PROGRAMFILES, process.env["PROGRAMFILES(X86)"], process.env.LOCALAPPDATA]) {
      if (!base) continue;
      for (const rel of ["Google/Chrome/Application/chrome.exe", "Microsoft/Edge/Application/msedge.exe"]) {
        if (existsSync(join(base, rel))) return join(base, rel);
      }
    }
    return null;
  }
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
    const srv = createNetServer();
    srv.listen(0, "127.0.0.1", () => {
      const port = (srv.address() as { port: number }).port;
      srv.close(() => resolve(port));
    });
    srv.on("error", reject);
  });
}

interface Site {
  server: HttpServer;
  requests: string[];
  upgrades: number;
  connections: number;
  sockets: Set<Socket>;
}

/** An HTTP page plus a minimal WebSocket endpoint that greets and closes. */
function startSite(host: string, port: number, title: string): Promise<Site> {
  const site: Site = { server: createHttpServer(), requests: [], upgrades: 0, connections: 0, sockets: new Set() };
  site.server.on("connection", (socket) => {
    site.connections++;
    // Upgraded sockets leave the server's bookkeeping; track them to close.
    site.sockets.add(socket);
    socket.on("close", () => site.sockets.delete(socket));
  });
  site.server.on("request", (req, res) => {
    site.requests.push(req.url ?? "");
    res.writeHead(200, { "content-type": "text/html" });
    res.end(`<title>${title}</title><p>${title}</p>`);
  });
  site.server.on("upgrade", (req, socket) => {
    site.upgrades++;
    const accept = createHash("sha1")
      .update(`${req.headers["sec-websocket-key"]}258EAFA5-E914-47DA-95CA-C5AB0DC85B11`)
      .digest("base64");
    socket.write(
      `HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: ${accept}\r\n\r\n`,
    );
    const payload = Buffer.from(`hello from ${title}`);
    socket.write(Buffer.concat([Buffer.from([0x81, payload.length]), payload]));
    socket.on("error", () => {});
  });
  return new Promise((resolve, reject) => {
    site.server.once("error", reject);
    site.server.listen(port, host, () => resolve(site));
  });
}

async function waitForCdp(port: number, timeoutMs = 15000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
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

/** Open a WebSocket in the page (or a dedicated worker) and report what happened. */
function openSocket(page: Page, url: string, inWorker = false): Promise<string> {
  return page.evaluate(
    ({ url, inWorker }) => {
      const probe = `(${String((u: string, post: (s: string) => void) => {
        const ws = new WebSocket(u);
        const timer = setTimeout(() => post("timeout"), 8000);
        ws.onmessage = (e) => {
          clearTimeout(timer);
          post(`message ${e.data}`);
        };
        ws.onerror = () => {
          clearTimeout(timer);
          post("error");
        };
      })})`;
      if (!inWorker) {
        return new Promise<string>((resolve) => (0, eval)(probe)(url, resolve));
      }
      const src = `${probe}(${JSON.stringify(url)}, (s) => postMessage(s));`;
      const worker = new Worker(URL.createObjectURL(new Blob([src], { type: "text/javascript" })));
      return new Promise<string>((resolve) => (worker.onmessage = (e) => resolve(String(e.data))));
    },
    { url, inWorker },
  );
}

const browserPath = findBrowser();

describe.skipIf(!browserPath)("egress proxy with a real Chromium", () => {
  let launched: LaunchedBrowser;
  let browser: Browser;
  let page: Page;
  let pub: Site;
  let internal: Site;
  let port: number;
  let userDataDir: string;
  /** What rebind.test answers right now; flipped by the rebinding test. */
  let rebindAnswer = ["127.0.0.1"];
  const lookups: string[] = [];
  const refusals: string[] = [];

  beforeAll(async () => {
    port = await freePort();
    pub = await startSite("127.0.0.1", port, "PUBLIC");
    internal = await startSite("127.0.0.2", port, "INTERNAL");
    const cdpPort = await freePort();
    const resolver: EgressResolver = async (host) => {
      lookups.push(host);
      if (host === "rebind.test") return rebindAnswer;
      throw new Error("ENOTFOUND");
    };
    userDataDir = mkdtempSync(join(tmpdir(), "browser-egress-it-"));
    launched = await launchBehindProxy(
      [
        browserPath!,
        "--headless=new",
        "--no-sandbox",
        "--disable-gpu",
        "--disable-background-networking",
        `--remote-debugging-port=${cdpPort}`,
        `--user-data-dir=${userDataDir}`,
        // A caller-supplied proxy switch must be dropped, never win.
        "--proxy-server=direct://",
        "about:blank",
      ],
      {
        policy: compilePolicy({ BROWSER_SSRF_ALLOW: "127.0.0.1/32", CHROME_CDP_PORT: String(cdpPort) } as NodeJS.ProcessEnv),
        resolver,
        stdio: "ignore",
        log: (line) => refusals.push(line),
      },
    );
    await waitForCdp(cdpPort);
    browser = await chromium.connectOverCDP(`http://127.0.0.1:${cdpPort}`);
    page = await (browser.contexts()[0] ?? (await browser.newContext())).newPage();
  }, 40000);

  afterAll(async () => {
    await browser?.close().catch(() => {});
    if (launched) {
      launched.child.kill("SIGKILL");
      await launched.exited;
    }
    for (const site of [pub, internal]) {
      if (!site) continue;
      site.server.closeAllConnections();
      for (const socket of site.sockets) socket.destroy();
      await new Promise<void>((r) => site.server.close(() => r()));
    }
    for (let attempt = 0; attempt < 5 && userDataDir; attempt++) {
      try {
        rmSync(userDataDir, { recursive: true, force: true });
        break;
      } catch {
        await new Promise((r) => setTimeout(r, 200));
      }
    }
  });

  it("routes loopback through the proxy: an allowed page loads, a blocked one never connects", async () => {
    await page.goto(`http://127.0.0.1:${port}/`);
    expect(await page.title()).toBe("PUBLIC");
    const response = await page.goto(`http://127.0.0.2:${port}/secret`);
    expect(response?.status()).toBe(403);
    expect(await page.content()).not.toContain("INTERNAL");
    expect(internal.connections).toBe(0);
  });

  it("reports a loopback port nobody answers as a retryable network error", async () => {
    const closed = await freePort();
    // Its own page: the error page commits late and would race the next goto.
    const scratch = await page.context().newPage();
    try {
      const failure = await scratch.goto(`http://127.0.0.1:${closed}/`).then(
        () => "loaded",
        (err: Error) => err.message,
      );
      expect(failure).toMatch(RETRYABLE_NET_ERRORS);
    } finally {
      await scratch.close();
    }
  });

  it("tells a policy refusal apart from a target that does not answer", async () => {
    const closed = await freePort();
    // A page per navigation: an error page commits late and would race the next goto.
    const visit = async <T>(url: string, read: (p: Promise<Awaited<ReturnType<Page["goto"]>>>) => Promise<T>) => {
      const scratch = await page.context().newPage();
      try {
        return await read(scratch.goto(url));
      } finally {
        await scratch.close();
      }
    };
    const failure = (url: string) =>
      visit(url, (nav) =>
        nav.then(
          () => "loaded",
          (err: Error) => err.message,
        ),
      );
    // https goes through CONNECT: refused by policy vs. allowed but closed.
    const refused = await failure(`https://127.0.0.2:${port}/`);
    expect(refused).toMatch(EGRESS_REFUSED_NET_ERRORS);
    expect(refused).not.toMatch(RETRYABLE_NET_ERRORS);
    const down = await failure(`https://127.0.0.1:${closed}/`);
    expect(down).toMatch(RETRYABLE_NET_ERRORS);
    expect(down).not.toMatch(EGRESS_REFUSED_NET_ERRORS);
    // Plain http: the proxy's 403 page carries the marker header.
    const marker = await visit(`http://127.0.0.2:${port}/`, async (nav) => (await nav)?.headers()["x-browser-egress"]);
    expect(marker).toBe("blocked");
    expect(internal.connections).toBe(0);
  });

  it("refuses a WebSocket to a blocked loopback port, from the page and from a worker", async () => {
    await page.goto(`http://127.0.0.1:${port}/`);
    expect(await openSocket(page, `ws://127.0.0.1:${port}/ws`)).toBe("message hello from PUBLIC");
    expect(await openSocket(page, `ws://127.0.0.2:${port}/ws`)).toBe("error");
    expect(await openSocket(page, `ws://127.0.0.2:${port}/ws`, true)).toBe("error");
    expect(internal.upgrades).toBe(0);
    expect(internal.connections).toBe(0);
    expect(refusals.some((l) => l.includes(`CONNECT 127.0.0.2:${port}`))).toBe(true);
  });

  it("DNS rebinding: a name that turns private after the page loads cannot reach the private address", async () => {
    rebindAnswer = ["127.0.0.1"];
    await page.goto(`http://rebind.test:${port}/`);
    expect(await page.title()).toBe("PUBLIC");
    expect(lookups).toContain("rebind.test");

    // The attacker's DNS now answers with the internal address. Same origin, so
    // without a single pinned resolution the page could read the response.
    rebindAnswer = ["127.0.0.2"];
    const fetched = await page.evaluate(() =>
      fetch("/secret").then(
        async (r) => `${r.status} ${await r.text()}`,
        (e) => `error ${e}`,
      ),
    );
    expect(fetched).not.toContain("INTERNAL");
    expect(fetched).toMatch(/^403 /);
    expect(await openSocket(page, `ws://rebind.test:${port}/ws`)).toBe("error");
    expect(internal.connections).toBe(0);
  });
});
