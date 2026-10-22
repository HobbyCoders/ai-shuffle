/**
 * Logic for the Browser panel's own pickers.
 *
 * Chrome draws a `<select>` list and the date/time pickers as separate OS
 * widgets, outside the page, so they never appear in the screencast. The
 * panel stands in with its own list and calendar/time picker; this module
 * holds everything about them that is not markup: wire validation, option
 * navigation and type-ahead, date/time parsing in each control's own unit,
 * and the HTML rules for min, max and step.
 *
 * Units (all integers):
 *   date → days since 1970-01-01, month → months since 1970-01,
 *   week → ISO weeks since 1970-W01, time → ms since midnight,
 *   datetime-local → ms since 1970-01-01T00:00 (no time zone).
 */

export const PICKER_KINDS = ['select', 'date', 'time', 'datetime-local', 'month', 'week'] as const;
export type PickerKind = (typeof PICKER_KINDS)[number];
export type DateKind = Exclude<PickerKind, 'select'>;

/** A picker's choice for the page: an option by index with the label shown for it, or a date/time value. */
export type PickerChoice = { index: number; label: string } | { value: string };

export function isPickerKind(type: string): type is PickerKind {
	return (PICKER_KINDS as readonly string[]).includes(type);
}

/**
 * Keys that open the control's popup in Chrome. On a focused page control the
 * panel opens its own picker for them instead (the real popup would be
 * invisible and swallow the next keys). Plain arrows on a select change the
 * value in place, which the screencast shows, so they still go to the page.
 */
export function opensPicker(kind: PickerKind, e: { key: string; altKey: boolean; ctrlKey: boolean; metaKey: boolean }): boolean {
	if (e.ctrlKey || e.metaKey) return false;
	if (e.key === 'F4' || (e.altKey && (e.key === 'ArrowDown' || e.key === 'ArrowUp'))) return true;
	return kind === 'select' && !e.altKey && (e.key === ' ' || e.key === 'Enter');
}

// ── Select ───────────────────────────────────────────────────────────────

export interface BrowserChoice {
	label: string;
	disabled: boolean;
	/** Index into `groups` for an option inside an `<optgroup>`. */
	group?: number;
}

/** A select's options as the runtime reads them (labels only; picked by index). */
export interface BrowserChoices {
	selected: number;
	groups: string[];
	options: BrowserChoice[];
	/** The page has more options than the panel was sent. */
	truncated?: boolean;
}

function record(v: unknown): v is Record<string, unknown> {
	return v !== null && typeof v === 'object' && !Array.isArray(v);
}

/** Validate an options reply before rendering it (same bounds as the server). */
export function isBrowserChoices(v: unknown): v is BrowserChoices {
	if (!record(v) || !Array.isArray(v.groups) || !Array.isArray(v.options) || v.options.length > 1000) return false;
	if (v.truncated !== undefined && typeof v.truncated !== 'boolean') return false;
	const groups = v.groups.length;
	if (!v.groups.every((g) => typeof g === 'string' && g.length <= 200)) return false;
	const ok = v.options.every(
		(o) =>
			record(o) &&
			typeof o.label === 'string' &&
			o.label.length <= 200 &&
			typeof o.disabled === 'boolean' &&
			(o.group === undefined || (Number.isInteger(o.group) && (o.group as number) >= 0 && (o.group as number) < groups)),
	);
	return ok && Number.isInteger(v.selected) && (v.selected as number) >= -1 && (v.selected as number) < v.options.length;
}

export function edgeChoice(options: readonly BrowserChoice[], end: 'first' | 'last'): number {
	if (end === 'first') return options.findIndex((o) => !o.disabled);
	for (let i = options.length - 1; i >= 0; i--) if (!options[i].disabled) return i;
	return -1;
}

/**
 * The option `delta` enabled steps away (|delta| > 1 is a page jump), never
 * past the first/last enabled option. From -1 (nothing active) it starts at
 * the first enabled option.
 */
