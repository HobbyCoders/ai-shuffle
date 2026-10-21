import { describe, expect, it } from 'vitest';
import {
	allowed,
	anyAllowedIn,
	civilFromDays,
	daysFromCivil,
	edgeChoice,
	firstDayOfWeek,
	formatValue,
	isBrowserChoices,
	isPickerKind,
	limitsFor,
	matchingChoices,
	monthGrid,
	moveChoice,
	nearestAllowed,
	opensPicker,
	parseValue,
	placePopup,
	TypeAhead,
	typeAheadMatch,
	weekIndexOf,
	type BrowserChoice,
} from './pickers';

const DAY = 86_400_000;
const opts = (...labels: (string | [string, boolean])[]): BrowserChoice[] =>
	labels.map((l) => (typeof l === 'string' ? { label: l, disabled: false } : { label: l[0], disabled: l[1] }));

describe('picker kinds', () => {
	it('recognizes the popup controls only', () => {
		for (const kind of ['select', 'date', 'time', 'datetime-local', 'month', 'week']) expect(isPickerKind(kind)).toBe(true);
		for (const kind of ['text', 'color', 'number', 'file', '']) expect(isPickerKind(kind)).toBe(false);
	});

	it('opens a select from Space, Enter, F4 and Alt+arrows, a date only from F4 and Alt+arrows', () => {
		const key = (key: string, altKey = false) => ({ key, altKey, ctrlKey: false, metaKey: false });
		for (const k of [key(' '), key('Enter'), key('F4'), key('ArrowDown', true), key('ArrowUp', true)])
			expect(opensPicker('select', k)).toBe(true);
		expect(opensPicker('select', key('ArrowDown'))).toBe(false); // changes the value in the page itself
		expect(opensPicker('date', key(' '))).toBe(false);
		expect(opensPicker('date', key('F4'))).toBe(true);
		expect(opensPicker('time', key('ArrowDown', true))).toBe(true);
		expect(opensPicker('select', { key: ' ', altKey: false, ctrlKey: true, metaKey: false })).toBe(false);
	});
});

describe('select options', () => {
	it('validates option lists from the wire', () => {
		expect(isBrowserChoices({ selected: 0, groups: ['G'], options: [{ label: 'A', disabled: false, group: 0 }] })).toBe(true);
		expect(isBrowserChoices({ selected: -1, groups: [], options: [] })).toBe(true);
		expect(isBrowserChoices({ selected: 1, groups: [], options: [{ label: 'A', disabled: false }] })).toBe(false);
		expect(isBrowserChoices({ selected: 0, groups: [], options: [{ label: 'A', disabled: false, group: 0 }] })).toBe(false);
		expect(isBrowserChoices({ selected: 0, groups: [], options: [{ label: 3, disabled: false }] })).toBe(false);
		expect(isBrowserChoices(null)).toBe(false);
	});

	it('moves past disabled options and stops at the ends', () => {
		const list = opts(['Off', true], 'A', ['Off', true], 'B', 'C', ['Off', true]);
		expect(moveChoice(list, 1, 1)).toBe(3);
		expect(moveChoice(list, 3, -1)).toBe(1);
		expect(moveChoice(list, 1, -1)).toBe(1);
		expect(moveChoice(list, 4, 1)).toBe(4);
		expect(moveChoice(list, -1, 1)).toBe(1); // nothing active yet: first enabled
		expect(moveChoice(list, 1, 10)).toBe(4); // a page jump lands on the last enabled
		expect(moveChoice(list, 4, -10)).toBe(1);
		expect(edgeChoice(list, 'first')).toBe(1);
		expect(edgeChoice(list, 'last')).toBe(4);
		expect(edgeChoice(opts(['x', true]), 'first')).toBe(-1);
	});

	it('type-ahead finds a prefix, cycles on a repeated letter and skips disabled options', () => {
		const list = opts('Austria', 'Belgium', ['Bhutan', true], 'Bolivia', 'Brazil', 'Égypte');
		expect(typeAheadMatch(list, 'b', 0)).toBe(1);
		expect(typeAheadMatch(list, 'b', 1)).toBe(3); // same letter again: the next B
		expect(typeAheadMatch(list, 'bb', 4)).toBe(1); // wraps around
		expect(typeAheadMatch(list, 'bra', 0)).toBe(4);
		expect(typeAheadMatch(list, 'eg', 0)).toBe(5); // accents fold
		expect(typeAheadMatch(list, 'zz', 0)).toBe(-1);
		const typed = new TypeAhead(500);
		expect(typed.push('b', 1000)).toBe('b');
		expect(typed.push('r', 1200)).toBe('br');
		expect(typed.push('a', 1900)).toBe('a'); // paused too long: a new search
	});

	it('filters by any part of the label', () => {
		const choices = { selected: 0, groups: [], options: opts('United States', 'United Kingdom', 'Réunion') };
		expect(matchingChoices(choices, 'king')).toEqual([1]);
		expect(matchingChoices(choices, 'reu')).toEqual([2]);
		expect(matchingChoices(choices, '  ')).toEqual([0, 1, 2]);
	});
});

