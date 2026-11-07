import { afterEach, describe, expect, it } from 'vitest';
import { daysInRange, fillDays, formatScale, labelIndexes, shiftDay, spendScale, type SpendDay } from './statsChart';

describe('spendScale', () => {
	it('rounds the top up to three even steps', () => {
		expect(spendScale(5.4)).toEqual({ top: 6, ticks: [2, 4, 6] });
		expect(spendScale(3)).toEqual({ top: 3, ticks: [1, 2, 3] });
		expect(spendScale(7)).toEqual({ top: 7.5, ticks: [2.5, 5, 7.5] });
		expect(spendScale(0.12)).toEqual({ top: 0.15, ticks: [0.05, 0.1, 0.15] });
		expect(spendScale(2400).top).toBe(3000);
	});
	it('keeps a scale for an empty range', () => {
		expect(spendScale(0)).toEqual({ top: 3, ticks: [1, 2, 3] });
	});
	it('never sits below the highest bar', () => {
		for (const max of [0.01, 0.9, 1.01, 4.2, 13, 99.9, 1234]) expect(spendScale(max).top).toBeGreaterThanOrEqual(max);
	});
});

describe('formatScale', () => {
	it('shows cents only when the step needs them', () => {
		expect(formatScale(2)).toBe('$2');
		expect(formatScale(2.5)).toBe('$2.50');
		expect(formatScale(0.05)).toBe('$0.05');
		expect(formatScale(1000)).toBe('$1,000');
	});
});

describe('labelIndexes', () => {
	it('labels first, last and evenly between, 3 to 5 in all', () => {
		expect(labelIndexes(31)).toEqual([0, 10, 20, 30]);
		expect(labelIndexes(7)).toEqual([0, 2, 4, 6]);
		expect(labelIndexes(31, 3)).toEqual([0, 15, 30]);
		expect(labelIndexes(91)).toEqual([0, 30, 60, 90]);
		expect(labelIndexes(5)).toEqual([0, 1, 2, 3, 4]);
		expect(labelIndexes(0)).toEqual([]);
	});
	it('always includes both ends', () => {
		for (const n of [6, 7, 8, 14, 30, 31, 90, 91]) {
			const ix = labelIndexes(n);
			expect(ix[0]).toBe(0);
			expect(ix.at(-1)).toBe(n - 1);
			expect(ix.length).toBeGreaterThanOrEqual(3);
			expect(ix.length).toBeLessThanOrEqual(5);
		}
	});
});

const spend = (date: string, cost: number, extra: Partial<SpendDay> = {}): SpendDay => ({ date, input_tokens: 100, output_tokens: 10, cost, estimated: false, ...extra });

describe('daysInRange and shiftDay', () => {
	it('includes both range edges, one bar per calendar day', () => {
		expect(daysInRange('2026-11-01', '2026-11-04')).toEqual(['2026-11-01', '2026-11-02', '2026-11-03', '2026-11-04']);
		expect(daysInRange('2026-11-07', '2026-11-07')).toEqual(['2026-11-07']);
		// The Stats presets: today back 7 / 30 / 90 days, both ends included.
		for (const back of [7, 30, 90]) expect(daysInRange(shiftDay('2026-11-07', -back), '2026-11-07')).toHaveLength(back + 1);
	});
	it('crosses month, year and leap-day edges', () => {
		expect(daysInRange('2028-02-27', '2028-03-01')).toEqual(['2028-02-27', '2028-02-28', '2028-02-29', '2028-03-01']);
		expect(daysInRange('2026-12-30', '2027-01-02')).toEqual(['2026-12-30', '2026-12-31', '2027-01-01', '2027-01-02']);
		expect(shiftDay('2026-03-01', -1)).toBe('2026-02-28');
		expect(shiftDay('2027-01-01', -30)).toBe('2026-12-02');
	});
	it('returns nothing for a backwards, unreadable or runaway range', () => {
		expect(daysInRange('2026-11-08', '2026-11-07')).toEqual([]);
		expect(daysInRange('', '2026-11-07')).toEqual([]);
		expect(daysInRange('2026-11-01', 'soon')).toEqual([]);
		expect(daysInRange('1990-01-01', '2026-11-07')).toEqual([]);
	});
});