export function moveChoice(options: readonly BrowserChoice[], from: number, delta: number): number {
	if (from < 0 || from >= options.length) return edgeChoice(options, delta < 0 ? 'last' : 'first');
	const dir = Math.sign(delta);
	let at = from;
	let left = Math.abs(delta);
	for (let i = from + dir; i >= 0 && i < options.length && left > 0; i += dir) {
		if (!options[i].disabled) {
			at = i;
			left--;
		}
	}
	return at;
}

/** Case- and accent-insensitive form for matching labels. */
export function fold(text: string): string {
	return text.normalize('NFD').replace(/[̀-ͯ]/g, '').toLocaleLowerCase().trim();
}

/** Collects typed characters; a pause longer than the timeout starts afresh. */
export class TypeAhead {
	private query = '';
	private last = -Infinity;
	constructor(private timeoutMs = 750) {}
	push(char: string, now: number): string {
		this.query = now - this.last > this.timeoutMs ? char : this.query + char;
		this.last = now;
		return this.query;
	}
	reset() {
		this.query = '';
		this.last = -Infinity;
	}
}

/**
 * The enabled option a type-ahead query lands on, like a native list: the
 * first label starting with the query after the current option, wrapping.
 * A repeated single letter ("bbb") cycles through the labels starting with it.
 */
export function typeAheadMatch(options: readonly BrowserChoice[], query: string, from: number): number {
	const q = fold(query);
	if (!q) return -1;
	const cycling = q.length > 1 && [...q].every((c) => c === q[0]);
	const needle = cycling ? q[0] : q;
	// A longer query refines the current match; a new letter or a cycle moves on.
	const start = needle.length > 1 && !cycling ? from : from + 1;
	const n = options.length;
	for (let k = 0; k < n; k++) {
		const i = (((start + k) % n) + n) % n;
		if (!options[i].disabled && fold(options[i].label).startsWith(needle)) return i;
	}
	return -1;
}

/** Indexes of options whose label contains the query (all for an empty query). */
export function matchingChoices(choices: BrowserChoices, query: string): number[] {
	const q = fold(query);
	const out: number[] = [];
	choices.options.forEach((o, i) => {
		if (!q || fold(o.label).includes(q)) out.push(i);
	});
	return out;
}

// ── Calendar arithmetic ──────────────────────────────────────────────────

const DAY_MS = 86_400_000;
const MAX_YEAR = 275760;

export function daysFromCivil(year: number, month: number, day: number): number {
	const d = new Date(0);
	d.setUTCFullYear(year, month - 1, day);
	return Math.floor(d.getTime() / DAY_MS);
}

export function civilFromDays(days: number): { year: number; month: number; day: number; weekday: number } {
	const d = new Date(days * DAY_MS);
	return { year: d.getUTCFullYear(), month: d.getUTCMonth() + 1, day: d.getUTCDate(), weekday: d.getUTCDay() };
}

export function daysInMonth(year: number, month: number): number {
	return daysFromCivil(year, month + 1, 1) - daysFromCivil(year, month, 1);
}

/** 42 consecutive days (six weeks) covering the month, from `firstDay` (0 = Sunday). */
export function monthGrid(year: number, month: number, firstDay: number): number[] {
	const first = daysFromCivil(year, month, 1);
	const lead = (civilFromDays(first).weekday - firstDay + 7) % 7;
	return Array.from({ length: 42 }, (_, i) => first - lead + i);
}

/** ISO week (Monday start) holding the day, as weeks since 1970-W01 (Monday 1969-12-29). */
export function weekIndexOf(days: number): number {
	return Math.floor((days + 3) / 7);
}

export function weekMonday(week: number): number {
	return week * 7 - 3;
}

export function monthIndex(year: number, month: number): number {
	return (year - 1970) * 12 + (month - 1);
}

export function monthOf(index: number): { year: number; month: number } {
	return { year: 1970 + Math.floor(index / 12), month: (((index % 12) + 12) % 12) + 1 };
}

