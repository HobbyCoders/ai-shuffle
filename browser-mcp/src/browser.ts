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

import {
  chromium,
  type Browser,
  type BrowserContext,
  type CDPSession,
  type Page,
  type Route,
} from "playwright-core";
import { promises as dns } from "node:dns";
import {
  compilePolicy,
  classifyIp,
  parseIp,
  normalizeHost,
  describePolicy,
  isMetadataHostname,
  type SsrfPolicy,
  type Verdict,
} from "./ssrf.js";

export const BROWSER_RESTARTING = "browser_restarting";

/** Schemes we classify. Everything else (file:, data:, about:, blob:) is passed
 * through untouched — they have no network host to guard. WebSocket upgrades are
 * not seen by CDP request interception; the egress proxy (egress.ts) covers them. */
const GUARDED_SCHEMES = new Set(["http", "https", "ws", "wss"]);

/** How long a DNS result / negative result is cached. */
const DNS_TTL_MS = 30_000;
const DNS_NEG_TTL_MS = 5_000;

/** Resolve a hostname to every address it has. Injectable for tests. */
export type HostResolver = (host: string) => Promise<string[]>;

const systemResolver: HostResolver = async (host) => {
  const results = await dns.lookup(host, { all: true });
  return results.map((r) => r.address);
};

/** The slice of a CDP session the interceptor uses (a fake in unit tests). */
export interface FetchSession {
  send(method: "Fetch.continueRequest", params: { requestId: string }): Promise<unknown>;
  send(
    method: "Fetch.failRequest",
    params: { requestId: string; errorReason: "BlockedByClient" },
  ): Promise<unknown>;
}

/** The fields of a Fetch.requestPaused event the interceptor reads. */
export interface PausedRequest {
  requestId: string;
  request: { url: string };
}

/**
 * Runtime SSRF guard: intercepts every request the browser makes and
 * classifies it by its RESOLVED IP. IP-literal hosts are classified without a
 * DNS call; hostnames are resolved via `dns.lookup` (all addresses) with a short
 * cache and blocked if ANY resolved address is blocked.
 *
 * Interception is BROWSER-WIDE CDP `Fetch` at the request stage, not
 * Playwright's context.route. Two properties of route made it unfit for a
 * security boundary: Playwright continues every redirect hop itself without
 * calling the handler (so a public page that 302s to 169.254.169.254 or a
 * loopback port was followed and read), and while any route is installed it
 * answers every CORS preflight itself with a permissive 204 (so a hostile page
 * could send non-simple PUT/DELETE/JSON requests to any reachable service).
 * The browser-level Fetch domain pauses each redirect hop as its own request,
 * leaves preflights to the real server, and covers every target in the
 * browser (all contexts, OOPIFs, workers).
 *
 * Fails CLOSED: a guard error blocks the request it was classifying.
 *
 * NOT THE WHOLE BOUNDARY: this guard never sees WebSocket handshakes, and its
 * DNS lookup is separate from the one that opens the socket, so a hostile
 * resolver could answer public here and private there. The supervised browser
 * is therefore launched behind the egress proxy (egress.ts, egress-launch.ts),
 * which receives every connection, resolves once and connects only to the
 * address it checked. This guard remains for per-session policy, redirect-hop
 * reasons the agent can read, and browsers launched without the proxy.
 */
export class SsrfGuard {
  private readonly dnsCache = new Map<string, { addrs: string[]; expires: number }>();
  private readonly recentBlocks = new Map<string, { reason: string; at: number }>();
  private readonly installed = new WeakSet<Browser>();

  constructor(
    private readonly policy: SsrfPolicy,
    private readonly resolver: HostResolver = systemResolver,
  ) {}

  summary(): string {
    return describePolicy(this.policy);
  }

  /**
   * Install browser-wide interception exactly once per connection. Falls back
   * to context.route (weaker: see the class comment) only if the browser
   * refuses browser-level Fetch.
   */
  async install(browser: Browser, context: BrowserContext): Promise<void> {
    if (this.installed.has(browser)) return;
    this.installed.add(browser);
    let cdp: CDPSession | undefined;
    try {
      cdp = await browser.newBrowserCDPSession();
      const session = cdp;
      session.on("Fetch.requestPaused", (event) => void this.handlePaused(session, event));
      await session.send("Fetch.enable", { patterns: [{ urlPattern: "*", requestStage: "Request" }] });
    } catch (err) {
      await cdp?.detach().catch(() => {});
      process.stderr.write(
        `[browser-mcp] browser-level request interception unavailable (${
          err instanceof Error ? err.message : String(err)
        }); falling back to per-context routing, which does not see redirect hops\n`,
      );
      await context.route("**/*", (route) => this.handleRoute(route));
    }
  }

