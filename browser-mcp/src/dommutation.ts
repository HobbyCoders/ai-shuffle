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
      const state = (window as unknown as Record<string, unknown>)[key] as { pendingRequests: number; lastMutation: number; lastPendingWork: number } | undefined;
      if (state) { state.pendingRequests = count; state.lastMutation = state.lastPendingWork = Date.now(); }
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
  observationFailed?: boolean;
  pendingRequests?: number;
  pendingTimers?: number;
  textChurnIgnored?: boolean;
  nonBaselineTextChanges?: number;
}
export interface MutationThresholds { majorNodeThreshold: number }
export const DEFAULT_MUTATION_THRESHOLDS: MutationThresholds = { majorNodeThreshold: 12 };
export const QUIET_MS = 300;
// Long enough for action-triggered delayed renders, but bounded on live tickers.
export const QUIET_CAP_MS = 1500;
export type MutationClass = "none" | "minor" | "major";
export function classifyMutations(stats: MutationStats, thresholds = DEFAULT_MUTATION_THRESHOLDS): MutationClass {
  if (stats.urlChanged || stats.addedNodes + stats.removedNodes >= thresholds.majorNodeThreshold ||
      (stats.nonBaselineTextChanges ?? 0) >= thresholds.majorNodeThreshold) return "major";
  return stats.addedNodes + stats.removedNodes + stats.attributeChanges + stats.characterDataChanges > 0 ? "minor" : "none";
}
export const WATCH_KEY = "__browserMcpWatch";

