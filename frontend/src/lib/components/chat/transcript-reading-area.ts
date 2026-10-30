/** Bottom sheets publish their DOM node, not a per-frame measured height.
 * Read geometry only when a transcript reprice needs the exposed reading area.
 * Capturing before registration also preserves the last key under full cover. */
const overlays = new Set<HTMLElement>();
const beforeCover = new Set<() => void>();
type Bounds = Pick<DOMRect, 'top' | 'bottom' | 'left' | 'right'>;

export function beforeTranscriptCovered(capture: () => void): () => void {
	beforeCover.add(capture);
	return () => { beforeCover.delete(capture); };
}

/** Svelte action for full-width bottom sheets and full-screen covers. */
export function obscuresTranscript(node: HTMLElement, enabled = true) {
	let registered = false;
	function update(next: boolean) {
		if (registered === next) return;
		if (next) {
			for (const capture of beforeCover) capture();
			overlays.add(node);
		} else overlays.delete(node);
		registered = next;
	}
	update(enabled);
	return { update, destroy() { overlays.delete(node); } };
}

export function exposedReadingViewport(viewport: Bounds): Pick<Bounds, 'top' | 'bottom'> {
	let bottom = viewport.bottom;
	for (const node of overlays) {
		if (!node.isConnected) continue;
		const box = node.getBoundingClientRect();
		if (box.right <= viewport.left || box.left >= viewport.right || box.bottom <= viewport.top || box.bottom <= box.top) continue;
		bottom = Math.min(bottom, Math.max(viewport.top, box.top));
	}
	return { top: viewport.top, bottom };
}
