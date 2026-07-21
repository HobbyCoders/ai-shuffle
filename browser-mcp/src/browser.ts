/**
 * CDP connection management.
 *
 * Connects to the externally supervised Chromium over CDP (the supervisor keeps
 * ownership of the profile / SingletonLock — we are only a thin client). We
 * reuse the persistent default context if one exists, else create one, and hand
 * out one Page per session. On an unexpected disconnect we reconnect with
 * backoff; a tool call arriving mid-reconnect returns `browser_restarting`
 * rather than hanging.
 */

import { chromium, type Browser, type BrowserContext, type Page } from "playwright-core";

export const BROWSER_RESTARTING = "browser_restarting";

export class BrowserRestartingError extends Error {
  constructor() {
    super(BROWSER_RESTARTING);
    this.name = "BrowserRestartingError";
  }
}

function cdpEndpoint(): string {
  const port = process.env.CHROME_CDP_PORT ?? "9221";
  return `http://127.0.0.1:${port}`;
}

export interface BrowserManagerOptions {
  endpoint?: string;
  maxBackoffMs?: number;
  baseBackoffMs?: number;
  maxRetries?: number;
}

export class BrowserManager {
  private browser?: Browser;
  private context?: BrowserContext;
  /** True when WE created `context` (no persistent context existed to reuse). */
  private ownsContext = false;
  private reconnecting = false;
  private closed = false;
  /** Pages this process created, for cleanup on shutdown. */
  private readonly ownedPages = new Set<Page>();

  private readonly endpoint: string;
  private readonly baseBackoffMs: number;
  private readonly maxBackoffMs: number;
  private readonly maxRetries: number;

  constructor(opts: BrowserManagerOptions = {}) {
    this.endpoint = opts.endpoint ?? cdpEndpoint();
    this.baseBackoffMs = opts.baseBackoffMs ?? 200;
    this.maxBackoffMs = opts.maxBackoffMs ?? 5000;
    this.maxRetries = opts.maxRetries ?? 6;
  }

  private async ensureBrowser(): Promise<Browser> {
    if (this.reconnecting) throw new BrowserRestartingError();
    if (this.browser && this.browser.isConnected()) return this.browser;

    this.reconnecting = true;
    try {
      let attempt = 0;
      let delay = this.baseBackoffMs;
      // eslint-disable-next-line no-constant-condition
      while (true) {
        try {
          const browser = await chromium.connectOverCDP(this.endpoint);
          browser.on("disconnected", () => {
            this.browser = undefined;
            this.context = undefined;
            this.ownsContext = false;
          });
          this.browser = browser;
          const existing = browser.contexts()[0];
          if (existing) {
            this.context = existing;
            this.ownsContext = false;
          } else {
            this.context = await browser.newContext();
            this.ownsContext = true;
          }
          return browser;
        } catch (err) {
          attempt += 1;
          if (attempt >= this.maxRetries) throw err;
          await sleep(delay);
          delay = Math.min(delay * 2, this.maxBackoffMs);
        }
      }
    } finally {
      this.reconnecting = false;
    }
  }

  /** Acquire a fresh page for a session (one Page per session). */
  async newPage(): Promise<Page> {
    if (this.closed) throw new Error("browser manager closed");
    await this.ensureBrowser();
    if (!this.context) throw new BrowserRestartingError();
    const page = await this.context.newPage();
    this.ownedPages.add(page);
    page.on("close", () => this.ownedPages.delete(page));
    return page;
  }

  isReconnecting(): boolean {
    return this.reconnecting;
  }

  /** Close only the pages this process opened; leave the browser/profile alone. */
  async cleanup(): Promise<void> {
    this.closed = true;
    const pages = [...this.ownedPages];
    this.ownedPages.clear();
    await Promise.allSettled(pages.map((p) => p.close()));
    // Close a context we created ourselves (leave a reused persistent one).
    if (this.ownsContext && this.context) {
      try {
        await this.context.close();
      } catch {
        /* best effort */
      }
      this.ownsContext = false;
      this.context = undefined;
    }
    // We do NOT close the browser: the supervisor owns its lifecycle.
    try {
      // Detach our CDP connection without killing the browser.
      if (this.browser && this.browser.isConnected()) {
        // playwright's Browser.close() over a CDP connection disconnects the
        // client without terminating the remote browser process.
        await this.browser.close();
      }
    } catch {
      /* best effort */
    }
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}
