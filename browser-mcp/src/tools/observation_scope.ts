/** Failure-isolated, visible composed-document scopes for text observations. */
import type { Frame, Locator, Page } from "playwright-core";
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
const pending = new WeakMap<Page, Map<Frame, { key: symbol; promise: Promise<unknown> }>>();
export function observationTask<T>(root: ObservationRoot, key: symbol, run: () => Promise<T>): Promise<T> | undefined {
  const page = root.frame.page();
  let tasks = pending.get(page);
  if (!tasks) pending.set(page, tasks = new Map());
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

/** Wait for actual settlement, but never launch late work after our own budget. */
export async function observationReady(root: ObservationRoot, deadline: number): Promise<void> {
  while (true) {
    const existing = pending.get(root.frame.page())?.get(root.frame);
    if (!existing) return;
    await boundedObservation(existing.promise.catch(() => undefined), deadline - Date.now());
    if (Date.now() >= deadline) throw new Error("frame observation timed out");
  }
}

export async function boundedObservation<T>(task: Promise<T> | undefined, timeout: number): Promise<T> {
  if (!task) throw new Error("observation busy");
  return raceWithTimeout(task, Math.max(1, timeout), "frame observation timed out");
}

/** Called inside each frame's bounded task: a bad child never rejects siblings. */
export async function observationRootStatus(root: ObservationRoot, mode: "reader" | "visible" | "attached" = "reader"): Promise<"included" | "hidden" | "excluded"> {
  if (root.frame.isDetached()) return "excluded";
  if ((mode === "attached" && !root.scope) || (root.primary && !root.frame.parentFrame())) return "included";
  let frame = root.frame;
  let withinScope = !root.scope || root.primary;
  let hidden = false;
  while (frame.parentFrame()) {
    const element = await frame.frameElement();
    try {
      const visible = mode === "attached" || (mode === "visible" ? await element.isVisible() : await element.evaluate(async el => {
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
        let left = rect.left + (el.clientLeft + px(ownStyle.paddingLeft)) * scaleX;
        let top = rect.top + (el.clientTop + px(ownStyle.paddingTop)) * scaleY;
        let right = left + width, bottom = top + height;
        const intersects = () => right > left && bottom > top;
        const clip = (x1: number, y1: number, x2: number, y2: number) => {
          left = Math.max(left, x1); top = Math.max(top, y1);
          right = Math.min(right, x2); bottom = Math.min(bottom, y2);
        };
        const length = (value: string, size: number) => value.endsWith("%") ? px(value) * size / 100 : px(value);
        // Slotted light-DOM children inherit clipping from the slot's shadow
        // ancestors before reaching the host, not from their light-DOM parent.
        const parent = (node: Element): Element | null => node.assignedSlot ?? node.parentElement ?? (node.getRootNode() as ShadowRoot).host ?? null;
        const containingBlock = (node: Element, fixed: boolean): Element | null => {
          for (let ancestor = parent(node); ancestor; ancestor = parent(ancestor)) {
            const style = getComputedStyle(ancestor);
            const effect = ["transform", "filter", "perspective", "backdrop-filter", "-webkit-backdrop-filter", "translate", "scale", "rotate"]
              .some(property => {
                const value = style.getPropertyValue(property);
                return value !== "" && value !== "none";
              });
            // Non-default will-change may establish a containing block even
            // before its effect is applied. Keep clipping for unfamiliar hints
            // too: uncertain ancestors must retain the base fail-closed behavior.
            if (style.display === "contents") continue;
            if ((!fixed && style.position !== "static") || effect || (style.willChange && style.willChange !== "auto") ||
                style.transformStyle === "preserve-3d" || style.contentVisibility === "auto" ||
                /(?:layout|paint|strict|content)/.test(style.contain)) return ancestor;
          }
          return fixed ? null : document.documentElement;
        };
        // Layout sees clipping inside closed shadow roots even when assignedSlot
        // is null. Check each scroll-reachable segment separately: expanding a
        // scrollport root admits its reachable content without ignoring closed
        // shadow clips inside it or around the scrollport itself. Known
        // fail-closed limitation: a scrollport inside a closed shadow root
        // cannot be traversed, so its off-scrollport content remains hidden.
        const segments: { target: Element; root: Element | Document }[] = [];
        let layoutTarget = el;
        let overflowRoot: Element | null = null;
        let clipOverflow = true;
        let node: Element | null = el;
        let opacity = 1;
        while (node) {
          const style = getComputedStyle(node);
          opacity *= Number(style.opacity);
          if (style.display === "none" || style.visibility !== "visible" || style.contentVisibility === "hidden" ||
              opacity <= 0.01 || Number(style.zIndex) < 0) return false;
          const box = node.getBoundingClientRect();
          const inset = /^inset\((.*?)(?:\s+round\s+.*?)?\)$/.exec(style.clipPath);
          if (inset) {
            const v = inset[1].trim().split(/\s+/);
            clip(box.left + length(v[3] ?? v[1] ?? v[0], box.width), box.top + length(v[0], box.height),
              box.right - length(v[1] ?? v[0], box.width), box.bottom - length(v[2] ?? v[0], box.height));
          }
          const legacy = /^rect\((.*?)\)$/.exec(style.clip);
          if (legacy) {
            const v = legacy[1].trim().split(/[,\s]+/);
            const edge = (i: number, size: number, fallback: number) => v[i] === "auto" ? fallback : length(v[i], size);
            clip(box.left + edge(3, box.width, 0), box.top + edge(0, box.height, 0),
              box.left + edge(1, box.width, box.width), box.top + edge(2, box.height, box.height));
          }
          if (node === overflowRoot) clipOverflow = true;
          if (node !== el && clipOverflow) {
            let scrollable = false;
            // Scrollable ancestors expose their full scrollable content, not
            // just today's scrollport. Non-scrollable overflow really clips.
            for (const axis of ["x", "y"] as const) {
              const overflow = axis === "x" ? style.overflowX : style.overflowY;
              if (overflow === "visible") continue;
              const size = axis === "x" ? node.clientWidth : node.clientHeight;
              if (!size) return false;
              const origin = axis === "x" ? box.left + node.clientLeft : box.top + node.clientTop;
              let lo = origin, hi = origin + size;
              if (overflow === "auto" || overflow === "scroll") {
                lo -= axis === "x" ? node.scrollLeft : node.scrollTop;
                hi = lo + (axis === "x" ? node.scrollWidth : node.scrollHeight);
              }
              if (axis === "x") { left = Math.max(left, lo); right = Math.min(right, hi); }
              else { top = Math.max(top, lo); bottom = Math.min(bottom, hi); }
              if (!intersects()) return false;
              if (overflow === "auto" || overflow === "scroll") {
                scrollable = true;
                // Project reachable content back to its scrollport before
                // checking outer ancestors/the document's scrollable area.
                if (axis === "x") { left = origin; right = origin + size; }
                else { top = origin; bottom = origin + size; }
              }
            }
            if (scrollable) { segments.push({ target: layoutTarget, root: node }); layoutTarget = node; }
          }
          if (!intersects()) return false;
          // Overflow clips along containing blocks, not every DOM ancestor.
          // A positioned portal can therefore escape a zero-height wrapper.
          if (clipOverflow && (style.position === "fixed" || style.position === "absolute")) {
            // offsetParent follows light DOM for slotted frames in Chromium.
            overflowRoot = containingBlock(node, style.position === "fixed");
            clipOverflow = false;
          }
          node = parent(node);
        }
        const doc = document.scrollingElement ?? document.documentElement;
        clip(-scrollX, -scrollY, Math.max(doc.scrollWidth, innerWidth) - scrollX,
          Math.max(doc.scrollHeight, innerHeight) - scrollY);
        if (!intersects()) return false;
        // Stop at this frame's document, not the top-level viewport. The outer
        // frame walk checks ancestor visibility separately, preserving readable
        // grandchildren below this frame's fold (including cross-origin ones).
        segments.push({ target: layoutTarget, root: document });
        const results = await Promise.all(segments.map(({ target, root }) => new Promise<boolean>((resolve, reject) => {
          const box = target.getBoundingClientRect();
          const bounds = root instanceof Element ? root : doc;
          const margin = Math.max(Math.abs(box.left), Math.abs(box.top), Math.abs(box.right), Math.abs(box.bottom),
            bounds.scrollWidth, bounds.scrollHeight, innerWidth, innerHeight) + 1;
          // A never-delivered callback fails closed: the bounded task rejects
          // and read_text reports the frame in frames_skipped, not as readable.
          const timer = setTimeout(() => { observer.disconnect(); reject(new Error("layout visibility timed out")); }, 250);
          const observer = new IntersectionObserver(entries => {
            clearTimeout(timer); observer.disconnect();
            const intersection = entries[0].intersectionRect;
            resolve(intersection.width > 2 && intersection.height > 2);
          }, { root, rootMargin: `${margin}px` });
          observer.observe(target);
        })));
        return results.every(Boolean);
      }));
      hidden ||= !visible;
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
  return !withinScope ? "excluded" : hidden ? "hidden" : "included";
}
