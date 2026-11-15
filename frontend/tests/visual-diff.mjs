// Judging a visual-regression difference (visual-regression.mjs).
//
// The whole-image share alone lets a small real change through: ten 1px rail
// ticks moving was 0.015% to 0.076% of the pixels, under the 0.2% tolerance,
// and the turn-navigator baselines stayed stale for five days (#1644). Each
// moved tick is one connected area of 10 to 12 changed pixels. In the pinned
// Playwright container, renders of an unchanged fixture match their baseline
// exactly or differ only below the per-channel threshold (99 CI renders,
// 2026-11-13 to 2026-11-15), so a connected area of MIN_AREA changed pixels
// is a real change, not noise.

/** Share of pixels allowed to differ in total (antialiasing, caret blink). */
export const TOLERANCE = 0.002;
/** Changed pixels in one connected area (8-neighbour) that fail on their own. */
export const MIN_AREA = 8;

/**
 * Changed pixel count and the size of the largest connected area of changed
 * pixels (8-neighbour) in `mask` (one byte per pixel, non-zero = changed).
 */
export function changedAreas(mask, width, height) {
	const seen = new Uint8Array(mask.length);
	const stack = [];
	let changed = 0, largest = 0;
	for (let start = 0; start < mask.length; start++) {
		if (!mask[start] || seen[start]) continue;
		let size = 0;
		seen[start] = 1;
		stack.push(start);
		while (stack.length) {
			const i = stack.pop();
			size++;
			const x = i % width, y = (i - x) / width;
			for (let dy = -1; dy <= 1; dy++) {
				const ny = y + dy;
				if (ny < 0 || ny >= height) continue;
				for (let dx = -1; dx <= 1; dx++) {
					const nx = x + dx;
					if (nx < 0 || nx >= width) continue;
					const j = ny * width + nx;
					if (mask[j] && !seen[j]) {
						seen[j] = 1;
						stack.push(j);
					}
				}
			}
		}
		changed += size;
		if (size > largest) largest = size;
	}
	return { changed, largest };
}

/**
 * 'ok' or the reason a render fails against its baseline. `ratio` is the
 * share of changed pixels (1 when the sizes differ), `largest` the largest
 * connected changed area.
 */
export function verdict({ ratio, largest }, { tolerance = TOLERANCE, minArea = MIN_AREA } = {}) {
	if (ratio > tolerance) return `CHANGED ${(ratio * 100).toFixed(2)}%`;
	if (largest >= minArea) return `CHANGED ${(ratio * 100).toFixed(3)}%, one area of ${largest} px`;
	return 'ok';
}
