/**
 * Session state: the pages this process drives, the ref registry, and the scale
 * factor of the most recent screenshot (so coordinate-based acts can map
 * screenshot pixels back to page pixels). Wraps the browser-facing calls; the
 * pure snapshot / ref / mutation / buffer logic lives in their own modules.
 *
 * A session owns MULTIPLE pages (tabs) in a map keyed by id (tab1, tab2, …) with
 * one "current" page pointer. Every tool except browser_tabs operates on the
 * current page; browser_tabs switches which page is current. Each page carries
 * its own console + network ring buffers, populated by listeners installed once
 * when the page is acquired. Switching or closing the current page invalidates
 * refs (they only resolve against the page they were snapshotted from).
 */

import type { Page, Locator, ConsoleMessage, Response, CDPSession } from "playwright-core";
import { BrowserManager, BrowserRestartingError } from "./browser.js";
import { RefRegistry, STALE_REF_REMEDY, isRefToken } from "./refs.js";
import {
  renderSnapshot,
  type SnapshotRenderOptions,
} from "./snapshot.js";
import {
  newConsoleBuffer,
  NetworkBuffer,
  cappedConsoleEntry,
  type ConsoleBuffer,
  type ConsoleEntry,
  type NetworkEntry,
} from "./buffers.js";
import {
  installMutationWatcher,
  collectMutationStats,
  classifyMutations,
  QUIET_MS,
  QUIET_CAP_MS,
  WATCH_KEY,
  type MutationClass,
  type MutationStats,
} from "./dommutation.js";
import { reportCurrentTarget } from "./panel.js";
import type { PageContent } from "./tools/result.js";

export class StaleRefError extends Error {
  constructor(ref: string) {
    super(`stale_ref: ${ref} — ${STALE_REF_REMEDY}`);
    this.name = "StaleRefError";
  }
}

/** Default wall-clock ceiling for a page-side eval (Playwright has none). */
export const EVAL_TIMEOUT_MS = 5000;
/** Ceiling for pulling a tab to the foreground before a capture. */
export const BRING_TO_FRONT_TIMEOUT_MS = 3000;
/** Ceiling for reading the page's own viewport back after a resize. */
export const APPLIED_VIEWPORT_TIMEOUT_MS = 3000;
/** Ceiling for asking a page whether it painted anything. */
export const PAGE_CONTENT_TIMEOUT_MS = 3000;

/** One recorded response, reduced to what a failure diagnosis needs. */
export interface NetworkSummary {
  id: string;
  method: string;
  url: string;
  status: number;
  content_type: string | null;
}

/** The viewport the page reports for itself, in CSS pixels. */
export interface AppliedViewport {
  width: number;
  height: number;
  deviceScaleFactor: number;
}

/**
 * Race a promise against a timer. Playwright's `page.evaluate` has no timeout,
 * so a pathological page-side computation would hang the whole (serialized)
 * session; this rejects with `message` after `timeoutMs`. The loser is left to
 * settle on its own — we cannot cancel an in-flight evaluate — but the caller is
 * unblocked. The timer is always cleared so the process can exit cleanly.
 */
export function raceWithTimeout<T>(
  promise: Promise<T>,
  timeoutMs: number,
  message: string,
): Promise<T> {
  let timer: ReturnType<typeof setTimeout>;
  const timeout = new Promise<never>((_, reject) => {
    timer = setTimeout(() => reject(new Error(message)), timeoutMs);
  });
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
}

export interface SnapshotResult {
  text: string;
  snapshot_id: number;
  refs: string[];
  truncated: boolean;
}

export interface SettleResult {
  urlChanged: boolean;
  domMutated: MutationClass;
}

export interface TabInfo {
  id: string;
  url: string;
  title: string;
  current: boolean;
}

/** Per-page state: the Page plus its console/network buffers. */
interface PageEntry {
  id: string;
  page: Page;
  console: ConsoleBuffer;
  network: NetworkBuffer;
}

export class Session {
  busy = false;
  onWorkspaceChange?: () => void;
  agentAdmission?: () => void;
  private screenshotValid = false;
  private readonly cdpSessions = new WeakMap<Page, Promise<CDPSession>>();
  viewportMode: "fit" | "fixed" = "fixed";
  private desiredViewport = { width: 1280, height: 800 };
  private pinnedViewport?: { width: number; height: number };
  workspaceStatus?: () => Record<string, unknown>;

