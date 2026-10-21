/** Bounded, action-scoped DOM/network/timer observation, including open roots. */
import type { Page, Request } from "playwright-core";

// Use Playwright's request lifecycle, not fetch monkey-patching: this also sees
// cached fetch functions, streaming bodies, images and same-origin frame work.
const requests = new WeakMap<Page, { sync(): Promise<void>; dispose(): void }>();
export function endRequestWatch(page: Page): void {
  requests.get(page)?.dispose();
  requests.delete(page);
}
export function beginRequestWatch(page: Page): void {
  endRequestWatch(page);
  const pending = new Set<Request>();
  let active = true;
  const sync = async () => {
    if (!active) return;
    await page.evaluate(({ key, count }) => {
      const state = (window as unknown as Record<string, unknown>)[key] as { pendingRequests: number; lastMutation: number } | undefined;
      if (state) { state.pendingRequests = count; state.lastMutation = Date.now(); }
    }, { key: WATCH_KEY, count: pending.size }).catch(() => {}); // navigation can destroy the watcher
  };
  const started = (request: Request) => { pending.add(request); void sync(); };
  const finished = (request: Request) => { if (pending.delete(request)) void sync(); };
  page.on("request", started);
  page.on("requestfinished", finished);
  page.on("requestfailed", finished);
  requests.set(page, { sync, dispose: () => {
    active = false;
    page.off("request", started); page.off("requestfinished", finished); page.off("requestfailed", finished);
  } });
}
export async function syncRequestWatch(page: Page): Promise<void> { await requests.get(page)?.sync(); }

export interface MutationStats {
  addedNodes: number;
  removedNodes: number;
  attributeChanges: number;
  characterDataChanges: number;
  urlChanged: boolean;
  settleCapped?: boolean;
  pendingRequests?: number;
  pendingTimers?: number;
}
export interface MutationThresholds { majorNodeThreshold: number }
export const DEFAULT_MUTATION_THRESHOLDS: MutationThresholds = { majorNodeThreshold: 12 };
export const QUIET_MS = 300;
// Long enough for action-triggered delayed renders, but bounded on live tickers.
export const QUIET_CAP_MS = 1500;
export type MutationClass = "none" | "minor" | "major";
export function classifyMutations(stats: MutationStats, thresholds = DEFAULT_MUTATION_THRESHOLDS): MutationClass {
  if (stats.urlChanged || stats.addedNodes + stats.removedNodes >= thresholds.majorNodeThreshold) return "major";
  return stats.addedNodes + stats.removedNodes + stats.attributeChanges + stats.characterDataChanges > 0 ? "minor" : "none";
}
export const WATCH_KEY = "__browserMcpWatch";

