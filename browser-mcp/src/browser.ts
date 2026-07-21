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

import { chromium, type Browser, type BrowserContext, type Page, type Route } from "playwright-core";
import { promises as dns } from "node:dns";
import {
  compilePolicy,
  classifyIp,
  parseIp,
  normalizeHost,
  describePolicy,
  type SsrfPolicy,
  type Verdict,
} from "./ssrf.js";

export const BROWSER_RESTARTING = "browser_restarting";

/** Schemes we classify. Everything else (file:, data:, about:, blob:) is passed
 * through untouched — they have no network host to guard. WebSocket upgrades are
 * not covered by context.route (a documented gap). */
const GUARDED_SCHEMES = new Set(["http", "https", "ws", "wss"]);

/** How long a DNS result / negative result is cached. */
const DNS_TTL_MS = 30_000;
const DNS_NEG_TTL_MS = 5_000;

/**
 * Runtime SSRF guard: installs Playwright request interception on a context and
 * classifies every http(s) request by its RESOLVED IP. IP-literal hosts are
 * classified without a DNS call; hostnames are resolved via `dns.lookup` (all
 * addresses) with a short cache and blocked if ANY resolved address is blocked.
 *
 * RESIDUAL TOCTOU / DNS-REBINDING GAP (accepted, see plan open-question #3):
 * Chromium performs its OWN name resolution for the actual socket. Our lookup
 * and Chromium's are two separate resolutions, so a hostile resolver could hand
 * us a public IP and Chromium a private one between the two. A robust fix needs
 * `--host-resolver-rules` or an egress proxy; this URL-host guard closes the
 * common cases (IP literals, redirects to metadata/loopback, stable rebinds).
 */
export class SsrfGuard {
  private readonly dnsCache = new Map<string, { addrs: string[]; expires: number }>();
  private readonly recentBlocks = new Map<string, { reason: string; at: number }>();
  private readonly installed = new WeakSet<BrowserContext>();

  constructor(private readonly policy: SsrfPolicy) {}

  summary(): string {
    return describePolicy(this.policy);
  }

  /** Install interception on a context exactly once. */
  async install(context: BrowserContext): Promise<void> {
    if (this.installed.has(context)) return;
    this.installed.add(context);
    await context.route("**/*", (route) => this.handle(route));
  }

  /**
   * The friendly block reason for a host we recently aborted, if any. Lets
   * navigate turn Chromium's opaque `net::ERR_BLOCKED_BY_CLIENT` into a clear
   * "blocked by SSRF policy: <reason>". Matched by host, with a short recency
   * fallback to the most recent block overall.
   */
  blockReasonFor(host: string): string | undefined {
    const now = Date.now();
    const norm = normalizeHost(host);
    const byHost = this.recentBlocks.get(norm);
    if (byHost && now - byHost.at < 15_000) return byHost.reason;
    let latest: { reason: string; at: number } | undefined;
    for (const entry of this.recentBlocks.values()) {
      if (!latest || entry.at > latest.at) latest = entry;
    }
    if (latest && now - latest.at < 5_000) return latest.reason;
    return undefined;
  }

  private record(host: string, reason: string): void {
    this.recentBlocks.set(normalizeHost(host), { reason, at: Date.now() });
    // Keep the map from growing without bound over a long session.
    if (this.recentBlocks.size > 256) {
      const oldest = [...this.recentBlocks.entries()].sort((a, b) => a[1].at - b[1].at)[0];
      if (oldest) this.recentBlocks.delete(oldest[0]);
    }
  }

  private async handle(route: Route): Promise<void> {
    let host = "";
    let scheme = "";
    try {
      const u = new URL(route.request().url());
      host = u.hostname;
      scheme = u.protocol.replace(/:$/, "");
    } catch {
      return this.safeContinue(route);
    }
    if (host === "" || !GUARDED_SCHEMES.has(scheme)) return this.safeContinue(route);

    let verdict: Verdict;
    try {
      verdict = await this.classifyHost(host);
    } catch {
      // Never let a guard bug wedge the page: fail open on unexpected errors.
      return this.safeContinue(route);
    }
    if (verdict.blocked) {
      this.record(host, verdict.reason ?? "blocked by SSRF policy");
      try {
        await route.abort("blockedbyclient");
      } catch {
        /* request already handled / page navigated away */
      }
      return;
    }
    return this.safeContinue(route);
  }

  private async safeContinue(route: Route): Promise<void> {
    try {
      await route.continue();
    } catch {
      /* request already handled / page navigated away */
    }
  }

  /** Classify a URL host: IP literals directly, hostnames via cached DNS. */
  async classifyHost(rawHost: string): Promise<Verdict> {
    const host = normalizeHost(rawHost);
    // localhost is loopback even without a DNS call; route it through the policy
    // (127.0.0.1) so an operator BROWSER_SSRF_ALLOW override still applies.
    if (host === "localhost" || host.endsWith(".localhost")) {
      const v = classifyIp("127.0.0.1", this.policy);
      return v.blocked ? { blocked: true, reason: `loopback host "${host}" (127.0.0.1)` } : v;
    }
    if (parseIp(host)) return classifyIp(host, this.policy);

    const addrs = await this.resolve(host);
    // Unresolvable host: let the request fail naturally in Chromium rather than
    // manufacturing a block (nothing to protect — the connection cannot form).
    if (addrs.length === 0) return { blocked: false };
    for (const addr of addrs) {
      const v = classifyIp(addr, this.policy);
      if (v.blocked) return { blocked: true, reason: `${host} → ${addr}: ${v.reason}` };
    }
    return { blocked: false };
  }

  private async resolve(host: string): Promise<string[]> {
    const now = Date.now();
    const cached = this.dnsCache.get(host);
    if (cached && cached.expires > now) return cached.addrs;
    try {
      const results = await dns.lookup(host, { all: true });
      const addrs = results.map((r) => r.address);
      this.dnsCache.set(host, { addrs, expires: now + DNS_TTL_MS });
      return addrs;
    } catch {
      this.dnsCache.set(host, { addrs: [], expires: now + DNS_NEG_TTL_MS });
      return [];
    }
  }
}

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
  /** Compiled SSRF policy. Defaults to one compiled from the environment. */
  ssrfPolicy?: SsrfPolicy;
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
  private readonly guard: SsrfGuard;

  constructor(opts: BrowserManagerOptions = {}) {
    this.endpoint = opts.endpoint ?? cdpEndpoint();
    this.baseBackoffMs = opts.baseBackoffMs ?? 200;
    this.maxBackoffMs = opts.maxBackoffMs ?? 5000;
    this.maxRetries = opts.maxRetries ?? 6;
    this.guard = new SsrfGuard(opts.ssrfPolicy ?? compilePolicy());
  }

  /** One-line summary of the effective SSRF policy (for startup logging). */
  ssrfPolicySummary(): string {
    return this.guard.summary();
  }

  /** Friendly SSRF block reason for a recently aborted host, if any. */
  ssrfBlockReason(host: string): string | undefined {
    return this.guard.blockReasonFor(host);
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
          // Install the SSRF egress guard on the (possibly reused) context. The
          // guard is idempotent per context; after a reconnect the context is a
          // fresh object, so interception is re-installed.
          await this.guard.install(this.context);
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
