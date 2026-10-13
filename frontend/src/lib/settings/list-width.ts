/**
 * Collection list width: default 384px, list at least 300px, detail at least
 * 440px, remembered in localStorage. Shared by every collection page.
 */

export const LIST_WIDTH_DEFAULT = 384;
export const LIST_WIDTH_MIN = 300;
export const DETAIL_WIDTH_MIN = 440;
/** Fixed list width between 601 and 900px of shell width (no divider there). */
export const LIST_WIDTH_NARROW = 280;
export const LIST_WIDTH_STEP = 16;
export const LIST_WIDTH_STEP_LARGE = 64;
export const LIST_WIDTH_STORAGE_KEY = 'settings.listWidth';

/** Largest list width that still leaves the detail its minimum. */
export function maxListWidth(containerWidth: number): number {
	if (!containerWidth || containerWidth <= 0) return Number.POSITIVE_INFINITY;
	return Math.max(LIST_WIDTH_MIN, containerWidth - DETAIL_WIDTH_MIN);
}

export function clampListWidth(width: number, containerWidth: number): number {
	const max = maxListWidth(containerWidth);
	const w = Number.isFinite(width) ? width : LIST_WIDTH_DEFAULT;
	return Math.round(Math.max(LIST_WIDTH_MIN, Math.min(w, max)));
}

export function readListWidth(): number {
	try {
		const raw = Number(localStorage.getItem(LIST_WIDTH_STORAGE_KEY));
		return Number.isFinite(raw) && raw >= LIST_WIDTH_MIN ? raw : LIST_WIDTH_DEFAULT;
	} catch {
		return LIST_WIDTH_DEFAULT;
	}
}

export function writeListWidth(width: number): void {
	try {
		localStorage.setItem(LIST_WIDTH_STORAGE_KEY, String(Math.round(width)));
	} catch {
		// Storage can be blocked; the width then lasts for this page only.
	}
}
