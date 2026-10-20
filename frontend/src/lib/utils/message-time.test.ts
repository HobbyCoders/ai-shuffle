import { describe, it, expect } from 'vitest';
import { formatMessageTime, formatMessageTimeFull, messageTimeISO } from './message-time';

// Local-time constructors keep these independent of the runner's time zone.
const NOW = new Date(2026, 9, 19, 16, 0); // Mon 19 Oct 2026, 4:00 PM local
const en = { now: NOW, locale: 'en-US' };

describe('formatMessageTime: the date with the time', () => {
	it('says "Today" with the time for a message from today', () => {
		expect(formatMessageTime(new Date(2026, 9, 19, 14, 30), en)).toBe('Today 2:30 PM');
	});

	it('says "Yesterday" for the previous calendar day, even under 24 hours ago', () => {
		expect(formatMessageTime(new Date(2026, 9, 18, 23, 55), en)).toBe('Yesterday 11:55 PM');
	});

	it('shows month and day (no year) for an older message this year', () => {
		expect(formatMessageTime(new Date(2026, 9, 14, 9, 5), en)).toBe('Oct 14, 9:05 AM');
	});

	it('adds the year for a message from another year', () => {
		expect(formatMessageTime(new Date(2025, 11, 31, 22, 45), en)).toBe('Dec 31, 2025, 10:45 PM');
	});

	it('follows the locale (24-hour clock and day-first order in en-GB)', () => {
		const gb = { now: NOW, locale: 'en-GB' };
		expect(formatMessageTime(new Date(2026, 9, 19, 14, 30), gb)).toBe('Today 14:30');
		expect(formatMessageTime(new Date(2026, 9, 14, 9, 5), gb)).toMatch(/^14 Oct, 0?9:05$/);
	});

	it('uses the locale’s own word for today, capitalised', () => {
		const label = formatMessageTime(new Date(2026, 9, 19, 14, 30), { now: NOW, locale: 'fr-FR' });
		expect(label.startsWith('Aujourd’hui')).toBe(true);
		expect(label).toContain('14:30');
	});

	// Regression guard for "timestamps don't persist": a stored ISO timestamp
	// must format to ITS date and time, not the current one.
	it('formats a persisted ISO-8601 string (with UTC offset) to its own date', () => {
		const iso = '2020-03-01T03:04:00+00:00';
		const expected = new Date(iso).toLocaleString('en-US', { month: 'short', day: 'numeric', year: 'numeric', hour: 'numeric', minute: '2-digit' });
		expect(formatMessageTime(iso, en)).toBe(expected);
		expect(formatMessageTime(iso, en)).toContain('2020');
	});

	it('falls back to the current time when the timestamp is missing or unparseable', () => {
		for (const missing of [undefined, '', 'not-a-date']) {
			expect(formatMessageTime(missing, { locale: 'en-US' })).toMatch(/^Today \d{1,2}:\d{2}\s?[AP]M$/);
		}
	});
});

describe('formatMessageTimeFull and messageTimeISO', () => {
	it('gives the long date and time for a hover title', () => {
		expect(formatMessageTimeFull(new Date(2026, 9, 14, 14, 30), { locale: 'en-US' })).toBe('Wednesday, October 14, 2026 at 2:30 PM');
	});

	it('gives an ISO value for <time datetime>', () => {
		expect(messageTimeISO('2026-01-15T09:05:00Z')).toBe('2026-01-15T09:05:00.000Z');
	});
});