  runAgentExclusive<T>(fn: () => Promise<T>): Promise<T> {
    try { this.agentAdmission?.(); } catch (err) { return Promise.reject(err); }
    return this.runExclusive(async () => {
      this.agentAdmission?.();
      await this.restorePinnedViewport();
      return fn();
    });
  }

  async currentCDP(): Promise<CDPSession> {
    const page = this.requirePage();
    let pending = this.cdpSessions.get(page);
    if (!pending) {
      pending = raceWithTimeout(page.context().newCDPSession(page), 5000, "CDP session timed out");
      this.cdpSessions.set(page, pending);
      pending.catch(() => this.cdpSessions.delete(page));
    }
    return pending;
  }

  async workspaceMetadata() {
    const id = this.currentTabId();
    const page = this.currentEntry()?.page;
    const tabs = await this.listTabs();
    let target: string | null = null;
    if (page) {
      try {
        const cdp = await this.currentCDP();
        const result = await raceWithTimeout(cdp.send("Target.getTargetInfo"), 2000, "target timeout");
        target = result.targetInfo.targetId;
      } catch { /* disconnected */ }
    }
    const { readEditors } = await import('./editors.js');
    const editors = page ? await readEditors(page).catch(() => []) : [];
    return { active_tab_id: id ?? null, target_id: target, editors,
      tabs: tabs.slice(0, 128).map(t => ({ ...t, url: t.url.slice(0, 1024), title: t.title.slice(0, 256) })),
      viewport: { mode: this.viewportMode, ...(page?.viewportSize() ?? this.desiredViewport) } };
  }

  invalidateScreenshot(): void { this.screenshotValid = false; }

  private readonly pages = new Map<string, PageEntry>();
  private currentId?: string;
  private tabSeq = 0;
  private readonly refs = new RefRegistry();
  /**
   * Mapping from the last screenshot's pixel space back to DOCUMENT (page) CSS
   * pixels: docCoord = offset + screenshotCoord / scale. `scale` is
   * screenshot/document (1 for the native-CSS captures we now emit); `offset`
   * is the document-space origin of the screenshot — the crop origin for a
   * region, (0,0) for a full-page shot, and the current scroll position for a
   * plain viewport shot. Document coordinates are converted to the viewport
   * (what page.mouse expects) at act time by {@link toViewportPoint}.
   */
  private lastScreenshot = { scale: 1, offsetX: 0, offsetY: 0 };
  /** Tail of the per-session serialization queue (see {@link runExclusive}). */
  private tail: Promise<unknown> = Promise.resolve();

  constructor(private readonly manager: BrowserManager) {}

  /**
   * Serialize an operation against every other operation on this session. The
   * MCP low-level Server dispatches request handlers without awaiting the
   * previous one, so two overlapping tool calls would otherwise race on shared
   * state (the page-side mutation-watch key, the last-screenshot mapping, page
   * acquisition). All tool executions funnel through here so they run strictly
   * one at a time. A rejection is propagated to its own caller but never
   * poisons the queue for the next operation.
   *
   * NOTE: not reentrant — browser_batch runs the inner tools by calling their
   * handlers directly (never through runExclusive) precisely because it is
   * itself already executing inside a runExclusive turn.
   */
  runExclusive<T>(fn: () => Promise<T>): Promise<T> {
    const execute = async () => {
      this.busy = true;
      this.onWorkspaceChange?.();
      try { return await fn(); } finally { this.busy = false; this.onWorkspaceChange?.(); }
    };
    const run = this.tail.then(execute, execute);
    this.tail = run.then(
      () => undefined,
      () => undefined,
    );
    return run;
  }

  hasPage(): boolean {
    const entry = this.currentEntry();
    return entry !== undefined && !entry.page.isClosed();
  }

  private currentEntry(): PageEntry | undefined {
    if (!this.currentId) return undefined;
    const entry = this.pages.get(this.currentId);
    if (!entry || entry.page.isClosed()) return undefined;
    return entry;
  }