  /**
   * The friendly block reason for a host we recently aborted, if any. Lets
   * navigate turn Chromium's opaque `net::ERR_BLOCKED_BY_CLIENT` into a clear
   * "blocked by SSRF policy: <reason>". Matched by host, with a short recency
   * fallback to the most recent block overall (a redirect hop's host differs
   * from the URL the agent asked for).
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

  /**
   * Decide one request URL. Unguarded schemes (no network host) pass; a guard
   * error blocks. Blocks are recorded for {@link blockReasonFor}.
   */
  async decide(url: string): Promise<Verdict> {
    let host = "";
    let scheme = "";
    let port: number | undefined;
    try {
      const u = new URL(url);
      host = u.hostname;
      scheme = u.protocol.replace(/:$/, "");
      port = u.port ? Number(u.port) : scheme === "https" || scheme === "wss" ? 443 : 80;
    } catch {
      // Chromium only issues requests for URLs it could parse; one we cannot is
      // not something to wave through.
      return { blocked: true, reason: "unparseable request URL" };
    }
    if (!GUARDED_SCHEMES.has(scheme)) return { blocked: false };
    if (host === "") return { blocked: true, reason: "request URL has no host" };

    let verdict: Verdict;
    try {
      verdict = await this.classifyHost(host, port);
    } catch (err) {
      verdict = {
        blocked: true,
        reason: `SSRF guard error (${err instanceof Error ? err.message : String(err)})`,
      };
    }
    if (verdict.blocked) this.record(host, verdict.reason ?? "blocked by SSRF policy");
    return verdict;
  }

  /** Browser-level Fetch.requestPaused handler: always answers the request. */
  async handlePaused(cdp: FetchSession, event: PausedRequest): Promise<void> {
    const verdict = await this.decide(event.request.url);
    try {
      if (verdict.blocked) {
        await cdp.send("Fetch.failRequest", { requestId: event.requestId, errorReason: "BlockedByClient" });
      } else {
        await cdp.send("Fetch.continueRequest", { requestId: event.requestId });
      }
    } catch {
      /* request already gone (tab closed / navigated away) */
    }
  }

  /** Fallback per-context route handler (see {@link install}). */
  private async handleRoute(route: Route): Promise<void> {
    const verdict = await this.decide(route.request().url());
    try {
      if (verdict.blocked) await route.abort("blockedbyclient");
      else await route.continue();
    } catch {
      /* request already handled / page navigated away */
    }
  }

  /** Classify a URL host: IP literals directly, hostnames via cached DNS. */
  async classifyHost(rawHost: string, port?: number): Promise<Verdict> {
    const host = normalizeHost(rawHost);
    if (isMetadataHostname(host)) {
      return {
        blocked: true,
        reason: `host "${host}" is a cloud metadata service: always blocked and NOT overridable`,
      };
    }
    // localhost is loopback even without a DNS call; route it through the policy
    // (127.0.0.1) so an operator BROWSER_SSRF_ALLOW override still applies.
    if (host === "localhost" || host.endsWith(".localhost")) {
      const v = classifyIp("127.0.0.1", this.policy, port);
      // Keep the classifier's reason: it names the setting that would allow it.
      return v.blocked
        ? { blocked: true, reason: `host "${host}" (127.0.0.1) is ${v.reason}` }
        : v;
    }
    if (parseIp(host)) {
      const v = classifyIp(host, this.policy, port);
      return v.blocked ? { blocked: true, reason: `host ${host} is ${v.reason}` } : v;
    }

    const addrs = await this.resolve(host);
    // Unresolvable host: let the request fail naturally in Chromium rather than
    // manufacturing a block. The egress proxy resolves for real and refuses a
    // name it cannot resolve, so a disagreeing resolver gains nothing.
    if (addrs.length === 0) return { blocked: false };
    for (const addr of addrs) {
      const v = classifyIp(addr, this.policy, port);
      if (v.blocked) return { blocked: true, reason: `${host} → ${addr}: ${v.reason}` };
    }
    return { blocked: false };
  }

  private async resolve(host: string): Promise<string[]> {
    const now = Date.now();
    const cached = this.dnsCache.get(host);
    if (cached && cached.expires > now) return cached.addrs;
    try {
      const addrs = await this.resolver(host);
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
          // Install the SSRF egress guard before any page exists. It is
          // idempotent per connection; after a reconnect the Browser is a fresh
          // object, so interception is re-installed.
          await this.guard.install(browser, this.context);
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
