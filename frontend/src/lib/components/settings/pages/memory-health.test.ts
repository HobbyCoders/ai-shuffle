import { describe, expect, it } from 'vitest';
import { memoryChecked, memoryCheckedAt, memoryRows, memorySummary, type MemoryHealth } from './memory-health';

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

	it('reads "Needs attention", never "0 need attention", when no check is listed', () => {
		const bare: MemoryHealth = { state: 'attention', checked_at: now, signals: [], repairs: [] };
		expect(memorySummary(bare)).toBe('Needs attention');
		// Only healthy signals listed: the same.
		expect(memorySummary({ ...bare, signals: [health.signals[1]] })).toBe('Needs attention');
		// Missing list from an older worker.
		expect(memorySummary({ ...bare, signals: undefined as unknown as MemoryHealth['signals'] })).toBe('Needs attention');
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
});
