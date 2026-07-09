/**
 * rate-limit store — snapshot application semantics.
 *
 * Covers the account-switch correctness guarantees:
 *   - a polled snapshot REPLACES the window set (a window absent from the new
 *     account's snapshot must disappear, not linger from the old account);
 *   - an out-of-order snapshot from an OLDER generation is discarded so a slow
 *     in-flight poll can't overwrite the new account's fresher data;
 *   - `markStale()` / a fresh snapshot toggle the runtime `stale` flag.
 *
 * The store is a singleton; each test resets it via an ungenerationed snapshot
 * (which also resets the in-memory generation guard).
 */
import { beforeEach, describe, expect, it } from 'vitest';
import { rateLimit } from './rate-limit';

const future = () => new Date(Date.now() + 3_600_000).toISOString();

beforeEach(() => {
	// Ungenerationed snapshot → clears windows AND resets the generation guard.
	rateLimit.applySnapshot({ windows: [], available: true });
});

describe('rateLimit.applySnapshot', () => {
	it('replaces the window set (a dropped window disappears)', () => {
		rateLimit.applySnapshot({
			available: true,
			generation: 1,
			windows: [
				{ type: 'five_hour', utilization: 10, resets_at: future() },
				{ type: 'seven_day_overage_included', utilization: 55, resets_at: future() },
			],
		});
		expect(Object.keys(rateLimit.current.windows).sort()).toEqual([
			'five_hour',
			'seven_day_overage_included',
		]);

		// New account has no Fable weekly row — it must not linger.
		rateLimit.applySnapshot({
			available: true,
			generation: 2,
			windows: [{ type: 'five_hour', utilization: 3, resets_at: future() }],
		});
		expect(Object.keys(rateLimit.current.windows)).toEqual(['five_hour']);
		expect(rateLimit.current.windows.five_hour.utilization).toBe(3);
	});

	it('discards a snapshot from an older generation (out-of-order arrival)', () => {
		rateLimit.applySnapshot({
			available: true,
			generation: 5,
			windows: [{ type: 'five_hour', utilization: 20, resets_at: future() }],
		});
		// A slow poll for the OLD account completes late with a lower generation.
		rateLimit.applySnapshot({
			available: true,
			generation: 3,
			windows: [{ type: 'five_hour', utilization: 99, resets_at: future() }],
		});
		expect(rateLimit.current.windows.five_hour.utilization).toBe(20);
	});

	it('applies a newer-or-equal generation', () => {
		rateLimit.applySnapshot({
			available: true,
			generation: 3,
			windows: [{ type: 'five_hour', utilization: 20, resets_at: future() }],
		});
		rateLimit.applySnapshot({
			available: true,
			generation: 4,
			windows: [{ type: 'five_hour', utilization: 44, resets_at: future() }],
		});
		expect(rateLimit.current.windows.five_hour.utilization).toBe(44);
	});

	it('recovers after a backend restart (epoch-seeded generation applies)', () => {
		// Pre-restart: tab has seen some generation from the old process. The
		// backend seeds its counter from the epoch clock, so even the FIRST
		// post-restart snapshot must carry a generation greater than anything
		// the old process produced — the tab applies it and un-wedges.
		const preRestartGen = Math.floor(Date.now() / 1000) - 10_000;
		rateLimit.applySnapshot({
			available: true,
			generation: preRestartGen,
			windows: [{ type: 'five_hour', utilization: 20, resets_at: future() }],
		});
		rateLimit.markStale();

		const postRestartGen = Math.floor(Date.now() / 1000); // fresh epoch seed
		rateLimit.applySnapshot({
			available: true,
			generation: postRestartGen,
			windows: [{ type: 'five_hour', utilization: 7, resets_at: future() }],
		});
		expect(rateLimit.current.windows.five_hour.utilization).toBe(7);
		expect(rateLimit.current.stale).toBe(false);
	});

	it('markStale sets stale; a fresh available snapshot clears it', () => {
		rateLimit.applySnapshot({
			available: true,
			generation: 1,
			windows: [{ type: 'five_hour', utilization: 20, resets_at: future() }],
		});
		expect(rateLimit.current.stale).toBe(false);

		rateLimit.markStale();
		expect(rateLimit.current.stale).toBe(true);

		rateLimit.applySnapshot({
			available: true,
			generation: 2,
			windows: [{ type: 'five_hour', utilization: 5, resets_at: future() }],
		});
		expect(rateLimit.current.stale).toBe(false);
	});

	it('a discarded stale snapshot does NOT clear the stale flag', () => {
		rateLimit.applySnapshot({
			available: true,
			generation: 5,
			windows: [{ type: 'five_hour', utilization: 20, resets_at: future() }],
		});
		rateLimit.markStale();
		// Straggler from an older generation is dropped before touching state.
		rateLimit.applySnapshot({
			available: true,
			generation: 2,
			windows: [{ type: 'five_hour', utilization: 99, resets_at: future() }],
		});
		expect(rateLimit.current.stale).toBe(true);
		expect(rateLimit.current.windows.five_hour.utilization).toBe(20);
	});
});