/** First day of the week for a locale (0 = Sunday), from Intl where it is known. */
export function firstDayOfWeek(locale?: string): number {
	try {
		const loc = new Intl.Locale(locale ?? (typeof navigator !== 'undefined' ? navigator.language : 'en-US'));
		const info =
			(loc as unknown as { getWeekInfo?: () => { firstDay: number } }).getWeekInfo?.() ??
			(loc as unknown as { weekInfo?: { firstDay: number } }).weekInfo;
		if (info && Number.isInteger(info.firstDay)) return info.firstDay % 7;
		const region = loc.maximize().region;
		return ['US', 'CA', 'MX', 'BR', 'JP', 'KR', 'TW', 'HK', 'IL', 'PH', 'ZA', 'IN', 'AU'].includes(region ?? '') ? 0 : 1;
	} catch {
		return 0;
	}
}

// ── Parsing and formatting ───────────────────────────────────────────────

const pad = (n: number, w = 2) => String(n).padStart(w, '0');
const yearText = (y: number) => pad(y, 4);

function validYear(y: number) {
	return y >= 1 && y <= MAX_YEAR;
}

function parseDateText(s: string): number | null {
	const m = /^(\d{4,6})-(\d{2})-(\d{2})$/.exec(s);
	if (!m) return null;
	const [y, mo, d] = [+m[1], +m[2], +m[3]];
	if (!validYear(y) || mo < 1 || mo > 12 || d < 1 || d > daysInMonth(y, mo)) return null;
	return daysFromCivil(y, mo, d);
}

function parseTimeText(s: string): number | null {
	const m = /^(\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,3}))?)?$/.exec(s);
	if (!m) return null;
	const [h, mi, sec] = [+m[1], +m[2], +(m[3] ?? 0)];
	if (h > 23 || mi > 59 || sec > 59) return null;
	const ms = m[4] ? +m[4].padEnd(3, '0') : 0;
	return ((h * 60 + mi) * 60 + sec) * 1000 + ms;
}

/** A control's value string in its unit, or null when it is empty or malformed. */
export function parseValue(kind: DateKind, s: string | null | undefined): number | null {
	if (!s) return null;
	switch (kind) {
		case 'date':
			return parseDateText(s);
		case 'time':
			return parseTimeText(s);
		case 'datetime-local': {
			const m = /^(.+?)[T ](.+)$/.exec(s);
			if (!m) return null;
			const d = parseDateText(m[1]);
			const t = parseTimeText(m[2]);
			return d === null || t === null ? null : d * DAY_MS + t;
		}
		case 'month': {
			const m = /^(\d{4,6})-(\d{2})$/.exec(s);
			if (!m || !validYear(+m[1]) || +m[2] < 1 || +m[2] > 12) return null;
			return monthIndex(+m[1], +m[2]);
		}
		case 'week': {
			const m = /^(\d{4,6})-W(\d{2})$/.exec(s);
			if (!m || !validYear(+m[1])) return null;
			const [y, w] = [+m[1], +m[2]];
			const jan4 = daysFromCivil(y, 1, 4);
			const monday = jan4 - ((civilFromDays(jan4).weekday + 6) % 7) + (w - 1) * 7;
			if (w < 1 || civilFromDays(monday + 3).year !== y) return null; // no week 53 this year
			return weekIndexOf(monday);
		}
	}
}

export type TimePrecision = 'minute' | 'second' | 'ms';

function timeText(ms: number, precision: TimePrecision): string {
	const h = Math.floor(ms / 3_600_000);
	const mi = Math.floor(ms / 60_000) % 60;
	const s = Math.floor(ms / 1000) % 60;
	const f = ms % 1000;
	const p: TimePrecision = f ? 'ms' : s && precision === 'minute' ? 'second' : precision;
	return `${pad(h)}:${pad(mi)}${p === 'minute' ? '' : `:${pad(s)}`}${p === 'ms' ? `.${pad(f, 3)}` : ''}`;
}

/** The value string a control expects. Time shows at least `precision`, more if the value needs it. */
export function formatValue(kind: DateKind, v: number, precision: TimePrecision = 'minute'): string {
	switch (kind) {
		case 'date': {
			const c = civilFromDays(v);
			return `${yearText(c.year)}-${pad(c.month)}-${pad(c.day)}`;
		}
		case 'time':
			return timeText(((v % DAY_MS) + DAY_MS) % DAY_MS, precision);
		case 'datetime-local': {
			const day = Math.floor(v / DAY_MS);
			return `${formatValue('date', day)}T${timeText(v - day * DAY_MS, precision)}`;
		}
		case 'month': {
			const m = monthOf(v);
			return `${yearText(m.year)}-${pad(m.month)}`;
		}
		case 'week': {
			const thursday = weekMonday(v) + 3;
			const year = civilFromDays(thursday).year;
			const week = Math.floor((thursday - daysFromCivil(year, 1, 1)) / 7) + 1;
			return `${yearText(year)}-W${pad(week)}`;
		}
	}
}

