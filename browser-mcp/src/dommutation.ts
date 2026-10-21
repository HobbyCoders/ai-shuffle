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
export async function installMutationWatcher(input: string | { key: string; passive: boolean; sample?: boolean }): Promise<void> {
  const key = typeof input === "string" ? input : input.key;
  const passive = typeof input !== "string" && input.passive;
  const w = window as unknown as Record<string, unknown>;
  type TextHistory = WeakMap<Node, { samples: number; first: number; last: number }>;
  const prev = w[key] as { observer?: { disconnect(): void }; textHistory?: TextHistory;
    sampledDocuments?: WeakSet<Document>; tickerDocuments?: WeakSet<Document>; emptyDocuments?: WeakSet<Document>;
    observedShadows?: WeakMap<ShadowRoot, number>;
    passive: boolean; observedSince: number } | undefined;
  const installedAt = Date.now();
  // Top-document history includes its attached open shadow roots.
  const observedSince = prev?.passive && Number.isFinite(prev.observedSince) ? prev.observedSince : installedAt;
  let sampleSince = observedSince;
  let hasUncoveredFrames = false;
  const canSample = typeof input === "string" || input.sample !== false;
  prev?.observer?.disconnect();
  const textHistory: TextHistory = prev?.textHistory ?? new WeakMap();
  const sampledDocuments = prev?.sampledDocuments ?? new WeakSet<Document>();
  // Frame observers are disconnected while idle: ticker evidence is valid for
  // this action only. A previously proven ticker document must sample again.
  const frameTextTargets = new WeakMap<Node, { samples: number; last: number }>();
  const tickerDocuments = prev?.tickerDocuments ?? new WeakSet<Document>();
  const emptyDocuments = prev?.emptyDocuments ?? new WeakSet<Document>();
  const observedShadows = prev?.observedShadows ?? new WeakMap<ShadowRoot, number>();
  const newDocuments = new WeakSet<Document>();
  const samplingDocuments = new Map<Document, number>();
  const samplingTargets = new Set<Node>();
  const hasText = (root: Node): boolean => {
    const walker = (root.nodeType === 9 ? root as Document : root.ownerDocument!).createTreeWalker(root, 4 /* SHOW_TEXT */);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) if (node.textContent?.trim()) return true;
    if ("querySelectorAll" in root) for (const el of Array.from((root as Document).querySelectorAll("*"))) {
      if (el.shadowRoot && hasText(el.shadowRoot)) return true;
    }
    return false;
  };
  const cleanups: (() => void)[] = [];
  const activeCleanups: (() => void)[] = [];
  const roots = new WeakSet<Node>();
  const documents = new WeakSet<Document>();
  // Retain only attached top-document roots. A removed host must not leave an
  // observer closure in cleanups (which would retain every former shadow tree).
  const shadows = new Map<ShadowRoot, MutationObserver>();
  const pruneShadows = () => {
    for (const [root, observer] of shadows) if (!root.host.isConnected || root.ownerDocument !== document) {
      observer.disconnect(); shadows.delete(root); roots.delete(root);
    }
  };
  let pruneQueued = false;
  const queuePrune = () => {
    if (pruneQueued) return;
    pruneQueued = true;
    queueMicrotask(() => { pruneQueued = false; pruneShadows(); });
  };
  cleanups.push(() => { for (const observer of shadows.values()) observer.disconnect(); shadows.clear(); });
  const state = {
    addedNodes: 0, removedNodes: 0, attributeChanges: 0, characterDataChanges: 0,
    startUrl: location.href, lastMutation: Date.now(), pendingRequests: 0, pendingTimers: 0,
    deadline: 0, textSamples: 0, textTargets: new Set<Node>(), lastNonTextMutation: Date.now(), lastPendingWork: 0,
    textHistory, sampledDocuments, tickerDocuments, emptyDocuments, observedShadows,
    knownTextTargets: new WeakSet<Node>(), hasKnownText: false,
    observedSince, lastNonBaselineTextMutation: Date.now(),
    nonBaselineTextChanges: 0, sampling: true, passive,
    schedule: window.setTimeout.bind(window),
    observer: {
      pause: () => {
        state.passive = true;
        for (const cleanup of activeCleanups.splice(0).reverse()) cleanup();
        state.textTargets.clear(); samplingTargets.clear(); samplingDocuments.clear(); // no idle frame retention
        pruneShadows();
      },
      disconnect: () => { state.observer.pause(); for (const cleanup of cleanups.splice(0).reverse()) cleanup(); },
    },
  };
  const changed = () => { state.lastMutation = Date.now(); };
  // Replacing a ticker's text node is text churn, not structural ref churn.
  const count = (nodes: NodeList) => Array.from(nodes).reduce((n, node) =>
    n + (node.nodeType === 1 ? 1 + (node as Element).querySelectorAll("*").length : 0), 0);
  const baseline = (node: Node) => {
    if (!state.sampling) return; // eligibility is frozen before dispatch
    const frame = node.ownerDocument !== document;
    const history = textHistory.get(node), frameHistory = frameTextTargets.get(node);
    const proven = frame
      ? frameHistory && frameHistory.samples >= 2 && Date.now() - frameHistory.last <= 250
      : history && history.samples >= 2 && history.last - history.first >= 50 && Date.now() - history.last <= 250;
    if (proven) {
      state.knownTextTargets.add(node); state.hasKnownText = true;
      if (frame && node.ownerDocument) tickerDocuments.add(node.ownerDocument);
    }
  };
  const scan = (root: Node) => {
    baseline(root);
    if (root.nodeType === 1) inspect(root as Element);
    if ("querySelectorAll" in root) for (const el of Array.from((root as Document).querySelectorAll("*"))) inspect(el);
  };
  const observe = (root: Node, realm: Window) => {
    const topRoot = root === document || root.ownerDocument === document;
    if (state.passive && !topRoot) return;
    // Detached roots will be discovered if their host is later inserted.
    if (topRoot && root !== document && !(root as ShadowRoot).host.isConnected) return;
    if (roots.has(root)) return;
    roots.add(root);
    const doc = root.nodeType === 9 ? root as Document : root.ownerDocument!;
    if (root !== doc) {
      const shadow = root as ShadowRoot;
      const since = observedShadows.get(shadow) ?? Date.now();
      observedShadows.set(shadow, since);
      if (!state.passive && state.sampling && canSample && Date.now() - since < 100) {
        hasUncoveredFrames = true;
        sampleSince = Math.max(sampleSince, since);
      }
    }
    if (!topRoot && newDocuments.has(doc) && hasText(root)) {
      hasUncoveredFrames = true;
      sampleSince = Math.max(sampleSince, Date.now());
    }
    const Observer = (realm as unknown as { MutationObserver: typeof MutationObserver }).MutationObserver;
    const observer = new Observer(records => {
      queuePrune();
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
            samplingTargets.add(target);
            if (target.ownerDocument !== document) {
              const previous = frameTextTargets.get(target);
              frameTextTargets.set(target, { samples: previous && now - previous.last <= 250 ? previous.samples + 1 : 1, last: now });
            }
            baseline(target);
          }
          if (!state.passive && !state.sampling) {
            if (state.knownTextTargets.has(target)) {
              state.textSamples++; state.textTargets.add(target);
            } else {
              state.nonBaselineTextChanges++;
              state.lastNonBaselineTextMutation = Date.now();
              // A late-starting frame clock gets at most one conservative
              // action before the next call establishes its baseline.
              if (target.ownerDocument && target.ownerDocument !== document) sampledDocuments.delete(target.ownerDocument);
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
    if (topRoot && root !== document) shadows.set(root as ShadowRoot, observer);
    else (topRoot ? cleanups : activeCleanups).push(() => observer.disconnect());
    scan(root);
  };
  const watchShadows = (win: Window) => {
    const realm = win as unknown as typeof window;
    const attach = realm.Element.prototype.attachShadow;
    const attachWrapper: typeof attach = function (this: Element, init) {
      const root = attach.call(this, init);
      if (init.mode === "open" && this.isConnected) observe(root, win);
      return root;
    };
    realm.Element.prototype.attachShadow = attachWrapper;
    activeCleanups.push(() => {
      if (realm.Element.prototype.attachShadow === attachWrapper) realm.Element.prototype.attachShadow = attach;
    });
  };
  const watchWindow = (win: Window) => {
    // Reading document intentionally enforces same-origin access. A frame's
    // WindowProxy survives reload, so track documents rather than windows.
    const doc = win.document;
    if (documents.has(doc)) return;
    documents.add(doc);
    if (state.passive) { if (win === window) observe(doc, win); return; }
    if (state.sampling && canSample && win !== window) {
      if (!hasText(doc)) { sampledDocuments.add(doc); emptyDocuments.add(doc); tickerDocuments.delete(doc); }
      else {
        const wasEmpty = emptyDocuments.delete(doc), hadTicker = tickerDocuments.delete(doc);
        if (wasEmpty || hadTicker) sampledDocuments.delete(doc);
        if (!sampledDocuments.has(doc)) { newDocuments.add(doc); samplingDocuments.set(doc, Date.now()); }
      }
    }
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
    watchShadows(win);
    observe(doc, win);
  };
  function inspect(el: Element) {
    baseline(el);
    const win = el.ownerDocument.defaultView!;
    if (el.shadowRoot) observe(el.shadowRoot, win);
    if (state.passive) return;
    if (el.tagName === "IFRAME") {
      const frame = el as HTMLIFrameElement;
      const loaded = () => { try { if (frame.contentWindow) watchWindow(frame.contentWindow); } catch { /* cross-origin */ } };
      loaded(); frame.addEventListener("load", loaded);
      activeCleanups.push(() => frame.removeEventListener("load", loaded));
    }
  }
  w[key] = state;
  watchWindow(window);
  if (passive) { state.sampling = false; state.observer.pause(); return; }
  // Only new/non-baseline-churning, non-empty frame documents need a new
  // sample. Re-check discovery time if another frame arrives during the wait;
  // bound extension under continuous frame churn. Navigation never samples.
  const sampleLimit = Date.now() + 300;
  while (canSample && (!state.hasKnownText || hasUncoveredFrames)) {
    const remaining = Math.min(100 - (Date.now() - sampleSince), sampleLimit - Date.now());
    // A 100ms clock may mutate only once in the first window. Require a
    // second sample (bounded), rather than treating that one write as a ticker.
    const proving = Array.from(samplingTargets).some(node => !state.knownTextTargets.has(node));
    if (remaining <= 0 && (!proving || Date.now() >= sampleLimit)) break;
    await new Promise<void>(resolve => state.schedule(resolve, remaining > 0 ? remaining : Math.min(50, sampleLimit - Date.now())));
  }
  for (const [doc, since] of samplingDocuments) if (Date.now() - since >= 100) sampledDocuments.add(doc);
  samplingDocuments.clear(); samplingTargets.clear();
  state.sampling = false;
  state.addedNodes = state.removedNodes = state.attributeChanges = state.characterDataChanges = 0;
  state.lastMutation = state.lastNonTextMutation = state.lastNonBaselineTextMutation = Date.now();
}

export function collectMutationStats(args: [string, number, number]): Promise<MutationStats> {
  const [key, quietMs, capMs] = args;
  const w = window as unknown as Record<string, unknown>;
  const state = w[key] as (MutationStats & {
    startUrl: string; lastMutation: number; observer: { pause(): void };
    pendingRequests: number; pendingTimers: number;
    deadline: number; textSamples: number; textTargets: Set<Node>;
    lastNonTextMutation: number; lastPendingWork: number; lastNonBaselineTextMutation: number;
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
      const stableText = state.textSamples >= 2 && state.textTargets.size <= 3 &&
        now - Math.max(start, state.lastNonTextMutation, state.lastNonBaselineTextMutation, state.lastPendingWork) >= quietMs;
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