  /**
   * Register a freshly acquired page: install its console/network listeners
   * (once) and its close handler, then store it. Returns the new entry.
   */
  private async trackPage(page: Page): Promise<PageEntry> {
    // Persistent CDP contexts have no default viewport override. Apply ours
    // before exposing the tab/target, including on first use and reconnect.
    const viewport = this.currentEntry()?.page.viewportSize() ?? this.desiredViewport;
    try {
      await raceWithTimeout(page.setViewportSize(viewport), 5000, "initial viewport timed out");
    } catch (error) {
      await page.close().catch(() => {});
      throw error;
    }
    this.desiredViewport = { ...viewport };
    this.tabSeq += 1;
    const id = `tab${this.tabSeq}`;
    const entry: PageEntry = {
      id,
      page,
      console: newConsoleBuffer(),
      network: new NetworkBuffer(),
    };

    page.on("console", (msg: ConsoleMessage) => {
      const loc = msg.location();
      const location = loc.url
        ? `${loc.url}:${loc.lineNumber}:${loc.columnNumber}`
        : "";
      // Cap per-entry size so a single huge console.log cannot be retained whole.
      const record: ConsoleEntry = cappedConsoleEntry(msg.type(), msg.text(), location);
      entry.console.push(record);
    });

    page.on("response", (response: Response) => {
      try {
        const req = response.request();
        entry.network.record({
          method: req.method(),
          url: response.url(),
          status: response.status(),
          resourceType: req.resourceType(),
          response,
        });
      } catch {
        /* response detached before we could read metadata — skip it */
      }
    });

    page.on("framenavigated", frame => {
      if (frame === page.mainFrame()) {
        if (this.currentId === id) this.notifyNavigation();
        else this.onWorkspaceChange?.();
      }
    });
    page.on("domcontentloaded", () => this.onWorkspaceChange?.());
    page.on("close", () => {
      this.pages.delete(id);
      this.onWorkspaceChange?.();
      if (this.currentId === id) {
        // Fall back to any surviving page; refs from the closed page are void.
        const next = this.pages.keys().next();
        this.currentId = next.done ? undefined : next.value;
        this.notifyNavigation();
        const nextEntry = this.currentId ? this.pages.get(this.currentId) : undefined;
        reportCurrentTarget(nextEntry ? nextEntry.page : null, this.currentId);
      }
    });

    this.pages.set(id, entry);
    reportCurrentTarget(entry.page, entry.id);
    return entry;
  }

  async ensurePage(): Promise<Page> {
    const entry = this.currentEntry();
    if (entry) return entry.page;
    const page = await this.manager.newPage();
    const fresh = await this.trackPage(page);
    this.currentId = fresh.id;
    await this.bringToFront();
    // A freshly acquired page (first use, or after a disconnect/crash) is a
    // blank document: refs handed out against a previous page must not resolve
    // against it. Drop them.
    this.notifyNavigation();
    return fresh.page;
  }

  private requireEntry(): PageEntry {
    const entry = this.currentEntry();
    if (!entry) throw new BrowserRestartingError();
    return entry;
  }

  private requirePage(): Page {
    return this.requireEntry().page;
  }

  get currentSnapshotId(): number {
    return this.refs.currentSnapshotId;
  }

  /** Refs are void after a committed navigation (or a current-page switch). */
  notifyNavigation(): void {
    this.refs.invalidateForNavigation();
    this.invalidateScreenshot();
    this.onWorkspaceChange?.();
  }

  /** Resolve a ref to a Locator, rejecting refs we never handed out. */
  resolveRef(ref: string): Locator {
    if (!isRefToken(ref)) throw new StaleRefError(ref);
    if (!this.refs.isKnown(ref)) throw new StaleRefError(ref);
    const page = this.requirePage();
    return page.locator(RefRegistry.selectorFor(ref));
  }

  /**
   * Take an aria snapshot (optionally scoped to a ref subtree), run the pure
   * post-processing pipeline, register the emitted refs, and stamp a snapshot_id.
   */
  async snapshot(
    opts: SnapshotRenderOptions & { scopeRef?: string } = {},
  ): Promise<SnapshotResult> {
    const page = this.requirePage();
    const { scopeRef, ...renderOpts } = opts;

    const scope: Locator = scopeRef
      ? this.resolveRef(scopeRef)
      : page.locator("body");

    let rawYaml: string;
    try {
      rawYaml = await scope.ariaSnapshot({ mode: "ai", boxes: true, timeout: 5000 });
    } catch (err) {
      if (scopeRef) throw new StaleRefError(scopeRef);
      throw err;
    }

    const rendered = renderSnapshot(rawYaml, renderOpts);
    const snapshot_id = this.refs.registerSnapshot(rendered.refs, Boolean(scopeRef));
    return {
      text: rendered.text,
      snapshot_id,
      refs: rendered.refs,
      truncated: rendered.truncated,
    };
  }

  // ---- reader / eval helpers ------------------------------------------------

