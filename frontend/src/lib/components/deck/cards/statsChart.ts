/** Axis helpers for the Stats "Estimated spend over time" bars. */

/** A rounded top for the currency scale and its three gridlines (step, 2·step, 3·step). */
export function spendScale(max: number): { top: number; ticks: number[] } {
	if (!(max > 0)) return { top: 3, ticks: [1, 2, 3] };
	const raw = max / 3;
	const mag = 10 ** Math.floor(Math.log10(raw));
	const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw - 1e-9) ?? 10 * mag;
	// toPrecision drops float noise such as 0.05 * 3 = 0.15000000000000002.
	const at = (k: number) => Number((step * k).toPrecision(12));
	return { top: at(3), ticks: [at(1), at(2), at(3)] };
}

/** "$1", "$2.50", "$0.05", "$1,000": as few decimals as the step needs. */
export function formatScale(value: number): string {
	const cents = Math.round(value * 100);
	if (cents % 100 === 0) return '$' + (cents / 100).toLocaleString('en-US');
	return '$' + (cents / 100).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

/**
 * Indexes that get a date label: first, last and evenly spaced between, 3 to 5
 * in all. A count that divides the range evenly wins (31 days: 0, 10, 20, 30).
 */
export function labelIndexes(length: number, count?: number): number[] {
	if (length <= 0) return [];
	if (length <= 5) return Array.from({ length }, (_, i) => i);
	count ??= [5, 4, 3].find((c) => (length - 1) % (c - 1) === 0) ?? 5;
	const out = new Set<number>();
	for (let k = 0; k < count; k++) out.add(Math.round((k * (length - 1)) / (count - 1)));
	return [...out];
}
