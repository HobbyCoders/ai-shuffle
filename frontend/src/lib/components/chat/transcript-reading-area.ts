/** Fixed sheets publish settled layout coverage only on open/close/resize.
 * Transcript repricing reads cached bounds, never polls per frame/token.
 * Svelte action cleanup owns registration. */
type Bounds = Pick<DOMRect, 'top' | 'bottom' | 'left' | 'right'>;
const overlays = new Map<HTMLElement, Bounds>();
const beforeCover = new Set<() => void>();

export function beforeTranscriptCovered(capture: () => void): () => void {
	beforeCover.add(capture);
	return () => { beforeCover.delete(capture); };
}

/** Svelte action for full-width bottom sheets and full-screen covers. */
export function obscuresTranscript(node: HTMLElement, enabled = true) {
	let registered = false;
	let observer: ResizeObserver | null = null;
	function measure(event?: Event) {
		if (!registered || !node.isConnected || (event && event.type !== 'resize' && event.target !== node)) return;
		// Fixed sheets animate with transform/translate, which never resizes
		// their layout box. Reserve the final open coverage from the first
		// frame, conservatively, instead of caching an offscreen animated rect.
		// All viewport-fixed publishers have no positioned offset parent.
		if (getComputedStyle(node).position === 'fixed' && node.offsetParent === null) {
			const top = node.offsetTop, left = node.offsetLeft;
			overlays.set(node, { top, left, bottom: top + node.offsetHeight, right: left + node.offsetWidth });
		} else overlays.set(node, node.getBoundingClientRect());
	}
	function update(next: boolean) {
		if (registered === next) return;
		if (next) {
			for (const capture of beforeCover) capture();
			registered = true;
			measure();
			observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(() => measure());
			observer?.observe(node);
			window.addEventListener('resize', measure);
		} else {
			registered = false;
			overlays.delete(node);
			observer?.disconnect(); observer = null;
			window.removeEventListener('resize', measure);
		}
	}
	update(enabled);
	return { update, destroy() { update(false); } };
}

export function exposedReadingViewport(viewport: Bounds): Pick<Bounds, 'top' | 'bottom'> {
	let bottom = viewport.bottom;
	for (const [node, box] of overlays) {
		// Route changes normally destroy the action; this also protects against
		// a removed DOM node and a delayed resize/animation callback.
		if (!node.isConnected) { overlays.delete(node); continue; }
		if (box.right <= viewport.left || box.left >= viewport.right || box.bottom <= viewport.top || box.bottom <= box.top) continue;
		bottom = Math.min(bottom, Math.max(viewport.top, box.top));
	}
	return { top: viewport.top, bottom };
}
