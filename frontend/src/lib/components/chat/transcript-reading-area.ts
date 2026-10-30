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
	function measure() {
		if (!registered || !node.isConnected) return;
		// Fixed sheets animate with transform/translate, which never resizes
		// their layout box. Reserve the final open coverage from the first
		// frame, conservatively, instead of caching an offscreen animated rect.
		// All viewport-fixed publishers have no positioned offset parent.
		const style = getComputedStyle(node);
		if (style.position === 'fixed' && node.offsetParent === null) {
			// Resolved insets preserve fractions and exclude animated transforms.
			// Round coverage OUTWARD: even a subpixel exposure overstatement can
			// choose the wrong reading row. Offset-only fallbacks include their
			// rounding uncertainty (one coordinate, or two summed metrics).
			const top = Number.parseFloat(style.top) + (Number.parseFloat(style.marginTop) || 0);
			const left = Number.parseFloat(style.left) + (Number.parseFloat(style.marginLeft) || 0);
			const bottom = window.innerHeight - Number.parseFloat(style.bottom) - (Number.parseFloat(style.marginBottom) || 0);
			const right = window.innerWidth - Number.parseFloat(style.right) - (Number.parseFloat(style.marginRight) || 0);
			overlays.set(node, {
				top: Math.floor(Number.isFinite(top) ? top : node.offsetTop - 1),
				left: Math.floor(Number.isFinite(left) ? left : node.offsetLeft - 1),
				bottom: Math.ceil(Number.isFinite(bottom) ? bottom : node.offsetTop + node.offsetHeight + 1),
				right: Math.ceil(Number.isFinite(right) ? right : node.offsetLeft + node.offsetWidth + 1)
			});
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