  /**
   * Run a function in the current page's context, bounded by a wall-clock
   * timeout (page.evaluate itself has none). A page-side script that never
   * returns rejects with "eval timed out" instead of pinning the session.
   */
  async evaluate<T>(fn: (arg: string) => T, arg: string, timeoutMs = EVAL_TIMEOUT_MS): Promise<T> {
    const page = this.requirePage();
    return raceWithTimeout(page.evaluate(fn, arg), timeoutMs, "eval timed out");
  }

  currentUrl(): string {
    return this.requirePage().url();
  }

  /** Friendly SSRF block reason for a recently aborted host, if any. */
  ssrfBlockReason(host: string): string | undefined {
    return this.manager.ssrfBlockReason(host);
  }

  async currentTitle(): Promise<string> {
    return raceWithTimeout(this.requirePage().title(), 5000, "title timed out");
  }

  /**
   * Make the current tab the foreground one. Chrome paints — and screencasts —
   * only the foreground tab of a window, so a capture of a background tab
   * blocks until its own deadline and the live panel receives no frames at all.
   * Best effort: a target that refuses to come forward is not a reason to fail
   * the caller.
   */
  async bringToFront(): Promise<boolean> {
    const entry = this.currentEntry();
    if (!entry) return false;
    try {
      await raceWithTimeout(entry.page.bringToFront(), BRING_TO_FRONT_TIMEOUT_MS, "bringToFront timed out");
      // Chromium 149 can keep the emulated DOM viewport but reset screencast
      // frames to the native window's content size after navigation. Keep the
      // foreground window in sync too; changing metadata would only hide the
      // mismatch and make pointer/editor coordinates unsafe. Reapply on tab
      // activation because differently sized tabs can share one native window.
      const viewport = entry.page.viewportSize();
      if (viewport && this.currentEntry() === entry) {
        const cdp = await this.currentCDP();
        const { windowId } = await raceWithTimeout(cdp.send("Browser.getWindowForTarget"), BRING_TO_FRONT_TIMEOUT_MS, "window lookup timed out");
        if (this.currentEntry() === entry) {
          await raceWithTimeout(cdp.send("Browser.setContentsSize", { windowId, ...viewport }), BRING_TO_FRONT_TIMEOUT_MS, "window resize timed out");
        }
      }
      return true;
    } catch {
      return false;
    }
  }

  /**
   * Set the viewport and read back what the page actually got.
   *
   * `pinned` marks an agent-requested size: the panel's own resizes (a human
   * dragging the docked view) apply immediately but do not clear the pin, and
   * the pin is re-asserted before the agent's next tool call whenever no viewer
   * holds control — so a measurement the agent takes is the size it asked for.
   */
  async setViewport(width: number, height: number, pinned = false): Promise<AppliedViewport> {
    await raceWithTimeout(this.requirePage().setViewportSize({ width, height }), 5000, "viewport timed out; outcome unknown, do not replay");
    this.desiredViewport = { width, height };
    this.viewportMode = "fixed";
    if (pinned) this.pinnedViewport = { width, height };
    await this.bringToFront();
    this.notifyNavigation();
    return this.appliedViewport();
  }

  /** What the page reports as its own viewport, not what we asked for. */
  async appliedViewport(): Promise<AppliedViewport> {
    const page = this.requirePage();
    try {
      return await raceWithTimeout(
        page.evaluate(() => ({
          width: window.innerWidth,
          height: window.innerHeight,
          deviceScaleFactor: window.devicePixelRatio,
        })),
        APPLIED_VIEWPORT_TIMEOUT_MS,
        "viewport read-back timed out",
      );
    } catch {
      const size = page.viewportSize() ?? this.desiredViewport;
      return { ...size, deviceScaleFactor: 1 };
    }
  }

  /**
   * Re-apply the agent's pinned viewport when nothing else owns the browser.
   * Called on the agent's own admission path, so a panel resize that happened
   * between two agent calls does not silently change what the agent measures.
   */
  async restorePinnedViewport(): Promise<void> {
    const pin = this.pinnedViewport;
    const entry = this.currentEntry();
    if (!pin || !entry) return;
    const current = entry.page.viewportSize();
    if (current && current.width === pin.width && current.height === pin.height) return;
    try {
      await raceWithTimeout(entry.page.setViewportSize(pin), 5000, "viewport timed out");
      this.desiredViewport = { ...pin };
      this.viewportMode = "fixed";
      await this.bringToFront();
      this.onWorkspaceChange?.();
    } catch {
      /* no page yet, or a target that went away: the next call re-tries */
    }
  }