/** Self-contained: Playwright serializes this function into the page. */
export function installMutationWatcher(key: string): void {
  const w = window as unknown as Record<string, unknown>;
  const prev = w[key] as { observer?: { disconnect(): void } } | undefined;
  prev?.observer?.disconnect();
  const cleanups: (() => void)[] = [];
  const roots = new WeakSet<Node>();
  const documents = new WeakSet<Document>();
  const state = {
    addedNodes: 0, removedNodes: 0, attributeChanges: 0, characterDataChanges: 0,
    startUrl: location.href, lastMutation: Date.now(), pendingRequests: 0, pendingTimers: 0,
    schedule: window.setTimeout.bind(window),
    observer: { disconnect: () => { for (const cleanup of cleanups.splice(0).reverse()) cleanup(); } },
  };
  const changed = () => { state.lastMutation = Date.now(); };
  // Replacing a ticker's text node is text churn, not structural ref churn.
  const count = (nodes: NodeList) => Array.from(nodes).reduce((n, node) =>
    n + (node.nodeType === 1 ? 1 + (node as Element).querySelectorAll("*").length : 0), 0);
  const scan = (root: Node) => {
    if (root.nodeType === 1) inspect(root as Element);
    if ("querySelectorAll" in root) for (const el of Array.from((root as Document).querySelectorAll("*"))) inspect(el);
  };
  const observe = (root: Node, realm: Window) => {
    if (roots.has(root)) return;
    roots.add(root);
    const Observer = (realm as unknown as { MutationObserver: typeof MutationObserver }).MutationObserver;
    const observer = new Observer(records => {
      for (const r of records) {
        if (r.type === "childList") {
          state.addedNodes += count(r.addedNodes); state.removedNodes += count(r.removedNodes);
          if ([...Array.from(r.addedNodes), ...Array.from(r.removedNodes)].some(node => node.nodeType === 3)) state.characterDataChanges++;
          for (const node of Array.from(r.addedNodes)) scan(node);
        } else if (r.type === "attributes") state.attributeChanges++;
        else state.characterDataChanges++;
      }
      changed();
    });
    observer.observe(root, { childList: true, subtree: true, attributes: true, characterData: true });
    cleanups.push(() => observer.disconnect());
    scan(root);
  };
  const watchWindow = (win: Window) => {
    // Reading document intentionally enforces same-origin access. A frame's
    // WindowProxy survives reload, so track documents rather than windows.
    const doc = win.document;
    if (documents.has(doc)) return;
    documents.add(doc);
    const realm = win as unknown as typeof window;
    // Timers scheduled by the action matter too (e.g. debounce/700ms render).
    // Existing intervals are deliberately not tracked: clocks must not hold refs hostage.
    type Timer = (handler: TimerHandler, delay?: number, ...args: unknown[]) => number;
    const set = realm.setTimeout as unknown as Timer;
    const clear = realm.clearTimeout as unknown as (id?: number) => void;
    const timers = new Set<number>();
    const setWrapper: Timer = (handler, delay, ...args) => {
      if (typeof handler !== "function") return set.call(realm, handler, delay, ...args);
      state.pendingTimers++;
      const id = set.call(realm, () => {
        if (timers.delete(id)) state.pendingTimers--;
        changed(); handler.apply(realm, args);
      }, delay);
      timers.add(id); return id;
    };
    const clearWrapper = (id?: number) => {
      if (id !== undefined && timers.delete(id)) { state.pendingTimers--; changed(); }
      clear.call(realm, id);
    };
    realm.setTimeout = setWrapper as typeof realm.setTimeout; realm.clearTimeout = clearWrapper as typeof realm.clearTimeout;
    cleanups.push(() => {
      if (realm.setTimeout === setWrapper) realm.setTimeout = set as typeof realm.setTimeout;
      if (realm.clearTimeout === clearWrapper) realm.clearTimeout = clear as typeof realm.clearTimeout;
    });
    const attach = realm.Element.prototype.attachShadow;
    const attachWrapper: typeof attach = function (this: Element, init) {
      const root = attach.call(this, init);
      if (init.mode === "open") observe(root, win);
      return root;
    };
    realm.Element.prototype.attachShadow = attachWrapper;
    cleanups.push(() => { if (realm.Element.prototype.attachShadow === attachWrapper) realm.Element.prototype.attachShadow = attach; });
    observe(doc, win);
  };
  function inspect(el: Element) {
    const win = el.ownerDocument.defaultView!;
    if (el.shadowRoot) observe(el.shadowRoot, win);
    if (el.tagName === "IFRAME") {
      const frame = el as HTMLIFrameElement;
      const loaded = () => { try { if (frame.contentWindow) watchWindow(frame.contentWindow); } catch { /* cross-origin */ } };
      loaded(); frame.addEventListener("load", loaded);
      cleanups.push(() => frame.removeEventListener("load", loaded));
    }
  }
  w[key] = state;
  watchWindow(window);
}

export function collectMutationStats(args: [string, number, number]): Promise<MutationStats> {
  const [key, quietMs, capMs] = args;
  const w = window as unknown as Record<string, unknown>;
  const state = w[key] as (MutationStats & {
    startUrl: string; lastMutation: number; observer: { disconnect(): void };
    pendingRequests: number; pendingTimers: number;
    schedule: typeof window.setTimeout;
  }) | undefined;
  // Missing watcher can mean same-URL document replacement. Refresh the tree,
  // but leave URL-change detection to the actual URL comparison in Session.
  if (!state) return Promise.resolve({ addedNodes: 12, removedNodes: 0, attributeChanges: 0, characterDataChanges: 0, urlChanged: false });
  const start = Date.now();
  // Quiet time starts at the END of dispatch, not watcher installation.
  state.lastMutation = start;
  return new Promise(resolve => {
    const tick = () => {
      const now = Date.now();
      const capped = now - start >= capMs;
      if (capped || (!state.pendingRequests && !state.pendingTimers && now - state.lastMutation >= quietMs)) {
        state.observer.disconnect();
        delete w[key];
        resolve({
          addedNodes: state.addedNodes, removedNodes: state.removedNodes,
          attributeChanges: state.attributeChanges, characterDataChanges: state.characterDataChanges,
          urlChanged: location.href !== state.startUrl, settleCapped: capped,
          pendingRequests: state.pendingRequests, pendingTimers: state.pendingTimers,
        });
      } else state.schedule(tick, 25);
    };
    tick();
  });
}
