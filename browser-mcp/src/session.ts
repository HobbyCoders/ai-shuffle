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

import type { Page, Locator, ConsoleMessage, Response } from "playwright-core";
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

export class StaleRefError extends Error {
  constructor(ref: string) {
    super(`stale_ref: ${ref} — ${STALE_REF_REMEDY}`);
    this.name = "StaleRefError";
  }
}

/** Default wall-clock ceiling for a page-side eval (Playwright has none). */
export const EVAL_TIMEOUT_MS = 5000;

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
    const run = this.tail.then(fn, fn);
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
  private trackPage(page: Page): PageEntry {
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

    page.on("close", () => {
      this.pages.delete(id);
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
    const fresh = this.trackPage(page);
    this.currentId = fresh.id;
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
      rawYaml = await scope.ariaSnapshot({ mode: "ai", boxes: true });
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
    return this.requirePage().title();
  }

  async setViewport(width: number, height: number): Promise<void> {
    await this.requirePage().setViewportSize({ width, height });
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
    const out: TabInfo[] = [];
    for (const entry of this.pages.values()) {
      if (entry.page.isClosed()) continue;
      let title = "";
      try {
        title = await entry.page.title();
      } catch {
        /* page mid-navigation; leave blank */
      }
      out.push({
        id: entry.id,
        url: entry.page.url(),
        title,
        current: entry.id === this.currentId,
      });
    }
    return out;
  }

  /** Open a new page, make it current, and invalidate refs from the old page. */
  async createTab(): Promise<string> {
    const page = await this.manager.newPage();
    const entry = this.trackPage(page);
    this.currentId = entry.id;
    this.notifyNavigation();
    return entry.id;
  }

  /** Switch the current page. Refs from the previous page become void. */
  selectTab(id: string): void {
    const entry = this.pages.get(id);
    if (!entry || entry.page.isClosed()) {
      throw new Error(`no such tab: ${id}`);
    }
    if (id !== this.currentId) {
      this.currentId = id;
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
    const [docX, docY] = this.screenshotToPage(coordinate);
    const page = this.requirePage();
    const scroll = await page.evaluate(
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
    );
    return [docX - scroll.x, docY - scroll.y];
  }

  setLastScreenshot(mapping: { scale: number; offsetX: number; offsetY: number }): void {
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
      await page.evaluate(installMutationWatcher, WATCH_KEY);
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
      await page.evaluate((key) => {
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
      }, WATCH_KEY);
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
      stats = await page.evaluate(collectMutationStats, [WATCH_KEY, QUIET_MS, QUIET_CAP_MS] as [
        string,
        number,
        number,
      ]);
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
