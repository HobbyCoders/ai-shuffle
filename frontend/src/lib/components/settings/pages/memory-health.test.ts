import { describe, expect, it } from 'vitest';
import { memoryRows, memorySummary, type MemoryHealth } from './memory-health';

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

	it('says so when nothing needs attention', () => {
		const rows = memoryRows({ state: 'healthy', checked_at: now, signals: [], repairs: [] }, now);
		expect(rows).toEqual([{ id: 'ok', name: 'All checks', tone: 'ok', text: 'Nothing needs attention' }]);
	});
});
