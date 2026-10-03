/**
 * rate-limit store — per-install bucket semantics.
 *
 * The 5-hour window is per Claude account, and in a clustered deployment each
 * install runs a different account. The store therefore keeps one bucket per
 * install. This suite covers:
 *   - modern `snapshots` payloads merge into the buckets map + set selfInstanceId;
 *   - a legacy/degraded (top-level windows only) payload folds NON-destructively
 *     into the current self bucket;
 *   - the generation guard is PER install (a straggler for one install can't
 *     clobber a fresher bucket);
 *   - live `rate_limit` events route by `instance_id`;
 *   - `resolveBucket` fallbacks (self for a null target, null for an unknown
 *     remote target — never borrow the local account's numbers);
 *   - `markStale` scopes to the self bucket only;
 *   - localStorage v1→v2 migration.
 *
 * The store is a singleton; each test resets it via `__resetForTests`, which
 * clears every bucket AND the module-level per-install generation high-water
 * marks (a plain legacy snapshot only clears the self key, leaking other keys).
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import {
	rateLimit,
	resolveBucket,
	resolveBucketKey,
	selfBucketKey,
	isBucketStale,
	bucketObservedAt,
	bucketHasUndatedWindow,
	BUCKET_STALE_MS,
	windowLabel,
	displayWindows,
	reportedRemainingPct,
	remainingFromUsedPct,
	effectivePct,
	isSelfTarget,
	SELF_BUCKET_KEY,
	claudeUsageSource,
	type ClaudeUsageSourceState,
	type UsageWindow,
} from './rate-limit';

const future = () => new Date(Date.now() + 3_600_000).toISOString();

beforeEach(() => {
	rateLimit.__resetForTests();
});

it('retains provider freshness metadata and cached credit timestamps independently of Claude', () => {
	rateLimit.applySnapshot({ windows: [], available: false, provider_refreshing: true, providers: [
		{ provider_id: 'api', status: 'stale', last_error_kind: 'missing_credentials', fetched_at: 123, credits: { balance_remaining: 42 } },
	] });
	expect(rateLimit.current.providerBuckets.api).toMatchObject({ status: 'stale', lastErrorKind: 'missing_credentials', fetchedAt: 123, credits: { balance_remaining: 42 } });
	rateLimit.applySnapshot({ windows: [], available: false });
	expect(rateLimit.current.providerBuckets.api.credits).toEqual({ balance_remaining: 42 });
});

it('keeps last-good provider rows while the backend cannot read them, with the cooldown', () => {
	rateLimit.applySnapshot({ windows: [], available: false, providers: [
		{ provider_id: 'or', status: 'fresh', fetched_at: 1_000, cooldown_until: 301_000, credits: { usage: 2 } },
	] });
	expect(rateLimit.current.providerBuckets.or).toMatchObject({ cooldownUntil: 301_000, credits: { usage: 2 } });
	// A failed read reports null (not []): the panel must not blank.
	rateLimit.applySnapshot({ windows: [], available: false, providers: null });
	expect(rateLimit.current.providerBuckets.or.credits).toEqual({ usage: 2 });
});

function selfBucket() {
	return resolveBucket(rateLimit.current, null);
}

describe('rateLimit.applySnapshot — legacy (top-level windows)', () => {
	it('folds top-level windows into the self bucket', () => {
		rateLimit.applySnapshot({
			available: true,
			windows: [{ type: 'five_hour', utilization: 42, resets_at: future() }],
		});
		expect(rateLimit.current.selfInstanceId).toBeNull();
		expect(Object.keys(rateLimit.current.buckets)).toEqual([SELF_BUCKET_KEY]);
		expect(selfBucket()?.windows.five_hour.utilization).toBe(42);
	});

	it('replaces the window set (a dropped window disappears)', () => {
		rateLimit.applySnapshot({
			available: true,
			generation: 1,
			windows: [
				{ type: 'five_hour', utilization: 10, resets_at: future() },
				{ type: 'seven_day_overage_included', utilization: 55, resets_at: future() },
			],
		});
		expect(Object.keys(selfBucket()!.windows).sort()).toEqual([
			'five_hour',
			'seven_day_overage_included',
		]);

		rateLimit.applySnapshot({
			available: true,
			generation: 2,
			windows: [{ type: 'five_hour', utilization: 3, resets_at: future() }],
		});
		expect(Object.keys(selfBucket()!.windows)).toEqual(['five_hour']);
		expect(selfBucket()!.windows.five_hour.utilization).toBe(3);
	});

	it('discards a snapshot from an older generation (out-of-order arrival)', () => {
		rateLimit.applySnapshot({
			available: true,
			generation: 5,
			windows: [{ type: 'five_hour', utilization: 20, resets_at: future() }],
		});
		rateLimit.applySnapshot({
			available: true,
			generation: 3,
			windows: [{ type: 'five_hour', utilization: 99, resets_at: future() }],
		});
		expect(selfBucket()!.windows.five_hour.utilization).toBe(20);
	});

	it('keeps last-known windows when a snapshot reports unavailable', () => {
		rateLimit.applySnapshot({
			available: true,
			windows: [{ type: 'five_hour', utilization: 30, resets_at: future() }],
		});
		rateLimit.applySnapshot({ available: false, windows: [] });
		expect(selfBucket()!.available).toBe(false);
		expect(selfBucket()!.windows.five_hour.utilization).toBe(30);
	});
});

describe('rateLimit.applySnapshot — modern (per-install snapshots)', () => {
	it('replaces buckets keyed by instance and sets selfInstanceId', () => {
		rateLimit.applySnapshot({
			available: true,
			windows: [],
			self_instance_id: 'primary',
			snapshots: [
				{
					instance_id: 'primary',
					device_name: 'Studio',
					available: true,
					fetched_at: Date.now(),
					windows: [{ type: 'five_hour', utilization: 12, resets_at: future() }],
				},
				{
					instance_id: 'worker',
					device_name: 'Worker',
					available: true,
					fetched_at: Date.now(),
					windows: [{ type: 'five_hour', utilization: 88, resets_at: future() }],
				},
			],
		});
		expect(rateLimit.current.selfInstanceId).toBe('primary');
		expect(Object.keys(rateLimit.current.buckets).sort()).toEqual(['primary', 'worker']);
		expect(resolveBucket(rateLimit.current, 'primary')!.windows.five_hour.utilization).toBe(12);
		expect(resolveBucket(rateLimit.current, 'worker')!.windows.five_hour.utilization).toBe(88);
		expect(selfBucketKey(rateLimit.current)).toBe('primary');
	});

	it('applies the generation guard PER install', () => {
		rateLimit.applySnapshot({
			available: true,
			windows: [],
			self_instance_id: 'primary',
			snapshots: [
				{ instance_id: 'primary', available: true, generation: 5, windows: [{ type: 'five_hour', utilization: 20, resets_at: future() }] },
				{ instance_id: 'worker', available: true, generation: 5, windows: [{ type: 'five_hour', utilization: 40, resets_at: future() }] },
			],
		});
		// A straggler where ONLY worker regressed: primary advances, worker's
		// stale entry is dropped and its prior bucket carries forward.
		rateLimit.applySnapshot({
			available: true,
			windows: [],
			self_instance_id: 'primary',
			snapshots: [
				{ instance_id: 'primary', available: true, generation: 6, windows: [{ type: 'five_hour', utilization: 25, resets_at: future() }] },
				{ instance_id: 'worker', available: true, generation: 3, windows: [{ type: 'five_hour', utilization: 99, resets_at: future() }] },
			],
		});
		expect(resolveBucket(rateLimit.current, 'primary')!.windows.five_hour.utilization).toBe(25);
		expect(resolveBucket(rateLimit.current, 'worker')!.windows.five_hour.utilization).toBe(40);
	});

	it('keeps a newer live reading when a same-generation snapshot arrives', () => {
		const snapshot = {
			available: true, windows: [], generation: 100, self_instance_id: 'self',
			snapshots: [{ instance_id: 'self', available: true, generation: 100, fetched_at: Date.now() - 60_000,
				windows: [{ type: 'five_hour', utilization: 50, resets_at: future() }] }],
		};
		rateLimit.applySnapshot(snapshot);
		rateLimit.applyLiveEvent({ rate_limit_type: 'five_hour', utilization: 0.9, resets_at: future(), instance_id: 'self' });
		// An in-flight GET assembled before the event persisted (same generation).
		rateLimit.applySnapshot(snapshot);
		expect(Math.round(rateLimit.current.buckets.self.windows.five_hour.utilization)).toBe(10);
		// A newer generation is authoritative again.
		rateLimit.applySnapshot({ ...snapshot, snapshots: [{ ...snapshot.snapshots[0], generation: 101 }] });
		expect(rateLimit.current.buckets.self.windows.five_hour.utilization).toBe(50);
	});
});

describe('rateLimit.applySnapshot — snapshot robustness', () => {
	it('an empty snapshots array is non-authoritative — falls back to the legacy fold (FIX 1)', () => {
		// Seed a modern bucket first.
		rateLimit.applySnapshot({
			available: true,
			windows: [],
			self_instance_id: 'primary',
			snapshots: [
				{ instance_id: 'primary', available: true, windows: [{ type: 'five_hour', utilization: 12, resets_at: future() }] },
			],
		});
		// `snapshots: []` must NOT wipe everything to an empty widget; it falls
		// through to the legacy fold, which uses the top-level windows and folds
		// them into the CURRENT self bucket (selfInstanceId preserved).
		rateLimit.applySnapshot({
			available: true,
			snapshots: [],
			windows: [{ type: 'five_hour', utilization: 7, resets_at: future() }],
		});
		expect(rateLimit.current.selfInstanceId).toBe('primary');
		expect(Object.keys(rateLimit.current.buckets)).toEqual(['primary']);
		expect(selfBucket()!.windows.five_hour.utilization).toBe(7);
	});

	it('preserves an install absent from a later snapshot and marks it stale (FIX 2)', () => {
		rateLimit.applySnapshot({
			available: true,
			windows: [],
			self_instance_id: 'primary',
			snapshots: [
				{ instance_id: 'primary', available: true, windows: [{ type: 'five_hour', utilization: 12, resets_at: future() }] },
				{ instance_id: 'worker', available: true, windows: [{ type: 'five_hour', utilization: 88, resets_at: future() }] },
			],
		});
		// Worker drops out of the registry for one poll → its bucket survives
		// (carried forward) and is flagged stale, rather than being wiped.
		rateLimit.applySnapshot({
			available: true,
			windows: [],
			self_instance_id: 'primary',
			snapshots: [
				{ instance_id: 'primary', available: true, windows: [{ type: 'five_hour', utilization: 15, resets_at: future() }] },
			],
		});
		expect(resolveBucket(rateLimit.current, 'worker')!.windows.five_hour.utilization).toBe(88);
		expect(rateLimit.current.staleByInstance['worker']).toBe(true);
		expect(rateLimit.current.staleByInstance['primary'] ?? false).toBe(false);
	});

	it('prunes stale flags for installs that end up with no bucket (FIX 2)', () => {
		// An orphaned flag: markStale on an empty store flags the self key even
		// though no bucket exists for it yet.
		rateLimit.markStale();
		expect(rateLimit.current.staleByInstance[SELF_BUCKET_KEY]).toBe(true);
		// The next modern snapshot (which carries no '__self__' bucket to carry
		// forward) must prune the bucket-less flag.
		rateLimit.applySnapshot({
			available: true,
			windows: [],
			self_instance_id: 'primary',
			snapshots: [{ instance_id: 'primary', available: true, windows: [{ type: 'five_hour', utilization: 10, resets_at: future() }] }],
		});
		expect(SELF_BUCKET_KEY in rateLimit.current.staleByInstance).toBe(false);
	});

	it('a null generation does not lower the per-install high-water mark (FIX 3)', () => {
		rateLimit.applySnapshot({
			available: true,
			windows: [],
			self_instance_id: 'primary',
			snapshots: [{ instance_id: 'primary', available: true, generation: 5, windows: [{ type: 'five_hour', utilization: 20, resets_at: future() }] }],
		});
		// A generation-less snapshot is still accepted (updates the bucket)…
		rateLimit.applySnapshot({
			available: true,
			windows: [],
			self_instance_id: 'primary',
			snapshots: [{ instance_id: 'primary', available: true, windows: [{ type: 'five_hour', utilization: 30, resets_at: future() }] }],
		});
		expect(resolveBucket(rateLimit.current, 'primary')!.windows.five_hour.utilization).toBe(30);
		// …but it must NOT have reset the high-water mark: an older (gen 3)
		// straggler arriving afterwards is still rejected.
		rateLimit.applySnapshot({
			available: true,
			windows: [],
			self_instance_id: 'primary',
			snapshots: [{ instance_id: 'primary', available: true, generation: 3, windows: [{ type: 'five_hour', utilization: 99, resets_at: future() }] }],
		});
		expect(resolveBucket(rateLimit.current, 'primary')!.windows.five_hour.utilization).toBe(30);
	});

	it('recovers a fresh in-memory guard after a reload (backend restart) (FIX 10)', async () => {
		vi.resetModules();
		localStorage.clear();
		const fresh = await import('./rate-limit');
		// Pre-restart: a high generation is recorded.
		fresh.rateLimit.applySnapshot({
			available: true,
			windows: [],
			self_instance_id: 'primary',
			snapshots: [{ instance_id: 'primary', available: true, generation: 9, windows: [{ type: 'five_hour', utilization: 20, resets_at: future() }] }],
		});
		// A page reload re-imports the module → the in-memory generation guard
		// starts fresh (only buckets persist via localStorage).
		vi.resetModules();
		const reloaded = await import('./rate-limit');
		// Backend restarted → its generation counter reset to 0. With no lingering
		// high-water mark, the reset-generation snapshot is accepted, not wedged.
		reloaded.rateLimit.applySnapshot({
			available: true,
			windows: [],
			self_instance_id: 'primary',
			snapshots: [{ instance_id: 'primary', available: true, generation: 0, windows: [{ type: 'five_hour', utilization: 77, resets_at: future() }] }],
		});
		expect(reloaded.resolveBucket(reloaded.rateLimit.current, 'primary')!.windows.five_hour.utilization).toBe(77);
	});
});

describe('rateLimit.applySnapshot — non-destructive legacy fold on a modern store', () => {
	function seedModern() {
		rateLimit.applySnapshot({
			available: true,
			windows: [],
			self_instance_id: 'primary',
			snapshots: [
				{
					instance_id: 'primary',
					device_name: 'Studio',
					available: true,
					windows: [{ type: 'five_hour', utilization: 12, resets_at: future() }],
				},
				{
					instance_id: 'worker',
					device_name: 'Worker',
					available: true,
					windows: [{ type: 'five_hour', utilization: 88, resets_at: future() }],
				},
			],
		});
	}

	it('a degraded payload (no snapshots key) folds into the REAL self bucket, preserving the rest', () => {
		seedModern();
		// A DB-degraded modern backend omits `snapshots` entirely — the fold must
		// not wipe the worker bucket, null selfInstanceId, or drop self metadata.
		rateLimit.applySnapshot({
			available: true,
			windows: [{ type: 'five_hour', utilization: 7, resets_at: future() }],
		});
		expect(rateLimit.current.selfInstanceId).toBe('primary');
		expect(Object.keys(rateLimit.current.buckets).sort()).toEqual(['primary', 'worker']);
		expect(SELF_BUCKET_KEY in rateLimit.current.buckets).toBe(false);
		const self = resolveBucket(rateLimit.current, null)!;
		expect(self.windows.five_hour.utilization).toBe(7);
		expect(self.deviceName).toBe('Studio');
		expect(rateLimit.current.buckets['worker'].windows.five_hour.utilization).toBe(88);
	});

	it('same for snapshots: [] — worker bucket and selfInstanceId survive', () => {
		seedModern();
		rateLimit.applySnapshot({
			available: true,
			snapshots: [],
			windows: [{ type: 'five_hour', utilization: 7, resets_at: future() }],
		});
		expect(rateLimit.current.selfInstanceId).toBe('primary');
		expect(Object.keys(rateLimit.current.buckets).sort()).toEqual(['primary', 'worker']);
		const self = resolveBucket(rateLimit.current, null)!;
		expect(self.windows.five_hour.utilization).toBe(7);
		expect(self.deviceName).toBe('Studio');
	});

	it('resolution stays strict after a degraded payload (no legacy-fallback misfire)', () => {
		seedModern();
		rateLimit.applySnapshot({
			available: true,
			windows: [{ type: 'five_hour', utilization: 7, resets_at: future() }],
		});
		// The store is still modern-shaped: a worker session keeps showing the
		// WORKER bucket, and an unknown target resolves to null — never the local
		// account's numbers.
		expect(resolveBucket(rateLimit.current, 'worker')!.windows.five_hour.utilization).toBe(88);
		expect(resolveBucketKey(rateLimit.current, 'worker')).toBe('worker');
		expect(resolveBucket(rateLimit.current, 'ghost')).toBeNull();
	});

	it('an unavailable degraded payload keeps last-known self windows and the worker bucket', () => {
		seedModern();
		rateLimit.applySnapshot({ available: false, windows: [] });
		expect(rateLimit.current.selfInstanceId).toBe('primary');
		const self = resolveBucket(rateLimit.current, null)!;
		expect(self.available).toBe(false);
		expect(self.windows.five_hour.utilization).toBe(12);
		expect(rateLimit.current.buckets['worker'].windows.five_hour.utilization).toBe(88);
	});
});

describe('resolveBucket — legacy self-fallback (FIX 4)', () => {
	it('a legacy store resolves a non-null target to the self bucket', () => {
		rateLimit.applySnapshot({ available: true, windows: [{ type: 'five_hour', utilization: 42, resets_at: future() }] });
		expect(rateLimit.current.selfInstanceId).toBeNull();
		expect(resolveBucket(rateLimit.current, 'some-remote')!.windows.five_hour.utilization).toBe(42);
		expect(resolveBucketKey(rateLimit.current, 'some-remote')).toBe(SELF_BUCKET_KEY);
	});

	it('a modern store still resolves an unknown remote target to null', () => {
		rateLimit.applySnapshot({
			available: true,
			windows: [],
			self_instance_id: 'primary',
			snapshots: [{ instance_id: 'primary', available: true, windows: [{ type: 'five_hour', utilization: 12, resets_at: future() }] }],
		});
		expect(resolveBucket(rateLimit.current, 'ghost')).toBeNull();
		expect(resolveBucketKey(rateLimit.current, 'ghost')).toBeNull();
	});
});

describe('rateLimit.applyLiveEvent — routing by instance_id', () => {
	it('routes an event to the tagged install bucket, not the self bucket', () => {
		rateLimit.applySnapshot({
			available: true,
			windows: [],
			self_instance_id: 'primary',
			snapshots: [
				{ instance_id: 'primary', available: true, windows: [{ type: 'five_hour', utilization: 10, resets_at: future() }] },
				{ instance_id: 'worker', available: true, windows: [{ type: 'five_hour', utilization: 10, resets_at: future() }] },
			],
		});
		rateLimit.applyLiveEvent({
			rate_limit_type: 'five_hour',
			utilization: 0.5, // used fraction → 50% used → 50 remaining
			resets_at: future(),
			instance_id: 'worker',
		});
		expect(resolveBucket(rateLimit.current, 'primary')!.windows.five_hour.utilization).toBe(10);
		expect(resolveBucket(rateLimit.current, 'worker')!.windows.five_hour.utilization).toBe(50);
	});

	it('falls back to the self bucket when the event carries no instance_id', () => {
		rateLimit.applySnapshot({
			available: true,
			windows: [{ type: 'five_hour', utilization: 10, resets_at: future() }],
		});
		rateLimit.applyLiveEvent({ rate_limit_type: 'five_hour', utilization: 0.25, resets_at: future() });
		expect(selfBucket()!.windows.five_hour.utilization).toBe(75);
	});

	it('stores REMAINING quota — the SDK reports the USED fraction', () => {
		rateLimit.applySnapshot({
			available: true,
			windows: [{ type: 'five_hour', utilization: 100, resets_at: future() }],
		});
		// 0.25 used → 75 left, matching the polled Claude feed's convention.
		rateLimit.applyLiveEvent({ rate_limit_type: 'five_hour', utilization: 0.25, resets_at: future() });
		expect(selfBucket()!.windows.five_hour.utilization).toBe(75);
		// Over the limit (1.05 used) leaves nothing, not ~99% (it is not a percent).
		rateLimit.applyLiveEvent({ rate_limit_type: 'five_hour', utilization: 1.05, resets_at: future() });
		expect(selfBucket()!.windows.five_hour.utilization).toBe(0);
	});

	it('dates the live window on every event, even with unchanged values', () => {
		vi.useFakeTimers();
		try {
			const start = new Date('2026-01-01T00:00:00Z').getTime();
			vi.setSystemTime(start);
			const resetsAt = future();
			rateLimit.applySnapshot({
				available: true,
				windows: [],
				self_instance_id: 'primary',
				snapshots: [
					{ instance_id: 'primary', available: true, fetched_at: start, windows: [{ type: 'five_hour', utilization: 75, resets_at: resetsAt }] },
				],
			});
			const later = start + BUCKET_STALE_MS + 60_000;
			vi.setSystemTime(later);
			expect(isBucketStale(selfBucket()!, later)).toBe(true);

			// Same reading as the snapshot (0.25 used → 75 left): still counts as fresh data.
			rateLimit.applyLiveEvent({ rate_limit_type: 'five_hour', utilization: 0.25, resets_at: resetsAt, instance_id: 'primary' });
			expect(selfBucket()!.windows.five_hour.utilization).toBe(75);
			expect(selfBucket()!.windows.five_hour.received_at).toBe(later);
			expect(isBucketStale(selfBucket()!, later)).toBe(false);
		} finally {
			vi.useRealTimers();
		}
	});

	it('keeps the other windows stale when a live event refreshes one of them', () => {
		vi.useFakeTimers();
		try {
			const start = new Date('2026-01-01T00:00:00Z').getTime();
			vi.setSystemTime(start);
			rateLimit.applySnapshot({
				available: true,
				windows: [],
				self_instance_id: 'primary',
				snapshots: [
					{
						instance_id: 'primary', available: true, fetched_at: start,
						windows: [
							{ type: 'five_hour', utilization: 75, resets_at: future() },
							{ type: 'seven_day', utilization: 40, resets_at: future() },
						],
					},
				],
			});
			const later = start + BUCKET_STALE_MS + 60_000;
			vi.setSystemTime(later);
			rateLimit.applyLiveEvent({ rate_limit_type: 'five_hour', utilization: 0.3, resets_at: future(), instance_id: 'primary' });
			// Before: the event set the whole bucket's fetchedAt, hiding the week's age.
			// The snapshot's windows carry no observed_at, so the week stays undated.
			expect(selfBucket()!.fetchedAt).toBe(start);
			expect(selfBucket()!.windows.seven_day.received_at).toBeNull();
			expect(bucketHasUndatedWindow(selfBucket()!)).toBe(true);
			expect(isBucketStale(selfBucket()!, later)).toBe(true);

			// The backend's broadcast after the event dates each window itself, so
			// the snapshot's newer fetched_at does not freshen the weekly window.
			rateLimit.applySnapshot({
				available: true,
				windows: [],
				self_instance_id: 'primary',
				snapshots: [
					{
						instance_id: 'primary', available: true, fetched_at: later, generation: 99,
						windows: [
							{ type: 'five_hour', utilization: 70, resets_at: future(), observed_at: later },
							{ type: 'seven_day', utilization: 40, resets_at: future(), observed_at: start },
						],
					},
				],
			});
			expect(selfBucket()!.windows.seven_day.received_at).toBe(start);
			expect(isBucketStale(selfBucket()!, later)).toBe(true);

			// Once every window has been reported recently the bucket is fresh.
			rateLimit.applyLiveEvent({ rate_limit_type: 'seven_day', utilization: 0.6, resets_at: future(), instance_id: 'primary' });
			expect(isBucketStale(selfBucket()!, later)).toBe(false);
		} finally {
			vi.useRealTimers();
		}
	});
});

describe('undated legacy windows (#1661)', () => {
	it('never re-dates a persisted Fable window from reports for other windows', () => {
		vi.useFakeTimers();
		try {
			const start = new Date('2026-01-01T00:00:00Z').getTime();
			vi.setSystemTime(start);
			const fableReset = future();
			// The backend seeded a window persisted by the removed OAuth producer:
			// no observed_at, unchanged utilization and reset across probes.
			const fable = { type: 'seven_day_overage_included', utilization: 64, resets_at: fableReset };
			const snapshotAt = (t: number, generation: number) => ({
				available: true,
				windows: [],
				self_instance_id: 'primary',
				snapshots: [
					{
						instance_id: 'primary', available: true, fetched_at: t, generation,
						windows: [
							{ type: 'five_hour', utilization: 70, resets_at: future(), observed_at: t },
							fable,
						],
					},
				],
			});
			rateLimit.applySnapshot(snapshotAt(start, 1));
			expect(selfBucket()!.windows.seven_day_overage_included.received_at).toBeNull();
			expect(isBucketStale(selfBucket()!, start)).toBe(true);
			expect(bucketObservedAt(selfBucket()!)).toBe(start);

			// Repeated five_hour reports and the REST snapshots that follow them
			// advance fetched_at, but never the Fable window's age.
			for (let i = 1; i <= 3; i++) {
				const t = start + i * 60_000;
				vi.setSystemTime(t);
				rateLimit.applyLiveEvent({ rate_limit_type: 'five_hour', utilization: 0.3, resets_at: future(), instance_id: 'primary' });
				rateLimit.applySnapshot(snapshotAt(t, 1 + i));
				const bucket = selfBucket()!;
				expect(bucket.fetchedAt).toBe(t);
				expect(bucket.windows.seven_day_overage_included.received_at).toBeNull();
				// The stored value is shown as reported, not inverted.
				expect(bucket.windows.seven_day_overage_included.utilization).toBe(64);
				expect(isBucketStale(bucket, t)).toBe(true);
			}

			// A real Fable report dates it; the bucket is then current.
			const reported = start + 5 * 60_000;
			vi.setSystemTime(reported);
			rateLimit.applyLiveEvent({ rate_limit_type: 'seven_day_overage_included', utilization: 0.4, resets_at: fableReset, instance_id: 'primary' });
			expect(selfBucket()!.windows.seven_day_overage_included.received_at).toBe(reported);
			expect(bucketHasUndatedWindow(selfBucket()!)).toBe(false);
			expect(isBucketStale(selfBucket()!, reported)).toBe(false);
		} finally {
			vi.useRealTimers();
		}
	});

	it('still dates provider windows by their snapshot fetch time', () => {
		rateLimit.applySnapshot({ windows: [], available: false, providers: [
			{ provider_id: 'zai', status: 'fresh', fetched_at: 5_000, windows: [{ type: 'five_hour', utilization: 10, resets_at: null }] },
		] });
		expect(rateLimit.current.providerBuckets.zai.windows.five_hour.received_at).toBe(5_000);
	});
});

describe('resolveBucket / resolveBucketKey', () => {
	beforeEach(() => {
		rateLimit.applySnapshot({
			available: true,
			windows: [],
			self_instance_id: 'primary',
			snapshots: [
				{ instance_id: 'primary', available: true, windows: [{ type: 'five_hour', utilization: 10, resets_at: future() }] },
				{ instance_id: 'worker', available: true, windows: [{ type: 'five_hour', utilization: 88, resets_at: future() }] },
			],
		});
	});

	it('exact match returns that install bucket', () => {
		expect(resolveBucket(rateLimit.current, 'worker')!.windows.five_hour.utilization).toBe(88);
		expect(resolveBucketKey(rateLimit.current, 'worker')).toBe('worker');
	});

	it('null target returns the self bucket', () => {
		expect(resolveBucket(rateLimit.current, null)!.windows.five_hour.utilization).toBe(10);
		expect(resolveBucketKey(rateLimit.current, null)).toBe('primary');
	});

	it('an unknown remote target returns null (never the self bucket)', () => {
		expect(resolveBucket(rateLimit.current, 'ghost')).toBeNull();
		expect(resolveBucketKey(rateLimit.current, 'ghost')).toBeNull();
	});
});

describe('rateLimit.markStale', () => {
	it('marks the self bucket stale; a fresh self snapshot clears it', () => {
		rateLimit.applySnapshot({
			available: true,
			windows: [{ type: 'five_hour', utilization: 20, resets_at: future() }],
		});
		expect(rateLimit.current.staleByInstance[SELF_BUCKET_KEY] ?? false).toBe(false);

		rateLimit.markStale();
		expect(rateLimit.current.staleByInstance[SELF_BUCKET_KEY]).toBe(true);

		rateLimit.applySnapshot({
			available: true,
			windows: [{ type: 'five_hour', utilization: 5, resets_at: future() }],
		});
		expect(rateLimit.current.staleByInstance[SELF_BUCKET_KEY]).toBe(false);
	});

	it('does not mark another install stale', () => {
		rateLimit.applySnapshot({
			available: true,
			windows: [],
			self_instance_id: 'primary',
			snapshots: [
				{ instance_id: 'primary', available: true, windows: [{ type: 'five_hour', utilization: 10, resets_at: future() }] },
				{ instance_id: 'worker', available: true, windows: [{ type: 'five_hour', utilization: 88, resets_at: future() }] },
			],
		});
		rateLimit.markStale();
		expect(rateLimit.current.staleByInstance['primary']).toBe(true);
		expect(rateLimit.current.staleByInstance['worker'] ?? false).toBe(false);
	});

	it('a discarded stale snapshot does NOT clear the stale flag', () => {
		rateLimit.applySnapshot({
			available: true,
			generation: 5,
			windows: [{ type: 'five_hour', utilization: 20, resets_at: future() }],
		});
		rateLimit.markStale();
		rateLimit.applySnapshot({
			available: true,
			generation: 2,
			windows: [{ type: 'five_hour', utilization: 99, resets_at: future() }],
		});
		expect(rateLimit.current.staleByInstance[SELF_BUCKET_KEY]).toBe(true);
		expect(selfBucket()!.windows.five_hour.utilization).toBe(20);
	});
});

describe('localStorage v1→v2 migration', () => {
	it('migrates a legacy usage-limit-windows map into the self bucket', async () => {
		vi.resetModules();
		localStorage.clear();
		localStorage.setItem(
			'usage-limit-windows',
			JSON.stringify({
				five_hour: {
					type: 'five_hour',
					utilization: 63,
					resets_at: null,
					received_at: Date.now(),
					source: 'poll',
				},
			})
		);
		const fresh = await import('./rate-limit');
		expect(localStorage.getItem('usage-limit-windows')).toBeNull();
		expect(fresh.resolveBucket(fresh.rateLimit.current, null)!.windows.five_hour.utilization).toBe(63);
	});
});

describe('rateLimit.applySnapshot — provider buckets', () => {
	it('folds providers[] into providerBuckets keyed by provider_id', () => {
		rateLimit.applySnapshot({
			available: true,
			windows: [],
			providers: [
				{
					provider_id: 'codex-1',
					provider_name: 'Codex',
					provider_type: 'codex',
					plan_type: 'Plus',
					windows: [
						{ type: 'five_hour', utilization: 30, resets_at: future() },
						{ type: 'seven_day', utilization: 12, resets_at: future() },
					],
					credits: null,
					fetched_at: 1_700_000_000_000,
				},
			],
		});
		const pb = rateLimit.current.providerBuckets['codex-1'];
		expect(pb).toBeDefined();
		expect(pb.providerName).toBe('Codex');
		expect(pb.providerType).toBe('codex');
		expect(pb.planType).toBe('Plus');
		expect(pb.fetchedAt).toBe(1_700_000_000_000);
		expect(Object.keys(pb.windows).sort()).toEqual(['five_hour', 'seven_day']);
		expect(pb.windows.five_hour.utilization).toBe(30);
	});

	it('full-replaces the provider set when providers[] is present', () => {
		rateLimit.applySnapshot({
			available: true,
			windows: [],
			providers: [{ provider_id: 'a', windows: [], credits: null }],
		});
		expect(Object.keys(rateLimit.current.providerBuckets)).toEqual(['a']);
		rateLimit.applySnapshot({
			available: true,
			windows: [],
			providers: [{ provider_id: 'b', windows: [], credits: null }],
		});
		expect(Object.keys(rateLimit.current.providerBuckets)).toEqual(['b']);
	});

	it('leaves providerBuckets UNTOUCHED when the providers key is absent (WS-only payload)', () => {
		rateLimit.applySnapshot({
			available: true,
			windows: [],
			providers: [{ provider_id: 'codex-1', windows: [], credits: null }],
		});
		expect(Object.keys(rateLimit.current.providerBuckets)).toEqual(['codex-1']);
		// A snapshot with no `providers` key (e.g. a WS-only rate_limit fold) must
		// not wipe the existing provider map.
		rateLimit.applySnapshot({
			available: true,
			generation: 1,
			windows: [{ type: 'five_hour', utilization: 5, resets_at: future() }],
		});
		expect(Object.keys(rateLimit.current.providerBuckets)).toEqual(['codex-1']);
	});

	it('empty providers[] clears the map (authoritative empty set)', () => {
		rateLimit.applySnapshot({
			available: true,
			windows: [],
			providers: [{ provider_id: 'codex-1', windows: [], credits: null }],
		});
		expect(Object.keys(rateLimit.current.providerBuckets)).toEqual(['codex-1']);
		rateLimit.applySnapshot({ available: true, windows: [], providers: [] });
		expect(rateLimit.current.providerBuckets).toEqual({});
	});

	it('stores the credits blob verbatim', () => {
		const credits = { usage: 3.1, limit_remaining: 6.9, unlimited: false };
		rateLimit.applySnapshot({
			available: true,
			windows: [],
			providers: [
				{
					provider_id: 'openrouter-1',
					provider_name: 'OpenRouter',
					provider_type: 'openrouter',
					windows: [],
					credits,
					fetched_at: null,
				},
			],
		});
		const pb = rateLimit.current.providerBuckets['openrouter-1'];
		expect(pb.credits).toEqual(credits);
		expect(pb.fetchedAt).toBeNull();
	});

	it('a combined snapshots+providers payload keeps BOTH Claude and provider buckets', () => {
		// The real poll payload carries per-install Claude `snapshots` AND
		// non-Claude `providers` together — the Claude path must not drop the
		// folded providerBuckets and vice-versa.
		rateLimit.applySnapshot({
			available: true,
			windows: [],
			self_instance_id: 'primary',
			snapshots: [
				{
					instance_id: 'primary',
					available: true,
					windows: [{ type: 'five_hour', utilization: 20, resets_at: future() }],
				},
			],
			providers: [{ provider_id: 'codex-1', windows: [], credits: null }],
		});
		expect(resolveBucket(rateLimit.current, 'primary')?.windows.five_hour.utilization).toBe(20);
		expect(Object.keys(rateLimit.current.providerBuckets)).toEqual(['codex-1']);
	});
});

describe('remaining quota helpers', () => {
	function win(utilization: number, resetsAt: string | null): UsageWindow {
		return {
			type: 'five_hour',
			utilization,
			resets_at: resetsAt,
			received_at: 0,
			source: 'poll',
		};
	}

	it('preserves a reported remaining percentage', () => {
		const now = Date.now();
		expect(reportedRemainingPct(win(12, new Date(now + 60_000).toISOString()), now)).toBe(12);
		expect(reportedRemainingPct(win(100, new Date(now + 60_000).toISOString()), now)).toBe(100);
		expect(reportedRemainingPct(win(0, new Date(now + 60_000).toISOString()), now)).toBe(0);
	});

	it('inverts a reported used percentage', () => {
		const now = Date.now();
		expect(remainingFromUsedPct(win(12, new Date(now + 60_000).toISOString()), now)).toBe(88);
		expect(remainingFromUsedPct(win(100, new Date(now + 60_000).toISOString()), now)).toBe(0);
		expect(remainingFromUsedPct(win(0, new Date(now + 60_000).toISOString()), now)).toBe(100);
	});

	it('reads a window past its reset as refilled, never as the pre-reset report', () => {
		const now = Date.now();
		const expired = win(12, new Date(now - 60_000).toISOString());
		expect(reportedRemainingPct(expired, now)).toBe(100);
		expect(remainingFromUsedPct(expired, now)).toBe(100);
		expect(effectivePct(expired, now)).toBe(0);
		// Until the reset passes the last report stands.
		expect(reportedRemainingPct(expired, now - 120_000)).toBe(12);
	});
});

describe('windowLabel — provider window types', () => {
	it('maps known Claude and provider types', () => {
		expect(windowLabel('five_hour')).toBe('5-hour limit');
		expect(windowLabel('day')).toBe('Daily');
		expect(windowLabel('month')).toBe('Monthly');
		expect(windowLabel('credit_limit')).toBe('Credit limit');
		expect(windowLabel('grok_api')).toBe('API');
		expect(windowLabel('grok_chat')).toBe('Grok Chat');
	});

	it('humanizes a window_XXm rolling window before the generic fallback', () => {
		expect(windowLabel('window_600m')).toBe('600m window');
		expect(windowLabel('window_5m')).toBe('5m window');
	});

	it('falls back to a humanized label for unknown types', () => {
		expect(windowLabel('some_other_thing')).toBe('some other thing');
	});
});

describe('displayWindows — drops degenerate and never-surfaced windows', () => {
	function win(type: string): UsageWindow {
		return {
			type,
			utilization: 10,
			resets_at: null,
			received_at: 0,
			source: 'poll',
		};
	}

	it('returns orderedWindows order, minus window_0m', () => {
		const windows: Record<string, UsageWindow> = {
			seven_day: win('seven_day'),
			window_0m: win('window_0m'),
			five_hour: win('five_hour'),
		};
		const result = displayWindows(windows);
		expect(result.map((w) => w.type)).toEqual(['five_hour', 'seven_day']);
	});

	it('keeps a non-zero rolling window like window_60m', () => {
		const windows: Record<string, UsageWindow> = {
			window_0m: win('window_0m'),
			window_60m: win('window_60m'),
		};
		const result = displayWindows(windows);
		expect(result.map((w) => w.type)).toEqual(['window_60m']);
	});

	it('drops a persisted grok_build window (no longer surfaced by the poller)', () => {
		const windows: Record<string, UsageWindow> = {
			seven_day: win('seven_day'),
			grok_build: win('grok_build'),
			grok_api: win('grok_api'),
		};
		const result = displayWindows(windows);
		expect(result.map((w) => w.type)).toEqual(['seven_day', 'grok_api']);
	});
});

describe('rateLimit.applySnapshot — usage source metadata', () => {
	it('keeps provider source info and the last attempt separate from the last success', () => {
		rateLimit.applySnapshot({ windows: [], available: false, providers: [
			{ provider_id: 'or', provider_type: 'openrouter', fetched_at: 10, last_attempt_at: 20,
				source_info: { id: 'openrouter_key', label: 'OpenRouter key API', scope: 'api_key',
					scope_label: 'Key spend', measure: 'reported_spend', measure_label: 'Provider-reported spend',
					collection: 'polled', support: 'documented', support_label: 'Documented API',
					dashboard_url: 'https://openrouter.ai/settings/keys' } },
			{ provider_id: 'old', provider_type: 'openrouter', fetched_at: 30 },
		] });
		const buckets = rateLimit.current.providerBuckets;
		expect(buckets.or.sourceInfo?.measure).toBe('reported_spend');
		expect(buckets.or.fetchedAt).toBe(10);
		expect(buckets.or.lastAttemptAt).toBe(20);
		// Older backends: the last attempt defaults to the last success.
		expect(buckets.old.lastAttemptAt).toBe(30);
		expect(buckets.old.sourceInfo).toBeNull();
	});

	it('records the Claude source from REST payloads and keeps it across WS-only payloads', () => {
		let state: ClaudeUsageSourceState | undefined;
		const unsubscribe = claudeUsageSource.subscribe((value) => (state = value));
		rateLimit.applySnapshot({ windows: [], available: true, attempted_at: 12.5, last_error_kind: 'timeout',
			claude_source: { id: 'claude_rate_limit_events', label: 'Claude', scope: 'subscription', scope_label: 's',
				measure: 'subscription_allowance', measure_label: 'Subscription allowance', collection: 'passive',
				support: 'response_metadata', support_label: 'Metadata', dashboard_url: 'https://claude.ai/' } });
		expect(state?.attemptedAt).toBe(12_500);
		expect(state?.lastErrorKind).toBe('timeout');
		rateLimit.applySnapshot({ windows: [], available: true });
		expect(state?.source).not.toBeNull();
		unsubscribe();
	});
});

describe('reset times and data-less live events', () => {
	it('reads resets_at as ISO, epoch seconds or epoch ms, and shows no countdown once it has passed', async () => {
		const { parseResetMs, formatCountdown, formatAsOf } = await import('./rate-limit');
		const now = Date.UTC(2026, 8, 25, 12, 0, 0);
		expect(parseResetMs(now / 1000)).toBe(now);
		expect(parseResetMs(now)).toBe(now);
		expect(parseResetMs(String(now / 1000))).toBe(now);
		expect(parseResetMs(new Date(now).toISOString())).toBe(now);
		expect(parseResetMs('  ')).toBeNull();
		expect(parseResetMs('soon')).toBeNull();

		expect(formatCountdown(new Date(now + 2 * 86_400_000 + 3 * 3_600_000).toISOString(), now)).toBe('2d 3h');
		expect(formatCountdown(String((now + 3_600_000 + 12 * 60_000) / 1000), now)).toBe('1h 12m');
		expect(formatCountdown(new Date(now + 61_500).toISOString(), now)).toBe('1m 2s');
		// A reset in the past is a finished window, not "now".
		expect(formatCountdown(new Date(now - 1000).toISOString(), now)).toBe('');
		expect(formatCountdown(new Date(now).toISOString(), now)).toBe('');
		expect(formatCountdown('garbage', now)).toBe('');

		expect(formatAsOf(now - 30_000, now)).toBe('just now');
		expect(formatAsOf(now - 4 * 60_000, now)).toBe('4m ago');
		expect(formatAsOf(now - 26 * 3_600_000, now)).toBe('1d ago');
		expect(formatAsOf(null, now)).toBe('');
	});

	it('ignores a live event that carries no utilization instead of creating or wiping a window', () => {
		rateLimit.applySnapshot({ available: true, windows: [{ type: 'five_hour', utilization: 60, resets_at: future() }] });
		const before = selfBucket()!.windows.five_hour;
		rateLimit.applyLiveEvent({ rate_limit_type: 'five_hour', utilization: undefined, resets_at: future() });
		rateLimit.applyLiveEvent({ rate_limit_type: 'seven_day', utilization: null as unknown as number });
		rateLimit.applyLiveEvent({ rate_limit_type: '', utilization: 0.5 });
		expect(selfBucket()!.windows.five_hour).toEqual(before);
		expect(Object.keys(selfBucket()!.windows)).toEqual(['five_hour']);
	});
});

describe('network devices and refilled windows', () => {
	beforeEach(() => {
		localStorage.clear();
		rateLimit.__resetForTests();
	});

	function networkSnapshot() {
		const now = Date.now();
		rateLimit.applySnapshot({
			available: true,
			windows: [],
			self_instance_id: 'ai-shuffle-local',
			self_device_id: 'dev_local',
			snapshots: [
				{ instance_id: 'ai-shuffle-local', device_id: 'dev_local', device_name: 'DesktopWorker', available: true,
					windows: [{ type: 'five_hour', utilization: 70, resets_at: String(now + 3_600_000), observed_at: now }] },
				{ instance_id: 'ai-shuffle-unraid', device_id: 'dev_unraid', device_name: 'Unraid', available: true,
					windows: [{ type: 'five_hour', utilization: 99, resets_at: String(now + 3_600_000), observed_at: now }] },
			],
		});
	}

	it('resolves a session owner named by device id to that install bucket', () => {
		networkSnapshot();
		const state = rateLimit.current;
		expect(resolveBucketKey(state, 'dev_unraid')).toBe('ai-shuffle-unraid');
		expect(resolveBucket(state, 'dev_unraid')?.deviceName).toBe('Unraid');
		expect(resolveBucketKey(state, 'dev_local')).toBe('ai-shuffle-local');
		expect(isSelfTarget(state, 'dev_local')).toBe(true);
		expect(isSelfTarget(state, 'dev_unraid')).toBe(false);
		// An unknown device never borrows this install's numbers.
		expect(resolveBucket(state, 'dev_ghost')).toBeNull();
	});

	it('a refilled Fable window no longer dates the bucket as stale', () => {
		const now = Date.now();
		rateLimit.applySnapshot({
			available: true, windows: [], self_instance_id: 'self',
			snapshots: [{ instance_id: 'self', available: true, fetched_at: now, windows: [
				{ type: 'five_hour', utilization: 99, resets_at: String(now + 3_600_000), observed_at: now },
				{ type: 'seven_day_overage_included', utilization: 19,
					resets_at: String(now - 86_400_000), observed_at: now - 6 * 86_400_000 },
				// Persisted before windows were dated, and already reset.
				{ type: 'seven_day_opus', utilization: 50, resets_at: String(now - 60_000) },
			] }],
		});
		const bucket = resolveBucket(rateLimit.current, null)!;
		expect(bucketObservedAt(bucket, now)).toBe(now);
		expect(bucketHasUndatedWindow(bucket, now)).toBe(false);
		expect(isBucketStale(bucket, now)).toBe(false);
		expect(reportedRemainingPct(bucket.windows.seven_day_overage_included, now)).toBe(100);
	});

	it('keeps the self device id across a reload of the persisted store', () => {
		networkSnapshot();
		const saved = JSON.parse(localStorage.getItem('usage-limit-buckets-v3') ?? '{}');
		expect(saved.selfDeviceId).toBe('dev_local');
		expect(saved.buckets['ai-shuffle-unraid'].deviceId).toBe('dev_unraid');
	});
});
