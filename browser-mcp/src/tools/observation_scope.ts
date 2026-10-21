/** Shared composed-document scope for text observations (not action targeting). */
import type { Locator } from "playwright-core";
import type { Session } from "../session.js";

export async function observationRoots(session: Session, scopeRef?: string): Promise<Locator[]> {
  const page = await session.ensurePage();
  if (!scopeRef) return page.frames().map(frame => frame.locator("body"));
  const scope = await session.resolveRef(scopeRef);
  const handle = await scope.elementHandle();
  if (!handle) throw new Error(`stale_ref: ${scopeRef}`);
  try {
    const owner = await handle.ownerFrame();
    const roots = [scope];
    for (const frame of page.frames()) {
      if (frame === owner) continue;
      let child = frame;
      while (child.parentFrame() && child.parentFrame() !== owner) child = child.parentFrame()!;
      if (child.parentFrame() !== owner) continue;
      const element = await child.frameElement();
      try {
        const inside = await scope.evaluate((root, candidate) => {
          let node: Node | null = candidate;
          while (node) {
            if (node === root) return true;
            node = node.parentNode ?? (node.getRootNode() as ShadowRoot).host ?? null;
          }
          return false;
        }, element);
        if (inside) roots.push(frame.locator("body"));
      } finally { await element.dispose(); }
    }
    return roots;
  } finally { await handle.dispose(); }
}
