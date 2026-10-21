/** Failure-isolated, visible composed-document scopes for text observations. */
import type { Frame, Locator } from "playwright-core";
import type { Session } from "../session.js";

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
        if (rect.width <= 2 || rect.height <= 2) return false;
        let node: Element | null = el;
        let opacity = 1;
        while (node) {
          const style = getComputedStyle(node);
          opacity *= Number(style.opacity);
          if (style.display === "none" || style.visibility !== "visible" || opacity <= 0.01) return false;
          node = node.parentElement ?? (node.getRootNode() as ShadowRoot).host ?? null;
        }
        return true;
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
