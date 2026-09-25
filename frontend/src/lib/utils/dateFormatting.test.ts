/**
 * parseUTC / formatDateTime — naive backend stamps are UTC, explicit
 * designators are honoured, and formatting follows the timezone preference.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';

const tz = vi.hoisted(() => ({ zone: 'UTC' }));

vi.mock('$lib/stores/timezonePreference.svelte', () => ({
	timezonePreference: { resolveTimezone: () => tz.zone }
}));

import { parseUTC, formatDateTime } from './dateFormatting';

beforeEach(() => {
	tz.zone = 'UTC';
});

describe('parseUTC', () => {
	it.each([
		['2026-09-25T10:30:00', '2026-09-25T10:30:00.000Z'],
		['2026-09-25 10:30:00', '2026-09-25T10:30:00.000Z'],
		['2026-09-25T10:30:00.123456', '2026-09-25T10:30:00.123Z'],
		['2026-09-25T10:30:00Z', '2026-09-25T10:30:00.000Z'],
		['2026-09-25T10:30:00z', '2026-09-25T10:30:00.000Z'],
		['2026-09-25T10:30:00+00:00', '2026-09-25T10:30:00.000Z'],
		['2026-09-25T10:30:00+05:30', '2026-09-25T05:00:00.000Z'],
		['2026-09-25T10:30:00-0400', '2026-09-25T14:30:00.000Z'],
		['2026-09-25 10:30:00+00', '2026-09-25T10:30:00.000Z'],
		['2026-09-25', '2026-09-25T00:00:00.000Z']
	])('%s -> %s', (input, iso) => {
		expect(parseUTC(input).toISOString()).toBe(iso);
	});

	it('does not treat a naive stamp as local time', () => {
		// Whatever TZ the test runner is in, the naive stamp means UTC.
		expect(parseUTC('2026-01-01T00:00:00').getTime()).toBe(Date.UTC(2026, 0, 1));
	});
});

describe('formatDateTime', () => {
	it('returns "Never" for empty input', () => {
		expect(formatDateTime(null)).toBe('Never');
		expect(formatDateTime('')).toBe('Never');
	});

	it('formats in the preferred timezone, not the runner zone', () => {
		const stamp = '2026-09-25T23:30:00';
		tz.zone = 'UTC';
		const utc = formatDateTime(stamp);
		tz.zone = 'Asia/Tokyo';
		const tokyo = formatDateTime(stamp);
		expect(utc).toBe(new Date(Date.UTC(2026, 8, 25, 23, 30)).toLocaleString(undefined, { timeZone: 'UTC' }));
		expect(tokyo).toBe(new Date(Date.UTC(2026, 8, 25, 23, 30)).toLocaleString(undefined, { timeZone: 'Asia/Tokyo' }));
		// Tokyo is UTC+9: the wall clock rolls over to the next day.
		expect(tokyo).not.toBe(utc);
		expect(tokyo).toMatch(/26/);
	});
});