describe('fillDays', () => {
	it('gives every day of the range a bar, zero on days without usage, with the range end last', () => {
		const filled = fillDays([spend('2026-11-02', 1.5), spend('2026-11-05', 0.25, { estimated: true })], '2026-11-01', '2026-11-07');
		expect(filled.map((d) => d.date)).toEqual(['2026-11-01', '2026-11-02', '2026-11-03', '2026-11-04', '2026-11-05', '2026-11-06', '2026-11-07']);
		expect(filled.map((d) => d.cost)).toEqual([0, 1.5, 0, 0, 0.25, 0, 0]);
		expect(filled[0]).toEqual({ date: '2026-11-01', input_tokens: 0, output_tokens: 0, cost: 0, estimated: false });
		expect(filled[4].estimated).toBe(true);
		// Today had no usage and is still the last bar.
		expect(filled.at(-1)?.date).toBe('2026-11-07');
	});
	it('keeps the first and last day of the range and drops points outside it', () => {
		const filled = fillDays([spend('2026-10-31', 9), spend('2026-11-01', 1), spend('2026-11-03', 2), spend('2026-11-04', 9)], '2026-11-01', '2026-11-03');
		expect(filled.map((d) => [d.date, d.cost])).toEqual([['2026-11-01', 1], ['2026-11-02', 0], ['2026-11-03', 2]]);
	});
	it('reads timestamp-shaped days and adds two points on one day together', () => {
		const filled = fillDays([spend('2026-11-02T00:00:00', 1), spend('2026-11-02 00:00:00+00', 0.5, { estimated: true }), spend('2026-11-01', 2)], '2026-11-01', '2026-11-02');
		expect(filled).toEqual([
			{ date: '2026-11-01', input_tokens: 100, output_tokens: 10, cost: 2, estimated: false },
			{ date: '2026-11-02', input_tokens: 200, output_tokens: 20, cost: 1.5, estimated: true }
		]);
	});
	it('keeps the points as they came when the range cannot be read', () => {
		const points = [spend('2026-11-02', 1)];
		expect(fillDays(points, '', '')).toEqual(points);
	});
	it('a range with no usage is all zeros, which the card shows as its empty state', () => {
		expect(fillDays([], '2026-11-01', '2026-11-07').every((d) => d.cost === 0 && d.input_tokens === 0)).toBe(true);
	});
});

describe('day arithmetic in any local time zone', () => {
	const original = process.env.TZ;
	afterEach(() => { process.env.TZ = original; });
	// Each zone crosses a DST change inside the range (US: 8 Mar 2026, NZ: 5 Apr
	// 2026, EU: 25 Oct 2026); a local-midnight loop would repeat or skip a day.
	const cases: [string, string, string, string][] = [
		['America/Los_Angeles', '2026-03-01', '2026-03-15', '2026-03-08'],
		['Pacific/Auckland', '2026-03-30', '2026-04-10', '2026-04-05'],
		['Europe/Berlin', '2026-10-20', '2026-10-30', '2026-10-25'],
		['Asia/Kolkata', '2026-10-20', '2026-10-30', '2026-10-25'],
		['UTC', '2026-03-01', '2026-03-15', '2026-03-08']
	];
	it.each(cases)('%s', (zone, start, end, change) => {
		process.env.TZ = zone;
		const expected = (() => { const out: string[] = []; for (let ms = Date.parse(start); ms <= Date.parse(end); ms += 864e5) out.push(new Date(ms).toISOString().slice(0, 10)); return out; })();
		expect(daysInRange(start, end)).toEqual(expected);
		expect(new Set(daysInRange(start, end)).size).toBe(expected.length);
		const filled = fillDays([spend(change, 3), spend(end, 1)], start, end);
		expect(filled.map((d) => d.date)).toEqual(expected);
		expect(filled.find((d) => d.date === change)?.cost).toBe(3);
		expect(filled.at(-1)).toMatchObject({ date: end, cost: 1 });
		expect(shiftDay(end, -(expected.length - 1))).toBe(start);
	});
	it('the zone switch really applies (guards the cases above)', () => {
		process.env.TZ = 'Pacific/Auckland';
		const nz = new Date(2026, 0, 15, 12).getTimezoneOffset();
		process.env.TZ = 'America/Los_Angeles';
		expect(new Date(2026, 0, 15, 12).getTimezoneOffset()).not.toBe(nz);
	});
});