  /** Forget the agent's pin (a human took the wheel). */
  clearPinnedViewport(): void {
    this.pinnedViewport = undefined;
  }

  // ---- console / network buffers (current page) -----------------------------

  consoleEntries(): readonly ConsoleEntry[] {
    return this.requireEntry().console.all();
  }

  clearConsole(): void {
    this.requireEntry().console.clear();
  }

  networkEntries(): readonly NetworkEntry[] {
    return this.requireEntry().network.all();
  }

  /**
   * The newest recorded responses, with the two fields a failure usually turns
   * on: HTTP status and content-type. Reading them off the buffered handles
   * costs nothing and needs no page access, so a tool that failed can say what
   * the network actually returned instead of only what the page threw.
   */
  recentNetwork(limit = 3): NetworkSummary[] {
    let entries: readonly NetworkEntry[];
    try {
      entries = this.requireEntry().network.all();
    } catch {
      return [];
    }
    return entries.slice(-limit).map((entry) => {
      let contentType: string | null = null;
      try {
        contentType = entry.response.headers()["content-type"] ?? null;
      } catch {
        /* the handle can outlive its page */
      }
      return {
        id: entry.id,
        method: entry.method,
        url: entry.url.slice(0, 1024),
        status: entry.status,
        content_type: contentType,
      };
    });
  }

  /** What the page says about itself. `textChars` < 0 when it could not be read. */
  async pageContent(): Promise<PageContent> {
    try {
      return await this.evaluate(
        () => ({
          title: document.title || "",
          textChars: ((document.body && document.body.innerText) || "").replace(/\s+/g, "").length,
          visibleMedia: Array.from(document.querySelectorAll("canvas, img, svg, video, iframe"))
            .some(element => {
              const box = element.getBoundingClientRect();
              const style = getComputedStyle(element);
              return box.width > 0 && box.height > 0 && style.visibility !== "hidden" && style.display !== "none";
            }),
        }),
        "",
        PAGE_CONTENT_TIMEOUT_MS,
      );
    } catch {
      return { title: "", textChars: -1 };
    }
  }

  findNetworkEntry(id: string): NetworkEntry | undefined {
    return this.requireEntry().network.find(id);
  }

  clearNetwork(): void {
    this.requireEntry().network.clear();
  }

  // ---- tab management -------------------------------------------------------

  currentTabId(): string | undefined {
    return this.currentEntry()?.id;
  }

  async listTabs(): Promise<TabInfo[]> {
    return Promise.all([...this.pages.values()].filter(entry => !entry.page.isClosed()).map(async entry => {
      let title = "";
      try { title = await raceWithTimeout(entry.page.title(), 1000, "title timeout"); }
      catch { /* page mid-navigation; leave blank */ }
      return { id: entry.id, url: entry.page.url(), title, current: entry.id === this.currentId };
    }));
  }

  /** Open a new page, make it current, and invalidate refs from the old page. */
  async createTab(): Promise<string> {
    if (this.pages.size >= 128) {
      throw new Error("browser tab limit reached (128); close a tab before creating another");
    }
    const page = await this.manager.newPage();
    const entry = await this.trackPage(page);
    this.currentId = entry.id;
    await this.bringToFront();
    this.notifyNavigation();
    return entry.id;
  }

  /** Switch the current page. Refs from the previous page become void. */
  async selectTab(id: string): Promise<void> {
    const entry = this.pages.get(id);
    if (!entry || entry.page.isClosed()) {
      throw new Error(`no such tab: ${id}`);
    }
    if (id !== this.currentId) {
      this.currentId = id;
      await this.bringToFront();
      this.notifyNavigation();
      reportCurrentTarget(entry.page, id);
    }
  }

  /** Close a page. Rejects closing the last remaining page. */
  async closeTab(id: string): Promise<void> {
    const entry = this.pages.get(id);
    if (!entry || entry.page.isClosed()) {
      throw new Error(`no such tab: ${id}`);
    }
    const open = [...this.pages.values()].filter((e) => !e.page.isClosed());
    if (open.length <= 1) {
      throw new Error("cannot close the last tab");
    }
    // The page's own close handler removes it from the map and re-points current.
    await entry.page.close();
    await this.bringToFront();
  }

  // ---- screenshot coordinate mapping ---------------------------------------

