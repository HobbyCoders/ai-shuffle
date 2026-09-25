/**
 * rate-limit store — edge readings the usage panel renders: display rounding
 * at 0/100, zone-less reset timestamps, rollover against a moving clock,
 * provider rows that age while persisted, and `providers: null`.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
	rateLimit,
	parseResetMs,
	effectivePct,
	reportedRemainingPct,
	remainingFromUsedPct,
	formatCountdown,
	providerStatusAt,
	type UsageWindow,
} from './rate-limit';

const T0 = Date.UTC(2026, 8, 25, 12, 0, 0);

function win(utilization: number, resets_at: string | null = null): UsageWindow {
	return { type: 'five_hour', utilization, resets_at, received_at: T0, source: 'poll' };
}

beforeEach(() => {
	rateLimit.__resetForTests();
	vi.useFakeTimers({ toFake: ['Date'] });
	vi.setSystemTime(T0);
});
afterEach(() => vi.useRealTimers());

describe('display rounding at the edges', () => {
	it.each([
		[0, 0],
		[0.4, 1],
		[0.5, 1],
		[42.4, 42],
		[99.4, 99],
		[99.6, 99],
		[100, 100],
	])('remaining %s%% shows %s%% left', (remaining, shown) => {
		expect(reportedRemainingPct(win(remaining), T0)).toBe(shown);
	});

	it.each([
		[0, 100],
		[0.3, 99],
		[99.7, 1],
		[100, 0],
		[130, 0],
	])('used %s%% shows %s%% left', (used, shown) => {
		expect(remainingFromUsedPct(win(used), T0)).toBe(shown);
	});

	it('used-quota providers never read 0% while something was used', () => {
		expect(effectivePct(win(0.2), T0)).toBe(1);
		expect(effectivePct(win(99.8), T0)).toBe(99);
		expect(effectivePct(win(100), T0)).toBe(100);
	});
});

describe('reset timestamps', () => {
	it('reads a zone-less ISO reset as UTC, like the backend wrote it', () => {
		// Pin a zone west of UTC so this holds on a UTC CI runner too (Node
		// applies a runtime TZ change to Date).
		const tz = process.env.TZ;
		process.env.TZ = 'America/Los_Angeles';
		try {
			expect(new Date(2026, 8, 25).getTimezoneOffset()).toBe(420);
			expect(parseResetMs('2026-09-25T15:00:00')).toBe(Date.UTC(2026, 8, 25, 15));
		} finally {
			if (tz === undefined) delete process.env.TZ;
			else process.env.TZ = tz;
		}
		expect(parseResetMs('2026-09-25 15:00:00.250')).toBe(Date.UTC(2026, 8, 25, 15, 0, 0, 250));
		expect(parseResetMs('2026-09-25T15:00:00+02:00')).toBe(Date.UTC(2026, 8, 25, 13));
		expect(parseResetMs('2026-09-25T15:00:00Z')).toBe(Date.UTC(2026, 8, 25, 15));
		expect(parseResetMs('1790000000')).toBe(1_790_000_000_000);
		expect(parseResetMs('1790000000000')).toBe(1_790_000_000_000);
		expect(parseResetMs('soon')).toBeNull();
	});

	it('counts down in UTC and never shows a negative countdown after the reset', () => {
		const reset = '2026-09-25T15:00:00';
		expect(formatCountdown(reset, Date.now())).toBe('3h 0m');
		vi.setSystemTime(Date.UTC(2026, 8, 25, 15, 0, 1));
		expect(formatCountdown(reset, Date.now())).toBe('');
		// Rolled over: the window reads as a fresh quota until the next report.
		expect(reportedRemainingPct(win(3, reset), Date.now())).toBe(100);
		expect(remainingFromUsedPct(win(97, reset), Date.now())).toBe(100);
		expect(effectivePct(win(97, reset), Date.now())).toBe(0);
	});
});

describe('provider status as of now', () => {
	it('ages a fresh row past its source interval', () => {
		const pb = { status: 'fresh' as const, fetchedAt: T0, sourceInfo: null };
		expect(providerStatusAt(pb, T0 + 300_000)).toBe('fresh');
		expect(providerStatusAt(pb, T0 + 300_001)).toBe('stale');
	});

	it('uses the source interval when the registry reports one', () => {
		const info = { min_interval_seconds: 900 } as never;
		const pb = { status: 'fresh' as const, fetchedAt: T0, sourceInfo: info };
		expect(providerStatusAt(pb, T0 + 600_000)).toBe('fresh');
		expect(providerStatusAt(pb, T0 + 900_001)).toBe('stale');
	});

	it('leaves other statuses alone', () => {
		for (const status of ['stale', 'unavailable', 'never_fetched', undefined] as const) {
			expect(providerStatusAt({ status, fetchedAt: T0 - 86_400_000, sourceInfo: null }, T0)).toBe(status);
		}
		expect(providerStatusAt({ status: 'fresh', fetchedAt: null, sourceInfo: null }, T0)).toBe('fresh');
	});

	it('a row restored from storage after a failed refresh is not fresh', () => {
		rateLimit.applySnapshot({
			windows: [], available: false,
			providers: [{ provider_id: 'cx', provider_type: 'chatgpt', status: 'fresh', fetched_at: T0 }],
		});
		vi.setSystemTime(T0 + 2 * 3_600_000);
		const pb = rateLimit.current.providerBuckets.cx;
		expect(pb.status).toBe('fresh');
		expect(providerStatusAt(pb, Date.now())).toBe('stale');
	});
});

describe('providers null vs empty', () => {
	const rows = [
		{ provider_id: 'a', status: 'fresh' as const, fetched_at: T0, windows: [{ type: 'five_hour', utilization: 10 }] },
		{ provider_id: 'b', status: 'unavailable' as const, last_error_kind: 'timeout' as const },
	];

	it('null keeps every row, an error row included', () => {
		rateLimit.applySnapshot({ windows: [], available: false, providers: rows });
		rateLimit.applySnapshot({ windows: [], available: false, providers: null, provider_refreshing: true });
		expect(Object.keys(rateLimit.current.providerBuckets).sort()).toEqual(['a', 'b']);
		expect(rateLimit.current.providerBuckets.a.windows.five_hour.utilization).toBe(10);
	});

	it('an empty list means no providers', () => {
		rateLimit.applySnapshot({ windows: [], available: false, providers: rows });
		rateLimit.applySnapshot({ windows: [], available: false, providers: [] });
		expect(rateLimit.current.providerBuckets).toEqual({});
	});

	it('windows without their own observation are dated by the row, not by now', () => {
		rateLimit.applySnapshot({ windows: [], available: false, providers: [
			{ provider_id: 'a', fetched_at: T0 - 60_000, windows: [{ type: 'five_hour', utilization: 10 }] },
		] });
		expect(rateLimit.current.providerBuckets.a.windows.five_hour.received_at).toBe(T0 - 60_000);
	});
});

describe('rejected live events', () => {
	const bucket = () => rateLimit.current.buckets['__self__'];

	it('marks the window exhausted without a utilization, dated now', () => {
		rateLimit.applyLiveEvent({ rate_limit_type: 'five_hour', status: 'rejected', resets_at: '1900000000' });
		const w = bucket().windows.five_hour;
		expect(w.utilization).toBe(0);
		expect(w.received_at).toBe(T0);
		expect(w.resets_at).toBe('1900000000');
		expect(reportedRemainingPct(w, T0)).toBe(0);
	});

	it('overrides a utilization that still shows headroom', () => {
		rateLimit.applyLiveEvent({ rate_limit_type: 'seven_day', status: 'rejected', utilization: 0.3 });
		expect(bucket().windows.seven_day.utilization).toBe(0);
	});

	it('other verdicts without a utilization leave the windows alone', () => {
		rateLimit.applyLiveEvent({ rate_limit_type: 'five_hour', status: 'allowed', utilization: 0.25 });
		rateLimit.applyLiveEvent({ rate_limit_type: 'five_hour', status: 'allowed_warning', utilization: null });
		expect(bucket().windows.five_hour.utilization).toBe(75);
	});
});
