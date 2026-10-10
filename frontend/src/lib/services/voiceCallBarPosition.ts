/**
 * Where the away voice-call bar floats once the user drags it out of the
 * notification dock. Saved on this device only (a phone and a desktop window
 * want different spots).
 *
 * The position is a fraction of the free space, not pixels: x 0 is the left
 * edge and 1 the right edge of the area the bar may cover, so the bar keeps
 * its corner when the window resizes and can never be placed off-screen.
 * `null` means docked (the default place under the page header).
 */
const KEY = 'voice-call-bar-position';

export interface CallBarPosition {
	x: number;
	y: number;
}

const unit = (value: unknown): number | null =>
	typeof value === 'number' && Number.isFinite(value) ? Math.max(0, Math.min(1, value)) : null;

export function loadCallBarPosition(): CallBarPosition | null {
	try {
		const raw = localStorage.getItem(KEY);
		if (!raw) return null;
		const parsed = JSON.parse(raw) as Record<string, unknown> | null;
		const x = unit(parsed?.x);
		const y = unit(parsed?.y);
		return x === null || y === null ? null : { x, y };
	} catch {
		return null;
	}
}

export function saveCallBarPosition(position: CallBarPosition | null): void {
	try {
		if (position) localStorage.setItem(KEY, JSON.stringify({ x: unit(position.x) ?? 0, y: unit(position.y) ?? 0 }));
		else localStorage.removeItem(KEY);
	} catch {
		/* private browsing */
	}
}

/**
 * The fraction that puts a box of size `size` with its leading edge at
 * `edge`, inside an allowed span starting at `start` that is `span` long.
 * A box as large as the span sits at 0.
 */
export function toFraction(edge: number, size: number, start: number, span: number): number {
	const free = span - size;
	if (!(free > 0)) return 0;
	return Math.max(0, Math.min(1, (edge - start) / free));
}
