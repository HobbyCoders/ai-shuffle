import { describe, expect, it } from 'vitest';
import { memoryAlert, memoryChecked, memoryCheckedAt, memoryRows, memorySummary, type MemoryHealth } from './memory-health';

const now = 1_791_000_000;
const health: MemoryHealth = {
	state: 'attention',
	checked_at: now,
	signals: [
		{ id: 'snapshot', label: 'Session snapshot', state: 'attention', reason: 'Sessions see 26 of 170 facts in one project; the rest need Recall.' },
		{ id: 'saves', label: 'Saving', state: 'healthy', reason: 'Saves are writing normally.' }
	],
	repairs: [
		{ kind: 'duplicates', count: 2, at: now - 3600, summary: 'Archived 2 exact duplicates.' },
		{ kind: 'unconfirmed', count: 1, at: now - 10 * 86400, summary: 'Archived 1 unconfirmed fact nobody used.' }
	]
};

describe('memory health', () => {
	it('summarises the collapsed row', () => {
		expect(memorySummary(null)).toBe('Loading');
		expect(memorySummary(health)).toBe('Needs attention');
		expect(memorySummary({ ...health, signals: health.signals.map((s) => ({ ...s, state: 'attention' as const })) })).toBe(
			'2 need attention'
		);
		expect(memorySummary({ ...health, state: 'healthy' })).toBe('Healthy');
	});

	it('lists what needs attention, then this week\'s repairs', () => {
		const rows = memoryRows(health, now);
		expect(rows.map((r) => [r.name, r.tone])).toEqual([
			['Session snapshot', 'warn'],
			['Repaired', 'ok']
		]);
		expect(rows[0].text).toContain('26 of 170');
		expect(rows[1].text).toMatch(/^Archived 2 exact duplicates\./);
	});

	it('shows a healthy capacity note quietly instead of "nothing needs attention"', () => {
		const reason =
			'2 live facts; saves have room. Automatic cleanup has recorded 80 permanent deletions of older facts ' +
			'(80 at the archive limit; 0 on expiry). Some older deletion times are unknown.';
		const quiet: MemoryHealth = {
			state: 'healthy',
			checked_at: now,
			signals: [
				{ id: 'capacity', label: 'Capacity', state: 'healthy', note: true, reason },
				{ id: 'saves', label: 'Saving', state: 'healthy', note: false, reason: 'Saves are writing normally.' }
			],
			repairs: []
		};
		expect(memorySummary(quiet)).toBe('Healthy, 1 note');
		expect(memoryRows(quiet, now)).toEqual([{ id: 'note-capacity', name: 'Capacity', tone: 'off', text: reason }]);
		// An attention signal still comes first and is the only warning.
		const mixed = { ...health, signals: [...health.signals, quiet.signals[0]] };
		expect(memoryRows(mixed, now).map((r) => [r.name, r.tone])).toEqual([
			['Session snapshot', 'warn'],
			['Capacity', 'off'],
			['Repaired', 'ok']
		]);
		expect(memorySummary(mixed)).toBe('Needs attention');
	});

	it('says so when nothing needs attention', () => {
		const rows = memoryRows({ state: 'healthy', checked_at: now, signals: [], repairs: [] }, now);
		expect(rows).toEqual([{ id: 'ok', name: 'All checks', tone: 'ok', text: 'Nothing needs attention' }]);
	});

	it('says when the check last ran, relative in the hint and in full in the list (#1042)', () => {
		expect(memoryChecked(health, (now + 12 * 60) * 1000)).toBe('Checked 12m ago');
		expect(memoryChecked(health, (now + 3 * 86400) * 1000)).toBe('Checked 3d ago');
		expect(memoryChecked({ ...health, checked_at: null })).toBe('Not checked yet');
		// A value the page cannot read as seconds does not stop the row loading.
		expect(memoryChecked({ ...health, checked_at: '2026-11-10T02:20:00Z' as unknown as number })).toBe('Not checked yet');
		const full = new Date(now * 1000).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
		expect(memoryCheckedAt(health)).toBe(`Last checked ${full}`);
		expect(memoryCheckedAt({ ...health, checked_at: null })).toBe('');
	});

	it('puts the warning in one line for the collapsed row, and stays quiet when healthy (#1042)', () => {
		const loss =
			'Automatic cleanup has recorded 3 permanent deletions of older facts (3 at the archive limit; 0 on expiry). ' +
			'Some facts may be counted more than once. Recall cannot recover a deleted copy.';
		const capacity = { id: 'capacity', label: 'Capacity', state: 'attention' as const, reason: loss };
		const one: MemoryHealth = { ...health, signals: [capacity, health.signals[1]] };
		expect(memoryAlert(one)).toBe(
			'Capacity: Automatic cleanup has recorded 3 permanent deletions of older facts (3 at the archive limit; 0 on expiry).'
		);
		const two: MemoryHealth = { ...health, signals: [capacity, health.signals[0]] };
		expect(memoryAlert(two)).toMatch(/\(3 at the archive limit; 0 on expiry\)\. 1 more check needs attention\.$/);
		expect(memoryAlert({ ...health, signals: [capacity, health.signals[0], { ...capacity, id: 'saves', label: 'Saving' }] })).toMatch(
			/ 2 more checks need attention\.$/
		);
		expect(memoryAlert({ ...health, signals: [] })).toBe('Memory needs attention.');
		// An abbreviation does not end the sentence; a very long one is cut at a word.
		expect(memoryAlert({ ...health, signals: [{ ...capacity, reason: 'Cleanup removed approx. 3 facts. Later text.' }] })).toBe(
			'Capacity: Cleanup removed approx. 3 facts.'
		);
		const long = memoryAlert({ ...health, signals: [{ ...capacity, reason: 'word '.repeat(80).trim() }] });
		expect(long.endsWith('word…')).toBe(true);
		expect(long.length).toBeLessThan(200);
		// A healthy store, even one with a quiet note about past deletions, shows nothing.
		expect(memoryAlert({ ...health, state: 'healthy', signals: [{ ...capacity, state: 'healthy', note: true }] })).toBe('');
	});
});
