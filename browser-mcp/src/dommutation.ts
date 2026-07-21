/**
 * DOM-mutation classification for browser_act's terse result.
 *
 * After an action we let the page settle: a MutationObserver accumulates
 * mutation stats and we resolve once the DOM has been quiet for ~300ms (capped
 * at 5s). The accumulated stats are then classified none/minor/major. The
 * classification is a pure function (unit-tested here); the observer itself is
 * a self-contained page-side script injected via page.evaluate.
 */

export interface MutationStats {
  addedNodes: number;
  removedNodes: number;
  attributeChanges: number;
  characterDataChanges: number;
  urlChanged: boolean;
}

export interface MutationThresholds {
  /** added+removed nodes at/above this count is a "major" structural change. */
  majorNodeThreshold: number;
}

export const DEFAULT_MUTATION_THRESHOLDS: MutationThresholds = {
  majorNodeThreshold: 12,
};

export const QUIET_MS = 300;
export const QUIET_CAP_MS = 5000;

export type MutationClass = "none" | "minor" | "major";

/**
 * Classify accumulated mutations.
 *   major → structural add/remove above threshold, or the URL changed
 *           (navigation via pushState/hash without a full load also counts)
 *   minor → any smaller node churn, or attribute/text-only tweaks
 *   none  → nothing observed
 */
export function classifyMutations(
  stats: MutationStats,
  thresholds: MutationThresholds = DEFAULT_MUTATION_THRESHOLDS,
): MutationClass {
  if (stats.urlChanged) return "major";

  const structural = stats.addedNodes + stats.removedNodes;
  if (structural >= thresholds.majorNodeThreshold) return "major";

  if (structural > 0) return "minor";
  if (stats.attributeChanges > 0 || stats.characterDataChanges > 0) return "minor";
  return "none";
}

/** Window key under which the page-side watcher stashes its accumulator. */
export const WATCH_KEY = "__browserMcpWatch";

/**
 * Page-side: install a MutationObserver that accumulates stats onto
 * `window[WATCH_KEY]`, recording the last-mutation timestamp. Installed BEFORE
 * the action so synchronous DOM changes made during the action are captured too.
 * Evaluated with (key). Serializes cleanly for page.evaluate (no Node closure).
 */
export function installMutationWatcher(key: string): void {
  const w = window as unknown as Record<string, unknown>;
  // Tear down a prior watcher if one is somehow still live.
  const prev = w[key] as { observer?: MutationObserver } | undefined;
  if (prev && prev.observer) {
    try {
      prev.observer.disconnect();
    } catch {
      /* noop */
    }
  }
  const state = {
    addedNodes: 0,
    removedNodes: 0,
    attributeChanges: 0,
    characterDataChanges: 0,
    startUrl: location.href,
    lastMutation: Date.now(),
    observer: undefined as MutationObserver | undefined,
  };
  const observer = new MutationObserver((records) => {
    for (const r of records) {
      if (r.type === "childList") {
        state.addedNodes += r.addedNodes.length;
        state.removedNodes += r.removedNodes.length;
      } else if (r.type === "attributes") {
        state.attributeChanges += 1;
      } else if (r.type === "characterData") {
        state.characterDataChanges += 1;
      }
    }
    state.lastMutation = Date.now();
  });
  observer.observe(document.documentElement, {
    childList: true,
    subtree: true,
    attributes: true,
    characterData: true,
  });
  state.observer = observer;
  w[key] = state;
}

/**
 * Page-side: wait until the DOM has been quiet for `quietMs` (no mutations since
 * the last one) or the `capMs` ceiling is reached, then disconnect and return
 * the accumulated stats. Takes a single tuple arg [key, quietMs, capMs] so it
 * serializes for page.evaluate (which passes exactly one argument).
 */
export function collectMutationStats(args: [string, number, number]): Promise<MutationStats> {
  const [key, quietMs, capMs] = args;
  const w = window as unknown as Record<string, unknown>;
  const state = w[key] as
    | {
        addedNodes: number;
        removedNodes: number;
        attributeChanges: number;
        characterDataChanges: number;
        startUrl: string;
        lastMutation: number;
        observer?: MutationObserver;
      }
    | undefined;

  if (!state) {
    // Watcher gone (e.g. document was replaced by a navigation): treat as a
    // navigation-class change.
    return Promise.resolve({
      addedNodes: 0,
      removedNodes: 0,
      attributeChanges: 0,
      characterDataChanges: 0,
      urlChanged: true,
    });
  }

  const start = Date.now();
  return new Promise<MutationStats>((resolve) => {
    const finish = () => {
      if (state.observer) {
        try {
          state.observer.disconnect();
        } catch {
          /* noop */
        }
      }
      delete w[key];
      resolve({
        addedNodes: state.addedNodes,
        removedNodes: state.removedNodes,
        attributeChanges: state.attributeChanges,
        characterDataChanges: state.characterDataChanges,
        urlChanged: location.href !== state.startUrl,
      });
    };
    const tick = () => {
      const now = Date.now();
      if (now - state.lastMutation >= quietMs || now - start >= capMs) {
        finish();
      } else {
        setTimeout(tick, 50);
      }
    };
    tick();
  });
}