describe('date and time values', () => {
	it('parses and formats every kind in its own unit', () => {
		expect(parseValue('date', '1970-01-02')).toBe(1);
		expect(formatValue('date', daysFromCivil(2026, 10, 21))).toBe('2026-10-21');
		expect(parseValue('date', '2026-02-30')).toBeNull();
		expect(parseValue('date', 'nope')).toBeNull();
		expect(parseValue('month', '1971-02')).toBe(13);
		expect(formatValue('month', 13)).toBe('1971-02');
		expect(parseValue('time', '13:45')).toBe((13 * 60 + 45) * 60_000);
		expect(parseValue('time', '13:45:30.250')).toBe((13 * 3600 + 45 * 60 + 30) * 1000 + 250);
		expect(parseValue('time', '24:00')).toBeNull();
		expect(formatValue('time', (13 * 60 + 45) * 60_000)).toBe('13:45');
		expect(formatValue('time', (13 * 3600 + 45 * 60 + 5) * 1000)).toBe('13:45:05');
		expect(formatValue('time', 5, 'second')).toBe('00:00:00.005');
		expect(formatValue('time', 0, 'second')).toBe('00:00:00');
		const at = parseValue('datetime-local', '2026-10-21T08:30')!;
		expect(at).toBe(daysFromCivil(2026, 10, 21) * DAY + 8.5 * 3_600_000);
		expect(formatValue('datetime-local', at)).toBe('2026-10-21T08:30');
		expect(parseValue('datetime-local', '2026-10-21 08:30')).toBe(at); // a space is valid too
	});

	it('maps ISO weeks, including 53-week years and the year edge', () => {
		expect(parseValue('week', '1970-W01')).toBe(0);
		expect(formatValue('week', 0)).toBe('1970-W01');
		expect(formatValue('week', parseValue('week', '2026-W43')!)).toBe('2026-W43');
		expect(parseValue('week', '2025-W53')).toBeNull(); // 2025 has 52 weeks
		expect(formatValue('week', parseValue('week', '2026-W53')!)).toBe('2026-W53'); // starts on a Thursday
		// 2024-12-30 (a Monday) belongs to 2025-W01.
		expect(formatValue('week', weekIndexOf(daysFromCivil(2024, 12, 30)))).toBe('2025-W01');
		expect(weekIndexOf(daysFromCivil(2025, 1, 5))).toBe(weekIndexOf(daysFromCivil(2024, 12, 30)));
	});

	it('round-trips civil dates and builds a six-week grid from the first weekday', () => {
		expect(civilFromDays(daysFromCivil(2026, 10, 21))).toMatchObject({ year: 2026, month: 10, day: 21, weekday: 3 });
		const sunday = monthGrid(2026, 10, 0);
		expect(sunday).toHaveLength(42);
		expect(civilFromDays(sunday[0])).toMatchObject({ month: 9, day: 27, weekday: 0 });
		const monday = monthGrid(2026, 10, 1);
		expect(civilFromDays(monday[0])).toMatchObject({ month: 9, day: 28, weekday: 1 });
	});

	it('reads the first day of the week from the locale', () => {
		expect(firstDayOfWeek('en-US')).toBe(0);
		expect(firstDayOfWeek('de-DE')).toBe(1);
	});
});