  /**
   * Map a coordinate given in the last screenshot's pixel space to DOCUMENT
   * (page) CSS pixels. Pure; the viewport conversion is done separately in
   * {@link toViewportPoint} because it may need to scroll.
   */
  screenshotToPage(coordinate: [number, number]): [number, number] {
    const { scale, offsetX, offsetY } = this.lastScreenshot;
    const s = scale || 1;
    return [offsetX + coordinate[0] / s, offsetY + coordinate[1] / s];
  }

  /**
   * Convert a coordinate in the last screenshot's pixel space to the VIEWPORT
   * CSS pixels that page.mouse works in. The screenshot mapping is stored in
   * document space (so it stays correct for region crops, full-page shots, and
   * scrolled pages); here we scroll the mapped document point into view if it
   * is currently off-screen, then subtract the resulting scroll offset. This is
   * the robust path for every screenshot kind including full_page.
   */
  async toViewportPoint(coordinate: [number, number]): Promise<[number, number]> {
    if (!this.screenshotValid) throw new Error("stale_screenshot: capture a new browser_screenshot before coordinate actions");
    const [docX, docY] = this.screenshotToPage(coordinate);
    const page = this.requirePage();
    const scroll = await raceWithTimeout(page.evaluate(
      ([dx, dy]) => {
        const offView =
          dx < window.scrollX ||
          dx > window.scrollX + window.innerWidth ||
          dy < window.scrollY ||
          dy > window.scrollY + window.innerHeight;
        if (offView) {
          const targetX = Math.max(0, dx - window.innerWidth / 2);
          const targetY = Math.max(0, dy - window.innerHeight / 2);
          window.scrollTo(targetX, targetY);
        }
        return { x: window.scrollX, y: window.scrollY };
      },
      [docX, docY] as [number, number],
    ), 5000, "coordinate conversion timed out");
    return [docX - scroll.x, docY - scroll.y];
  }

  setLastScreenshot(mapping: { scale: number; offsetX: number; offsetY: number }): void {
    this.screenshotValid = true;
    this.lastScreenshot = {
      scale: mapping.scale > 0 ? mapping.scale : 1,
      offsetX: mapping.offsetX,
      offsetY: mapping.offsetY,
    };
  }

  /**
   * Install the mutation watcher BEFORE an action so synchronous DOM changes
   * made during the action are captured. Best-effort — a failure here just means
   * we fall back to a post-hoc URL comparison in {@link settle}.
   */
  async beginWatch(): Promise<void> {
    const page = this.requirePage();
    try {
      await raceWithTimeout(page.evaluate(installMutationWatcher, WATCH_KEY), 5000, "mutation watcher timed out");
    } catch {
      /* watcher is best-effort */
    }
  }

  /**
   * Tear down the mutation watcher without collecting stats. {@link settle} is
   * the normal teardown, but when an action throws before settling this is the
   * cleanup path so the MutationObserver and its `window[WATCH_KEY]`
   * accumulator do not leak. Best-effort.
   */
  async disposeWatch(): Promise<void> {
    const entry = this.currentEntry();
    if (!entry) return;
    const page = entry.page;
    try {
      await raceWithTimeout(page.evaluate((key) => {
        const w = window as unknown as Record<string, unknown>;
        const st = w[key] as { observer?: MutationObserver } | undefined;
        if (st && st.observer) {
          try {
            st.observer.disconnect();
          } catch {
            /* noop */
          }
        }
        delete w[key];
      }, WATCH_KEY), 5000, "mutation cleanup timed out");
    } catch {
      /* best-effort teardown */
    }
  }

  /**
   * After an action, wait for the DOM to go quiet, collect the accumulated
   * mutation stats, and classify what changed. Returns none/minor/major plus
   * whether the URL changed. Call {@link beginWatch} before the action.
   */
  async settle(urlBefore?: string): Promise<SettleResult> {
    const page = this.requirePage();
    const before = urlBefore ?? page.url();
    let stats: MutationStats;
    try {
      stats = await raceWithTimeout(page.evaluate(collectMutationStats, [WATCH_KEY, QUIET_MS, QUIET_CAP_MS] as [
        string,
        number,
        number,
      ]), 5000, "mutation settle timed out");
    } catch {
      // Page navigated/destroyed mid-settle — treat as a navigation-class change.
      stats = {
        addedNodes: 0,
        removedNodes: 0,
        attributeChanges: 0,
        characterDataChanges: 0,
        urlChanged: true,
      };
    }

    const urlChanged = stats.urlChanged || page.url() !== before;
    if (urlChanged) this.notifyNavigation();
    const domMutated = classifyMutations(stats);
    return { urlChanged, domMutated };
  }
}
