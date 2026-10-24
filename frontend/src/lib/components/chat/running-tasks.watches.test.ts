/** Watch rows for the running-tasks panel (#371 step 3). */
import { describe, it, expect } from 'vitest';
import {
	buildWatchRows,
	formatFired,
	formatWatchCost,
	visibleWatches,
	watchCostTitle,
	watchCount,
	watchKey,
	watchSummary
} from './running-tasks';

const base = { what: 'Inbox', last_fired_at: null, cost_usd: 0, can_pause: true, can_stop: true };

describe('buildWatchRows', () => {
	it('carries where a watch runs, and fences a row mid-move (#565)', () => {
		const [placed, fenced] = buildWatchRows([
			{ ...base, id: 'a', status: 'active', placement_device_id: 'dev_server', worker_id: 'ServerWorker' },
			{ ...base, id: 'b', status: 'paused', reason: 'Transfer pending', placement_device_id: 'dev_desktop', worker_id: 'transfer:dev_server', can_pause: false }
		]);
		expect(placed).toMatchObject({ placementDeviceId: 'dev_server', workerId: 'ServerWorker', transferTo: null, canPause: true, canStop: true });
		// A fenced row does not run; the server ignores Pause, Resume and Stop on it.
		expect(fenced).toMatchObject({
			state: 'paused',
			transferTo: 'dev_server',
			workerId: null,
			reason: null,
			canPause: false,
			canResume: false,
			canStop: false
		});
	});

	it('keeps active and paused watches in server order and drops stopped ones', () => {
		const rows = buildWatchRows([
			{ ...base, id: 'a', status: 'active' },
			{ ...base, id: 'b', status: 'stopped', can_pause: false, can_stop: false },
			{ ...base, id: 'c', status: 'paused', can_pause: false, reason: 'Fired too often' }
		]);
		expect(rows.map((r) => [r.id, r.state])).toEqual([['a', 'active'], ['c', 'paused']]);
		expect(rows[0]).toMatchObject({ canPause: true, canResume: false, canStop: true, reason: null });
		expect(rows[1]).toMatchObject({ canPause: false, canResume: true, canStop: true, reason: 'Fired too often' });
	});

	it('keeps a watch stopped here as Stopped, with no actions', () => {
		const [row] = buildWatchRows([{ ...base, id: 'a', status: 'stopped', can_stop: false }], { stoppedHere: new Set(['a']) });
		expect(row).toMatchObject({ state: 'stopped', canPause: false, canResume: false, canStop: false });
	});

	it('marks a stop in flight and ignores a failure reason while active', () => {
		const [row] = buildWatchRows([{ ...base, id: 'a', status: 'active', reason: 'timeout' }], { stopping: new Set([watchKey('a')]) });
		expect(row.state).toBe('stopping');
		expect(row.reason).toBeNull();
		expect(row.canStop).toBe(false);
	});

	it('falls back to the description, and reads cost, budget and the reporting chat', () => {
		const [row] = buildWatchRows([
			{ id: 'a', status: 'active', what: '  ', description: 'GitHub CI on main', last_fired_at: '2026-10-01T00:00:00Z', cost_usd: 1.5, daily_budget_usd: 2, creating_session_id: 's1' }
		]);
		expect(row).toMatchObject({ name: 'GitHub CI on main', costUsd: 1.5, dailyBudgetUsd: 2, sessionId: 's1', lastFiredAt: Date.parse('2026-10-01T00:00:00Z') });
	});
});

describe('watch labels', () => {
	const now = Date.parse('2026-10-24T12:00:00Z');
	it('says when it last fired', () => {
		expect(formatFired(null, now)).toBe('Not fired yet');
		expect(formatFired(now - 10_000, now)).toBe('Fired just now');
		expect(formatFired(now - 5 * 60_000, now)).toBe('Fired 5m ago');
		expect(formatFired(now - 3 * 3600_000, now)).toBe('Fired 3h ago');
		expect(formatFired(now - 50 * 3600_000, now)).toBe('Fired 2d ago');
	});

	it('formats cost and its tooltip', () => {
		expect(formatWatchCost(0)).toBe('$0.00');
		expect(formatWatchCost(0.004)).toBe('<$0.01');
		expect(formatWatchCost(1.234)).toBe('$1.23');
		expect(watchCostTitle({ costUsd: 0.42, dailyBudgetUsd: null })).toBe('$0.42 spent in total');
		expect(watchCostTitle({ costUsd: 0.42, dailyBudgetUsd: 1 })).toBe('$0.42 spent in total · daily cap $1.00');
	});

	it('counts watches, naming the paused ones', () => {
		expect(watchCount(1)).toBe('1 watch');
		expect(watchCount(3)).toBe('3 watches');
		const rows = buildWatchRows([
			{ ...base, id: 'a', status: 'active' },
			{ ...base, id: 'b', status: 'paused' }
		]);
		expect(watchSummary(rows)).toBe('2 watches · 1 paused');
		expect(watchSummary([])).toBe('');
	});

	it('shows watches under All and Watches only, and searches their names', () => {
		const rows = buildWatchRows([{ ...base, id: 'a', status: 'active', what: 'Mail from Alex' }]);
		expect(visibleWatches(rows, 'all', '')).toHaveLength(1);
		expect(visibleWatches(rows, 'watches', 'alex')).toHaveLength(1);
		expect(visibleWatches(rows, 'watches', 'bob')).toHaveLength(0);
		expect(visibleWatches(rows, 'agents', '')).toHaveLength(0);
		expect(visibleWatches(rows, 'shell', '')).toHaveLength(0);
	});
});
