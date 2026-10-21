/** Failure-isolated, visible composed-document scopes for text observations. */
import type { Frame, Locator } from "playwright-core";
import { raceWithTimeout, type Session } from "../session.js";

export interface ObservationRoot {
  frame: Frame;
  scope?: Locator;
  owner?: Frame;
  primary: boolean;
}
export async function observationRoots(session: Session, scopeRef?: string): Promise<ObservationRoot[]> {
  const page = await session.ensurePage();
  const scope = scopeRef ? await session.resolveRef(scopeRef) : undefined;
  let owner = page.mainFrame();
  if (scope) {
    const handle = await scope.elementHandle({ timeout: 200 });
    if (!handle) throw new Error(`stale_ref: ${scopeRef}`);
    try { owner = (await handle.ownerFrame())!; }
    finally { await handle.dispose(); }
  }
  return [{ frame: owner, scope, owner, primary: true }, ...page.frames()
    .filter(frame => frame !== owner).map(frame => ({ frame, scope, owner, primary: false }))];
}

// A timed-out Playwright evaluation cannot be cancelled. Keep its slot occupied
// until it really settles, and reuse it across polls instead of piling up work.
const pending = new WeakMap<Session, Map<Frame, { key: string; promise: Promise<unknown> }>>();
export function observationTask<T>(session: Session, root: ObservationRoot, key: string, run: () => Promise<T>): Promise<T> | undefined {
  let tasks = pending.get(session);
  if (!tasks) pending.set(session, tasks = new Map());
  const existing = tasks.get(root.frame);
  if (existing) return existing.key === key ? existing.promise as Promise<T> : undefined;
  if (!root.primary && tasks.size >= 7) return undefined;
  const promise = Promise.resolve().then(run);
  tasks.set(root.frame, { key, promise });
  // Register both handlers: a rejected late evaluation must not be unhandled.
  const cleanup = () => { if (tasks!.get(root.frame)?.promise === promise) tasks!.delete(root.frame); };
  void promise.then(cleanup, cleanup);
  return promise;
}

export async function boundedObservation<T>(task: Promise<T> | undefined, timeout: number): Promise<T> {
  if (!task) throw new Error("observation busy");
  return raceWithTimeout(task, Math.max(1, timeout), "frame observation timed out");
}

/** Called inside each frame's bounded task: a bad child never rejects siblings. */
export async function includeObservationRoot(root: ObservationRoot): Promise<boolean> {
  if (root.frame.isDetached()) return false;
  if (root.primary && !root.frame.parentFrame()) return true;
  let frame = root.frame;
  let withinScope = !root.scope || root.primary;
  while (frame.parentFrame()) {
    const element = await frame.frameElement();
    try {
      const visible = await element.evaluate(el => {
        if (!(el instanceof Element)) return false;
        const rect = el.getBoundingClientRect();
        const ownStyle = getComputedStyle(el);
        const px = (value: string) => parseFloat(value) || 0;
        const scaleX = rect.width / ((el as HTMLElement).offsetWidth || rect.width || 1);
        const scaleY = rect.height / ((el as HTMLElement).offsetHeight || rect.height || 1);
        // clientWidth/Height include padding, which is not the iframe viewport.
        const width = (el.clientWidth - px(ownStyle.paddingLeft) - px(ownStyle.paddingRight)) * scaleX;
        const height = (el.clientHeight - px(ownStyle.paddingTop) - px(ownStyle.paddingBottom)) * scaleY;
        if (width <= 2 || height <= 2) return false;
        const left = Math.max(0, rect.left + (el.clientLeft + px(ownStyle.paddingLeft)) * scaleX);
        const top = Math.max(0, rect.top + (el.clientTop + px(ownStyle.paddingTop)) * scaleY);
        const right = Math.min(innerWidth, rect.left + (el.clientLeft + px(ownStyle.paddingLeft)) * scaleX + width);
        const bottom = Math.min(innerHeight, rect.top + (el.clientTop + px(ownStyle.paddingTop)) * scaleY + height);
        if (right <= left || bottom <= top) return false;
        let node: Element | null = el;
        let opacity = 1;
        while (node) {
          const style = getComputedStyle(node);
          opacity *= Number(style.opacity);
          if (style.display === "none" || style.visibility !== "visible" ||
              style.contentVisibility === "hidden" || opacity <= 0.01) return false;
          node = node.parentElement ?? (node.getRootNode() as ShadowRoot).host ?? null;
        }
        // Browser hit testing accounts for overflow, clip/clip-path, stacking
        // behind the page, and clipping ancestors (also inside open shadows).
        for (const x of [0.2, 0.5, 0.8]) for (const y of [0.2, 0.5, 0.8]) {
          let hit = document.elementFromPoint(left + (right - left) * x, top + (bottom - top) * y);
          while (hit?.shadowRoot) {
            const inner = hit.shadowRoot.elementFromPoint(left + (right - left) * x, top + (bottom - top) * y);
            if (!inner || inner === hit) break;
            hit = inner;
          }
          if (hit === el) return true;
        }
        return false;
      });
      if (!visible) return false;
      if (root.scope && frame.parentFrame() === root.owner) {
        withinScope = await root.scope.evaluate((scope, candidate) => {
          let node: Node | null = candidate;
          while (node) {
            if (node === scope) return true;
            node = node.parentNode ?? (node.getRootNode() as ShadowRoot).host ?? null;
          }
          return false;
        }, element, { timeout: 150 });
      }
    } finally { await element.dispose(); }
    frame = frame.parentFrame()!;
  }
  return withinScope;
}
