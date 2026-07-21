/**
 * Session state: the single Page this process drives, its ref registry, and the
 * scale factor of the most recent screenshot (so coordinate-based acts can map
 * screenshot pixels back to page pixels). Wraps the browser-facing calls; the
 * pure snapshot / ref / mutation logic lives in their own modules.
 */

import type { Page, Locator } from "playwright-core";
import { BrowserManager, BrowserRestartingError } from "./browser.js";
import { RefRegistry, STALE_REF_REMEDY, isRefToken } from "./refs.js";
import {
  renderSnapshot,
  type SnapshotRenderOptions,
} from "./snapshot.js";
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

export class StaleRefError extends Error {
  constructor(ref: string) {
    super(`stale_ref: ${ref} — ${STALE_REF_REMEDY}`);
    this.name = "StaleRefError";
  }
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

export class Session {
  private page?: Page;
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
    return this.page !== undefined && !this.page.isClosed();
  }

  async ensurePage(): Promise<Page> {
    if (this.page && !this.page.isClosed()) return this.page;
    this.page = await this.manager.newPage();
    // A freshly acquired page (first use, or after a disconnect/crash) is a
    // blank document: refs handed out against the previous page must not
    // resolve against it. Drop them.
    this.notifyNavigation();
    return this.page;
  }

  private requirePage(): Page {
    if (!this.page || this.page.isClosed()) {
      throw new BrowserRestartingError();
    }
    return this.page;
  }

  get currentSnapshotId(): number {
    return this.refs.currentSnapshotId;
  }

  /** Refs are void after a committed navigation. */
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
    const page = this.page;
    if (!page || page.isClosed()) return;
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