// ── min, max and step ────────────────────────────────────────────────────

export interface Limits {
	kind: DateKind;
	min: number | null;
	max: number | null;
	/** Step in the kind's unit; null for step="any". */
	step: number | null;
	/** Step base: min when given, else the HTML default. */
	base: number;
	/** A time range with min after max wraps past midnight. */
	wraps: boolean;
}

const DEFAULT_STEP: Record<DateKind, number> = { date: 1, month: 1, week: 1, time: 60, 'datetime-local': 60 };

/** The page's min/max/step attributes in the kind's unit, by the HTML rules. */
/**
 * The panel's limits for a date/time control, from the page's attributes.
 * Steps count from min; without one, from the value attribute (`base`), as
 * HTML does, and otherwise from the kind's default (1970-01-01, 00:00…).
 */
export function limitsFor(kind: DateKind, min?: string, max?: string, step?: string, base?: string): Limits {
	const lo = parseValue(kind, min);
	const hi = parseValue(kind, max);
	const timed = kind === 'time' || kind === 'datetime-local';
	let s: number | null;
	if (step?.trim().toLowerCase() === 'any') s = null;
	else {
		const n = Number(step);
		const raw = step !== undefined && step.trim() !== '' && Number.isFinite(n) && n > 0 ? n : DEFAULT_STEP[kind];
		// Seconds become ms for time kinds; day/month/week steps are whole units.
		s = timed ? Math.max(1, Math.round(raw * 1000)) : Math.max(1, Math.round(raw));
	}
	const wraps = kind === 'time' && lo !== null && hi !== null && lo > hi;
	return { kind, min: lo, max: hi, step: s, base: lo ?? parseValue(kind, base) ?? 0, wraps };
}

/**
 * The wall-clock time now in the page's time zone (an IANA name the runtime
 * reports), as a local Date the pickers can read with getHours() and friends.
 * Falls back to this device's clock when the zone is missing or unknown.
 */
export function wallClock(zone?: string, at: Date = new Date()): Date {
	if (!zone) return at;
	try {
		const parts = new Intl.DateTimeFormat('en-US', {
			timeZone: zone, hourCycle: 'h23',
			year: 'numeric', month: 'numeric', day: 'numeric', hour: 'numeric', minute: 'numeric', second: 'numeric',
		}).formatToParts(at);
		const part = (type: string) => Number(parts.find((p) => p.type === type)?.value);
		const local = new Date(part('year'), part('month') - 1, part('day'), part('hour'), part('minute'), part('second'));
		return Number.isNaN(local.getTime()) ? at : local;
	} catch {
		return at; // an unknown zone name
	}
}

const mod = (a: number, b: number) => ((a % b) + b) % b;

function onStep(l: Limits, v: number) {
	return l.step === null || mod(v - l.base, l.step) === 0;
}

export function allowed(l: Limits, v: number): boolean {
	if (!Number.isFinite(v)) return false;
	if (l.kind === 'time' && (v < 0 || v >= DAY_MS)) return false;
	const inRange = l.wraps
		? v >= l.min! || v <= l.max!
		: (l.min === null || v >= l.min) && (l.max === null || v <= l.max);
	return inRange && onStep(l, v);
}

/** First on-step value ≥ v (v itself without a step). */
function stepUp(l: Limits, v: number) {
	return l.step === null ? v : l.base + Math.ceil((v - l.base) / l.step) * l.step;
}

function segmentHas(l: Limits, lo: number, hi: number) {
	return lo <= hi && stepUp(l, lo) <= hi;
}

