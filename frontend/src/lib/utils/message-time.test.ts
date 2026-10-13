import { describe, it, expect } from 'vitest';
import { formatMessageTime } from './message-time';

// Regression guard for "timestamps don't persist": a stored ISO timestamp
// must format to ITS time, not the current time.
describe('formatMessageTime', () => {
	const expected = (d: Date) =>
		d.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', hour12: true });

	it('formats a persisted ISO-8601 string (with UTC offset) to its own time', () => {
		const iso = '2026-01-15T14:30:00+00:00';
		expect(formatMessageTime(iso)).toBe(expected(new Date(iso)));
	});

	it('formats a Z-suffixed ISO string (JSONL timestamps)', () => {
		const iso = '2026-01-15T09:05:00Z';
		expect(formatMessageTime(iso)).toBe(expected(new Date(iso)));
	});

	it('does NOT show the current time for an old persisted timestamp', () => {
		const iso = '2020-03-01T03:04:00Z';
		const past = new Date(iso);
		const now = new Date();
		expect(formatMessageTime(iso)).toBe(expected(past));
		// Only meaningful when past and now differ at minute granularity
		// (avoids a 1-in-1440 flake if the wall clock matches hh:mm).
		if (expected(past) !== expected(now)) {
			expect(formatMessageTime(iso)).not.toBe(expected(now));
		}
	});

	it('accepts a Date object', () => {
		const d = new Date('2026-01-15T22:45:00Z');
		expect(formatMessageTime(d)).toBe(expected(d));
	});

	it('falls back to the current time when the timestamp is missing', () => {
		expect(formatMessageTime(undefined)).toBe(expected(new Date()));
		expect(formatMessageTime('')).toBe(expected(new Date()));
	});

	it('falls back to the current time for an unparseable string', () => {
		expect(formatMessageTime('not-a-date')).toBe(expected(new Date()));
	});
});