/** Self-contained: Playwright serializes this function into the page. */
export async function installMutationWatcher(input: string | { key: string; passive: boolean }): Promise<void> {
  const key = typeof input === "string" ? input : input.key;
  const passive = typeof input !== "string" && input.passive;
  const w = window as unknown as Record<string, unknown>;
  type TextHistory = WeakMap<Node, { samples: number; first: number; last: number }>;
  const prev = w[key] as { observer?: { disconnect(): void }; textHistory?: TextHistory } | undefined;
  prev?.observer?.disconnect();
  const textHistory: TextHistory = prev?.textHistory ?? new WeakMap();
  const installedAt = Date.now();
  const cleanups: (() => void)[] = [];
  const activeCleanups: (() => void)[] = [];
  const roots = new WeakSet<Node>();
  const documents = new WeakSet<Document>();
  const state = {
    addedNodes: 0, removedNodes: 0, attributeChanges: 0, characterDataChanges: 0,
    startUrl: location.href, lastMutation: Date.now(), pendingRequests: 0, pendingTimers: 0,
    deadline: 0, textSamples: 0, textTargets: new Set<Node>(), lastNonTextMutation: Date.now(), lastPendingWork: 0,
    textHistory, knownTextTargets: new WeakSet<Node>(), hasKnownText: false,
    allTextTargetsKnown: true, nonBaselineTextChanges: 0, sampling: true, passive: false,
    schedule: window.setTimeout.bind(window),
    observer: {
      pause: () => { state.passive = true; for (const cleanup of activeCleanups.splice(0).reverse()) cleanup(); },
      disconnect: () => { state.observer.pause(); for (const cleanup of cleanups.splice(0).reverse()) cleanup(); },
    },
  };
  const changed = () => { state.lastMutation = Date.now(); };
  // Replacing a ticker's text node is text churn, not structural ref churn.
  const count = (nodes: NodeList) => Array.from(nodes).reduce((n, node) =>
    n + (node.nodeType === 1 ? 1 + (node as Element).querySelectorAll("*").length : 0), 0);
  const baseline = (node: Node) => {
    const history = textHistory.get(node);
    if (history && history.samples >= 2 && history.last - history.first >= 50 && installedAt - history.last <= 250) {
      state.knownTextTargets.add(node); state.hasKnownText = true;
    }
  };
  const scan = (root: Node) => {
    baseline(root);
    if (root.nodeType === 1) inspect(root as Element);
    if ("querySelectorAll" in root) for (const el of Array.from((root as Document).querySelectorAll("*"))) inspect(el);
  };
  const observe = (root: Node, realm: Window) => {
    if (roots.has(root)) return;
    roots.add(root);
    const Observer = (realm as unknown as { MutationObserver: typeof MutationObserver }).MutationObserver;
    const observer = new Observer(records => {
      for (const r of records) {
        const textOnly = r.type === "characterData" || (r.type === "childList" &&
          [...Array.from(r.addedNodes), ...Array.from(r.removedNodes)].every(node => node.nodeType === 3));
        if (textOnly) {
          const target = r.target.nodeType === 3 ? r.target.parentNode ?? r.target : r.target;
          const now = Date.now(), old = textHistory.get(target);
          textHistory.set(target, old && now - old.last <= 250
            ? { samples: old.samples + 1, first: old.first, last: now }
            : { samples: 1, first: now, last: now });
          if (state.sampling && !state.passive) {
            state.knownTextTargets.add(target); state.hasKnownText = true;
          }
          if (!state.passive && !state.sampling) {
            state.textSamples++; state.textTargets.add(target);
            if (!state.knownTextTargets.has(target)) {
              state.allTextTargetsKnown = false; state.nonBaselineTextChanges++;
            }
          }
        } else state.lastNonTextMutation = Date.now();
        // Keep only weak text history between calls, not action counters or
        // timer/shadow hooks. A later action must prove its own baseline.
        if (state.passive) {
          if (r.type === "childList") for (const node of Array.from(r.addedNodes)) scan(node);
          continue;
        }
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
    if (state.passive) { observe(doc, win); return; }
    const realm = win as unknown as typeof window;
    // Timers scheduled by the action matter too (e.g. debounce/700ms render).
    // Existing intervals are deliberately not tracked: clocks must not hold refs hostage.
    type Timer = (handler: TimerHandler, delay?: number, ...args: unknown[]) => number;
    const set = realm.setTimeout as unknown as Timer;
    const clear = realm.clearTimeout as unknown as (id?: number) => void;
    const timers = new Set<number>();
    const setWrapper: Timer = (handler, delay, ...args) => {
      // A timer that cannot fire within this observation budget (e.g. toast
      // dismissal) must not hold every unrelated action to the cap.
      const remaining = state.deadline ? state.deadline - Date.now() : 1500;
      if (typeof handler !== "function" || Number(delay ?? 0) >= remaining) return set.call(realm, handler, delay, ...args);
      state.pendingTimers++;
      const id = set.call(realm, () => {
        if (timers.delete(id)) state.pendingTimers--;
        state.lastPendingWork = Date.now();
        changed(); handler.apply(realm, args);
      }, delay);
      timers.add(id); return id;
    };
    const clearWrapper = (id?: number) => {
      if (id !== undefined && timers.delete(id)) { state.pendingTimers--; state.lastPendingWork = Date.now(); changed(); }
      clear.call(realm, id);
    };
    realm.setTimeout = setWrapper as typeof realm.setTimeout; realm.clearTimeout = clearWrapper as typeof realm.clearTimeout;
    activeCleanups.push(() => {
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
    activeCleanups.push(() => { if (realm.Element.prototype.attachShadow === attachWrapper) realm.Element.prototype.attachShadow = attach; });
    observe(doc, win);
  };
  function inspect(el: Element) {
    baseline(el);
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
  if (passive) { state.sampling = false; state.observer.pause(); return; }
  // A fresh page/ticker has no history yet. Briefly sample BEFORE dispatch;
  // targets first mutated or created by the action can never join this set.
  if (!state.hasKnownText) await new Promise<void>(resolve => state.schedule(resolve, 100));
  state.sampling = false;
  state.addedNodes = state.removedNodes = state.attributeChanges = state.characterDataChanges = 0;
  state.lastMutation = state.lastNonTextMutation = Date.now();
}

export function collectMutationStats(args: [string, number, number]): Promise<MutationStats> {
  const [key, quietMs, capMs] = args;
  const w = window as unknown as Record<string, unknown>;
  const state = w[key] as (MutationStats & {
    startUrl: string; lastMutation: number; observer: { pause(): void };
    pendingRequests: number; pendingTimers: number;
    deadline: number; textSamples: number; textTargets: Set<Node>; allTextTargetsKnown: boolean;
    lastNonTextMutation: number; lastPendingWork: number;
    schedule: typeof window.setTimeout;
  }) | undefined;
  // Missing watcher can mean same-URL document replacement. Refresh the tree,
  // but leave URL-change detection to the actual URL comparison in Session.
  if (!state) return Promise.resolve({ addedNodes: 12, removedNodes: 0, attributeChanges: 0, characterDataChanges: 0, urlChanged: false, observationFailed: true });
  const start = Date.now();
  // Quiet time starts at the END of dispatch, not watcher installation.
  state.lastMutation = start;
  state.deadline = start + capMs;
  return new Promise(resolve => {
    const tick = () => {
      const now = Date.now();
      const capped = now - start >= capMs;
      // Only containers observed churning BEFORE dispatch can be ignored.
      // A counter/typewriter/chat stream started by the action is real work.
      const stableText = state.allTextTargetsKnown && state.textSamples >= 2 && state.textTargets.size <= 3 &&
        now - Math.max(start, state.lastNonTextMutation, state.lastPendingWork) >= quietMs;
      if (capped || (!state.pendingRequests && !state.pendingTimers && (now - state.lastMutation >= quietMs || stableText))) {
        state.observer.pause();
        resolve({
          addedNodes: state.addedNodes, removedNodes: state.removedNodes,
          attributeChanges: state.attributeChanges, characterDataChanges: state.characterDataChanges,
          urlChanged: location.href !== state.startUrl, settleCapped: capped,
          pendingRequests: state.pendingRequests, pendingTimers: state.pendingTimers,
          nonBaselineTextChanges: state.nonBaselineTextChanges,
          textChurnIgnored: !capped && stableText && now - state.lastMutation < quietMs,
        });
      } else state.schedule(tick, 25);
    };
    tick();
  });
}
