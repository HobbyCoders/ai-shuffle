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

const DAY_MS = 86_400_000;
const DAY_RE = /^(\d{4})-(\d{2})-(\d{2})(?:$|[T ])/;

/** A calendar day ("YYYY-MM-DD", or a timestamp starting with one) as a UTC
 *  midnight, or NaN. Days are counted on that UTC line, so the local time
 *  zone and its DST changes never add, drop or repeat a day. */
function dayMs(day: string): number {
	const m = DAY_RE.exec(day);
	return m ? Date.UTC(+m[1], +m[2] - 1, +m[3]) : NaN;
}

const dayKey = (ms: number) => new Date(ms).toISOString().slice(0, 10);

/** `day` moved by `days` calendar days ("2026-03-01", -1 → "2026-02-28"). */
export function shiftDay(day: string, days: number): string {
	const ms = dayMs(day);
	return Number.isNaN(ms) ? day : dayKey(ms + days * DAY_MS);
}

/** Every calendar day from `start` to `end`, both included. Empty when either
 *  is not a day, the range runs backwards, or it is implausibly long. */
export function daysInRange(start: string, end: string, max = 1000): string[] {
	const from = dayMs(start);
	const to = dayMs(end);
	if (Number.isNaN(from) || Number.isNaN(to) || to < from) return [];
	const count = Math.round((to - from) / DAY_MS) + 1;
	if (count > max) return [];
	return Array.from({ length: count }, (_, i) => dayKey(from + i * DAY_MS));
}

export interface SpendDay {
	date: string;
	input_tokens: number;
	output_tokens: number;
	cost: number;
	estimated: boolean;
}

/**
 * One bar per day of the range, oldest first. The trends API returns only days
 * that had usage; a day without any becomes a zero, so the axis stays
 * continuous and the range end (today) is always the last bar. Points outside
 * the range are dropped and two points on one day are added together. A range
 * that cannot be read keeps the points as they came.
 */
export function fillDays(points: readonly SpendDay[], start: string, end: string): SpendDay[] {
	const days = daysInRange(start, end);
	if (days.length === 0) return [...points];
	const byDay = new Map<string, SpendDay>(days.map((date) => [date, { date, input_tokens: 0, output_tokens: 0, cost: 0, estimated: false }]));
	for (const point of points) {
		const ms = dayMs(point.date);
		const day = Number.isNaN(ms) ? undefined : byDay.get(dayKey(ms));
		if (!day) continue;
		day.input_tokens += point.input_tokens || 0;
		day.output_tokens += point.output_tokens || 0;
		day.cost += point.cost || 0;
		day.estimated ||= point.estimated;
	}
	return days.map((date) => byDay.get(date)!);
}