/** Whether [from, to) holds any allowed value: a time column entry, a calendar day. */
export function anyAllowedIn(l: Limits, from: number, to: number): boolean {
	const last = to - 1; // integer units: the inclusive upper end
	if (l.wraps) {
		return segmentHas(l, Math.max(from, l.min!), Math.min(last, DAY_MS - 1)) || segmentHas(l, Math.max(from, 0), Math.min(last, l.max!));
	}
	let lo = from;
	let hi = last;
	if (l.kind === 'time') {
		lo = Math.max(lo, 0);
		hi = Math.min(hi, DAY_MS - 1);
	}
	if (l.min !== null) lo = Math.max(lo, l.min);
	if (l.max !== null) hi = Math.min(hi, l.max);
	return segmentHas(l, lo, hi);
}

/** The allowed value closest to v (ties go down), or null if none exists nearby. */
export function nearestAllowed(l: Limits, v: number): number | null {
	if (allowed(l, v)) return v;
	let x = v;
	if (!l.wraps) {
		if (l.min !== null) x = Math.max(x, l.min);
		if (l.max !== null) x = Math.min(x, l.max);
		if (l.kind === 'time') x = Math.min(Math.max(x, 0), DAY_MS - 1);
	}
	const candidates: number[] = [];
	if (l.step === null) candidates.push(x);
	else {
		const down = l.base + Math.floor((x - l.base) / l.step) * l.step;
		candidates.push(down, down + l.step, down - l.step);
	}
	if (l.min !== null) candidates.push(l.min);
	if (l.max !== null) candidates.push(stepUp(l, l.max) > l.max && l.step !== null ? stepUp(l, l.max) - l.step : l.max);
	const valid = candidates.filter((c) => allowed(l, c));
	if (!valid.length) return null;
	return valid.reduce((best, c) => (Math.abs(c - v) < Math.abs(best - v) || (Math.abs(c - v) === Math.abs(best - v) && c < best) ? c : best));
}

/** How much of the time a value string must carry for this step. */
export function precisionFor(l: Limits): TimePrecision {
	if (l.step !== null && l.step % 1000) return 'ms';
	if (l.step !== null && l.step % 60_000) return 'second';
	return 'minute';
}

/**
 * Scroll `el` into view inside `container` only. (scrollIntoView would also
 * scroll every ancestor, including the clipped live view and the app.)
 */
export function revealIn(container: HTMLElement | null | undefined, el: Element | null | undefined, center = false) {
	if (!container || !el) return;
	const c = container.getBoundingClientRect();
	const r = el.getBoundingClientRect();
	if (center) container.scrollTop += r.top + r.height / 2 - (c.top + c.height / 2);
	else if (r.top < c.top) container.scrollTop -= c.top - r.top;
	else if (r.bottom > c.bottom) container.scrollTop += r.bottom - c.bottom;
}

// ── Popup placement ──────────────────────────────────────────────────────

export interface PopupPlacement {
	left: number;
	top: number;
	width: number;
	maxHeight: number;
	placement: 'below' | 'above';
}

const EDGE = 8;
const GAP = 4;

/**
 * Where a popup goes for a field at `anchor` (live-view px): below the field
 * when its preferred height fits, otherwise on whichever side has more room,
 * at least as wide as the field and always inside the view.
 */
export function placePopup(
	anchor: { left: number; top: number; width: number; height: number },
	view: { width: number; height: number },
	size: { width: number; height: number },
): PopupPlacement {
	const width = Math.max(0, Math.min(Math.max(size.width, anchor.width), view.width - EDGE * 2));
	const left = Math.min(Math.max(EDGE, anchor.left), Math.max(EDGE, view.width - width - EDGE));
	const belowTop = anchor.top + anchor.height + GAP;
	const below = view.height - belowTop - EDGE;
	const above = anchor.top - GAP - EDGE;
	if (below >= size.height || below >= above) {
		return { left, top: Math.max(EDGE, belowTop), width, maxHeight: Math.max(0, Math.min(size.height, below)), placement: 'below' };
	}
	const maxHeight = Math.max(0, Math.min(size.height, above));
	return { left, top: anchor.top - GAP - maxHeight, width, maxHeight, placement: 'above' };
}