describe('min, max and step', () => {
	it('applies date min/max and a day step from min', () => {
		const l = limitsFor('date', '2026-10-05', '2026-10-31', '7');
		const d = (n: number) => daysFromCivil(2026, 10, n);
		expect(allowed(l, d(5))).toBe(true);
		expect(allowed(l, d(12))).toBe(true);
		expect(allowed(l, d(13))).toBe(false);
		expect(allowed(l, d(4))).toBe(false);
		expect(allowed(l, daysFromCivil(2026, 11, 2))).toBe(false);
		expect(nearestAllowed(l, d(14))).toBe(d(12));
		expect(nearestAllowed(l, d(1))).toBe(d(5));
		expect(nearestAllowed(l, d(31) + 30)).toBe(d(26));
	});

	it('uses the default step and base for each kind', () => {
		expect(limitsFor('time').step).toBe(60_000);
		expect(limitsFor('datetime-local').step).toBe(60_000);
		expect(limitsFor('date').step).toBe(1);
		expect(limitsFor('time', undefined, undefined, 'any').step).toBeNull();
		expect(limitsFor('time', undefined, undefined, '-5').step).toBe(60_000);
		expect(limitsFor('time', undefined, undefined, '0.5').step).toBe(500);
		expect(limitsFor('month', undefined, undefined, '2.6').step).toBe(3);
		expect(limitsFor('date', 'garbage').min).toBeNull();
		expect(allowed(limitsFor('time'), 61_000)).toBe(false); // seconds need a step
	});

	it('finds whether a range holds any allowed value (time columns, calendar days)', () => {
		const l = limitsFor('time', '09:00', '17:30', '1800');
		const h = (n: number) => n * 3_600_000;
		expect(anyAllowedIn(l, h(8), h(9))).toBe(false);
		expect(anyAllowedIn(l, h(9), h(10))).toBe(true);
		expect(anyAllowedIn(l, h(17), h(18))).toBe(true);
		expect(anyAllowedIn(l, h(18), h(19))).toBe(false);
		expect(anyAllowedIn(l, h(9) + 60_000, h(9) + 120_000)).toBe(false); // 09:01 is off-step
		const dt = limitsFor('datetime-local', '2026-10-21T10:00', '2026-10-23T12:00');
		const day = (n: number) => daysFromCivil(2026, 10, n) * DAY;
		expect(anyAllowedIn(dt, day(20), day(21))).toBe(false);
		expect(anyAllowedIn(dt, day(23), day(24))).toBe(true);
		expect(anyAllowedIn(dt, day(24), day(25))).toBe(false);
	});

	it('treats a time range with min after max as wrapping past midnight', () => {
		const l = limitsFor('time', '22:00', '02:00');
		expect(l.wraps).toBe(true);
		expect(allowed(l, 23 * 3_600_000)).toBe(true);
		expect(allowed(l, 3_600_000)).toBe(true);
		expect(allowed(l, 12 * 3_600_000)).toBe(false);
		expect(anyAllowedIn(l, 12 * 3_600_000, 13 * 3_600_000)).toBe(false);
		expect(anyAllowedIn(l, 0, 3_600_000)).toBe(true);
	});
});

describe('popup placement', () => {
	const view = { width: 800, height: 600 };
	it('opens below the field when it fits, above when there is more room there', () => {
		expect(placePopup({ left: 100, top: 100, width: 200, height: 30 }, view, { width: 260, height: 300 }))
			.toEqual({ left: 100, top: 134, maxHeight: 300, placement: 'below', width: 260 });
		const above = placePopup({ left: 100, top: 500, width: 200, height: 30 }, view, { width: 260, height: 300 });
		expect(above).toMatchObject({ placement: 'above', top: 196, maxHeight: 300 });
	});

	it('stays inside the view horizontally and caps its height to the room left', () => {
		const p = placePopup({ left: 700, top: 250, width: 90, height: 30 }, view, { width: 260, height: 500 });
		expect(p.left).toBe(800 - 260 - 8);
		expect(p.maxHeight).toBeLessThan(500);
		expect(p.top + p.maxHeight).toBeLessThanOrEqual(600 - 8);
		// At least as wide as the field, never wider than the view.
		expect(placePopup({ left: 0, top: 0, width: 400, height: 30 }, view, { width: 260, height: 100 }).width).toBe(400);
		expect(placePopup({ left: 0, top: 0, width: 400, height: 30 }, { width: 300, height: 600 }, { width: 260, height: 100 }).width).toBe(284);
	});
});
