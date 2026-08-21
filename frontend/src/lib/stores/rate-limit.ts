/**
 * Usage-limits store — per-INSTALL buckets of Claude subscription usage.
 *
 * The 5-hour usage window is scoped to a Claude ACCOUNT, and in a clustered
 * deployment (primary + worker sharing one Postgres) each install is logged into
 * a DIFFERENT account. A session can be offloaded to the worker, so the usage a
 * viewer cares about is the usage of the install that actually RUNS the viewed
 * session — not necessarily the local install's account. This store therefore
 * keeps one `UsageBucket` PER install (keyed by `instance_id`) rather than a
 * single global window set. The widget resolves which bucket to show from the
 * currently viewed/composed session (see `usage-target.ts`).
 *
 * Fed by BOTH:
 *   1. A polled snapshot (`usage_snapshot` WS event + the REST `/usage/limits`
 *      endpoint) — authoritative full sets of windows per install, from the
 *      backend usage poller. Utilization is already 0–100 percent. The modern
 *      payload carries a `snapshots` array (one entry per install) plus
 *      `self_instance_id`; a legacy payload carries only top-level windows for
 *      the LOCAL install, which we fold into a self bucket.
 *   2. Live SDK `rate_limit` events (WS `case 'rate_limit'`) — refine a SINGLE
 *      window of the emitting install's bucket, routed by the event's
 *      `instance_id`. SDK utilization is a 0.0–1.0 fraction of quota USED, while
 *      the polled Claude feed reports quota REMAINING; ingest normalises the
 *      fraction to 0–100 and flips it to remaining so both feeds agree.
 *
 * Windows key on the window-type string (`five_hour`, `seven_day`,
 * `seven_day_opus`, `seven_day_sonnet`, `seven_day_overage_included`,
 * `overage`). Buckets are persisted to localStorage so the header pill/popover
 * stay populated across reloads.
 *
 * Plain `.ts` (no $state runes) — see KNOWN_ISSUES.md Root Cause 10.
 *
 * Consumers: `UsageLimitIndicator.svelte` (header pill + popover) resolves a
 * bucket via `resolveBucket`; `ws-message-handlers.ts` calls `applyLiveEvent` /
 * `applySnapshot`; `usage-target.ts` derives which install to show.
 */

import { createStore } from './store-utils';

/** A single rate-limit window, utilization normalised to 0–100 percent. */
export interface UsageWindow {
	type: string;
	/** 0–100 percent. */
	utilization: number;
	resets_at: string | null;
	/** Wall-clock (ms since epoch) when we last updated this window. */
	received_at: number;
	source: 'poll' | 'live';
}

/**
 * One install's usage snapshot: its window set plus who/where it belongs to.
 * `accountId`/`accountEmail` identify the Claude account so the popover can
 * dedupe two installs that happen to share one account.
 */
export interface UsageBucket {
	/** Keyed by window type. */
	windows: Record<string, UsageWindow>;
	/** Epoch ms the backend last fetched this install's usage (staleness). */
	fetchedAt: number | null;
	/** Whether this install currently has a usable Claude credential. */
	available: boolean;
	accountId?: string | null;
	accountEmail?: string | null;
	accountName?: string | null;
	deviceName?: string | null;
	/** Claude subscription tier of that install's account (e.g. `'max'`). */
	planType?: string | null;
	/** Owning install seen recently (heartbeat). null = unknown. */
	online?: boolean | null;
	/** Monotonic generation tag from the backend for THIS install (see below). */
	generation?: number | null;
}

/**
 * One non-Claude provider's usage snapshot (Codex/ChatGPT, OpenRouter, …).
 * Provider-scoped (not install-scoped), so none of the per-install
 * generation/merge logic applies — the endpoint returns the authoritative
 * current set and we full-replace on each poll.
 */
export interface ProviderUsageBucket {
	providerId: string;
	providerName: string | null;
	providerType: string | null;
	planType: string | null;
	/** Keyed by window type (reuses the Claude `UsageWindow` shape). */
	windows: Record<string, UsageWindow>;
	/** Verbatim provider credits blob (unknown shape — guard all reads). */
	credits: Record<string, unknown> | null;
	/** Epoch ms the backend last fetched this provider's usage (staleness). */
	fetchedAt: number | null;
}

interface RateLimitState {
	/** Keyed by install `instance_id` (or `'__self__'` for a legacy/local-only
	 *  payload that carries no instance id). */
	buckets: Record<string, UsageBucket>;
	/** Keyed by `provider_id`. Provider-scoped (see `ProviderUsageBucket`). */
	providerBuckets: Record<string, ProviderUsageBucket>;
	/** The local install's instance id, when the backend reports it. null in the
	 *  legacy path — the self bucket is then keyed `'__self__'`. */
	selfInstanceId: string | null;
	/**
	 * Per-install "stale between an account switch and the fresh snapshot" flag.
	 * The indicator dims the pill for the shown install so we never present the
	 * OLD account's numbers as current. Runtime-only (not persisted).
	 */
	staleByInstance: Record<string, boolean>;
}

/** Fallback bucket key when a payload carries no instance id (legacy backend). */
export const SELF_BUCKET_KEY = '__self__';

/** Live SDK `rate_limit` event payload (utilization is a 0–1 fraction). */
export interface LiveRateLimitEvent {
	status?: string;
	rate_limit_type: string;
	resets_at?: string | null;
	utilization?: number | null;
	session_id?: string;
	/** The install that emitted the event (relayed remote events are tagged with
	 *  the worker's id). Absent on legacy backends → routed to the self bucket. */
	instance_id?: string | null;
}

/** One window from a polled snapshot (utilization already 0–100). */
export interface SnapshotWindow {
	type: string;
	utilization: number;
	resets_at?: string | null;
}

/** One install's entry in the modern `snapshots` array. */
export interface SnapshotInstance {
	instance_id: string;
	device_name?: string | null;
	online?: boolean | null;
	account_id?: string | null;
	account_email?: string | null;
	account_name?: string | null;
	/**
	 * Claude subscription tier for that install's account, joined from
	 * `claude_accounts.subscription_type`. Null when the install has no account
	 * row or its plan isn't known yet.
	 */
	plan_type?: string | null;
	windows: SnapshotWindow[];
	/** Epoch MILLISECONDS (the legacy top-level `fetched_at` is seconds). */
	fetched_at?: number | null;
	available: boolean;
	generation?: number | null;
}

/**
 * One non-Claude provider's entry in a polled snapshot's `providers` array
 * (U4 wire shape — the `raw` debug blob is never sent). `windows` reuses the
 * Claude `SnapshotWindow` shape (`type` / `utilization` 0–100 / `resets_at`).
 */
export interface ProviderUsageEntry {
	provider_id: string;
	provider_name?: string | null;
	provider_type?: string | null;
	plan_type?: string | null;
	windows?: SnapshotWindow[];
	credits?: Record<string, unknown> | null;
	fetched_at?: number | null;
}

export interface UsageSnapshot {
	/** Legacy top-level fields — the LOCAL install's data. Always present for
	 *  backwards compatibility; folded into the self bucket when `snapshots` is
	 *  absent. */
	windows: SnapshotWindow[];
	fetched_at?: number | null;
	available: boolean;
	/**
	 * Monotonic generation tag from the backend. Bumped on every account switch.
	 * Lets us drop a snapshot from an older generation that arrives out of order
	 * after a newer one (e.g. a slow in-flight poll completing post-switch).
	 */
	generation?: number | null;
	/** Modern per-install snapshots (each `fetched_at` in MILLISECONDS). When
	 *  present AND non-empty, merges into the buckets map (absent installs are
	 *  carried forward and marked stale). Absent or empty (e.g. a backend DB
	 *  blip) → the top-level fields fold into the current self bucket. */
	snapshots?: SnapshotInstance[];
	/** The local install's instance id (which entry in `snapshots` is "us"). */
	self_instance_id?: string | null;
	/** Non-Claude provider usage. Full-replaces `providerBuckets` when present;
	 *  when the key is ABSENT the store leaves the existing map untouched (so a
	 *  WS-only payload without providers can't wipe it). Additive; empty for old
	 *  or provider-less installs. */
	providers?: ProviderUsageEntry[];
}

// Bumped to v3 when the backend started reporting Claude window utilization as
// quota REMAINING: any cached v2 bucket holds percent-USED numbers that would
// render as "1% left" on a barely-touched window until the next poll lands.
// A fresh key discards them outright rather than showing a wrong figure first.
const STORAGE_KEY_V3 = 'usage-limit-buckets-v3';
const LEGACY_STORAGE_KEY = 'usage-limit-windows';

// Human labels per known window type. Unknown types get a humanized fallback.
export const WINDOW_LABELS: Record<string, string> = {
	five_hour: '5-hour limit',
	seven_day: 'Weekly (all models)',
	seven_day_opus: 'Weekly Opus',
	seven_day_sonnet: 'Weekly Sonnet',
	seven_day_overage_included: 'Fable 5 limit',
	overage: 'Overage',
	// Non-Claude provider windows.
	day: 'Daily',
	month: 'Monthly',
	credit_limit: 'Credit limit',
	// SuperGrok subscription product windows (weekly is seven_day above).
	// GrokBuild is deliberately not surfaced — see `normalize_grok_billing`.
	grok_api: 'API',
	grok_chat: 'Grok Chat',
	// OpenAI/Anthropic API-key passive header capture (#757) — utilization is
	// "percent of window already used", same semantics as the Claude windows.
	requests: 'API requests',
	tokens: 'API tokens',
	input_tokens: 'Input tokens',
	output_tokens: 'Output tokens',
};

// Display order for the popover / headline selection tie-breaks.
export const WINDOW_ORDER = [
	'five_hour',
	'seven_day',
	'seven_day_opus',
	'seven_day_sonnet',
	'seven_day_overage_included',
	'overage',
	// Non-Claude provider windows.
	'day',
	'month',
	'credit_limit',
	'grok_api',
	'grok_chat',
	'requests',
	'tokens',
	'input_tokens',
	'output_tokens',
];

export function windowLabel(type: string): string {
	if (WINDOW_LABELS[type]) return WINDOW_LABELS[type];
	// Provider rolling windows come through as `window_<minutes>m`.
	const m = /^window_(\d+)m$/.exec(type);
	if (m) return `${m[1]}m window`;
	return type.replace(/_/g, ' ');
}

/** Clamp an already-percent (0–100) value into range. */
function _clampPct(u: number): number {
	if (typeof u !== 'number' || Number.isNaN(u) || !Number.isFinite(u)) return 0;
	return Math.min(100, Math.max(0, u));
}

/** Normalise an SDK fraction (0–1, defensively 0–100) to a 0–100 percent. */
function _fractionToPct(u: number): number {
	return _clampPct(u <= 1 ? u * 100 : u);
}

/** Parse `resets_at` (ISO string, epoch seconds, or epoch ms) into epoch ms. */
export function parseResetMs(resetsAt: string | number | null | undefined): number | null {
	if (resetsAt == null) return null;
	if (typeof resetsAt === 'number') {
		return resetsAt < 1e12 ? resetsAt * 1000 : resetsAt;
	}
	const trimmed = resetsAt.trim();
	if (trimmed === '') return null;
	if (/^\d+$/.test(trimmed)) {
		const n = Number(trimmed);
		return n < 1e12 ? n * 1000 : n;
	}
	const parsed = Date.parse(trimmed);
	return Number.isNaN(parsed) ? null : parsed;
}

/** Effective utilization for a window, accounting for a rolled-over window. */
export function effectivePct(w: UsageWindow, nowMs: number): number {
	const resetMs = parseResetMs(w.resets_at);
	if (resetMs != null && resetMs <= nowMs) return 0; // window rolled over
	return Math.round(w.utilization);
}

/** Reported remaining quota, returning a fresh window after rollover. */
export function reportedRemainingPct(w: UsageWindow, nowMs: number): number {
	const resetMs = parseResetMs(w.resets_at);
	if (resetMs != null && resetMs <= nowMs) return 100;
	return Math.round(w.utilization);
}

/** Convert reported used quota to remaining, returning a fresh window after rollover. */
export function remainingFromUsedPct(w: UsageWindow, nowMs: number): number {
	const resetMs = parseResetMs(w.resets_at);
	if (resetMs != null && resetMs <= nowMs) return 100;
	return Math.round(100 - w.utilization);
}

/** Format a countdown like "3h 12m" / "12m 4s" / "now". */
export function formatCountdown(resetsAt: string | null | undefined, nowMs: number): string {
	const target = parseResetMs(resetsAt);
	if (target == null) return '';
	const diffMs = target - nowMs;
	if (diffMs <= 0) return 'now';
	const totalSec = Math.ceil(diffMs / 1000);
	const d = Math.floor(totalSec / 86400);
	const h = Math.floor((totalSec % 86400) / 3600);
	const m = Math.floor((totalSec % 3600) / 60);
	const s = totalSec % 60;
	if (d > 0) return `${d}d ${h}h`;
	if (h > 0) return `${h}h ${m}m`;
	if (m > 0) return `${m}m ${s}s`;
	return `${s}s`;
}

/** Format an "as of" age from a fetched timestamp: "just now" / "4m ago". */
export function formatAsOf(fetchedAt: number | null | undefined, nowMs: number): string {
	if (fetchedAt == null) return '';
	const diffMs = nowMs - fetchedAt;
	if (diffMs < 60_000) return 'just now';
	const totalMin = Math.floor(diffMs / 60_000);
	const d = Math.floor(totalMin / 1440);
	const h = Math.floor((totalMin % 1440) / 60);
	const m = totalMin % 60;
	if (d > 0) return `${d}d ago`;
	if (h > 0) return `${h}h ago`;
	return `${m}m ago`;
}

/** How old a bucket's data may get before we surface an "as of" staleness line. */
export const BUCKET_STALE_MS = 5 * 60_000;

/** Windows as an array, ordered by WINDOW_ORDER then any extras alphabetically. */
export function orderedWindows(windows: Record<string, UsageWindow>): UsageWindow[] {
	const known = WINDOW_ORDER.filter((t) => t in windows).map((t) => windows[t]);
	const extra = Object.keys(windows)
		.filter((t) => !WINDOW_ORDER.includes(t))
		.sort()
		.map((t) => windows[t]);
	return [...known, ...extra];
}

/** Windows for display: ordered, minus degenerate zero-length rolling windows
 *  (`window_0m`) that older backends captured from ChatGPT plus-plan headers,
 *  and minus the Grok Build window — dropped at the source now, but persisted
 *  localStorage buckets may still carry it until the next poll. */
export function displayWindows(windows: Record<string, UsageWindow>): UsageWindow[] {
	return orderedWindows(windows).filter((w) => w.type !== 'window_0m' && w.type !== 'grok_build');
}

/** The key of the self (local install's) bucket for the given state. */
export function selfBucketKey(state: RateLimitState): string {
	return state.selfInstanceId ?? SELF_BUCKET_KEY;
}

/**
 * Resolve which bucket to display for a target install:
 *   - exact match on a non-null `instanceId` → that install's bucket;
 *   - `instanceId == null` (a local/self session) → the self bucket;
 *   - a non-null `instanceId` with no bucket, on a LEGACY store (no cluster info:
 *     `selfInstanceId == null` and a '__self__' bucket exists) → the self bucket.
 *     A legacy backend only knows the single local install, so any target maps to
 *     it. On a MODERN store (`selfInstanceId` known) we deliberately do NOT fall
 *     back — an unknown remote target must resolve to null rather than showing the
 *     LOCAL account's numbers, which would be a lie.
 */
export function resolveBucket(state: RateLimitState, instanceId: string | null): UsageBucket | null {
	if (instanceId != null) {
		const exact = state.buckets[instanceId];
		if (exact) return exact;
		if (state.selfInstanceId == null && state.buckets[SELF_BUCKET_KEY]) {
			return state.buckets[SELF_BUCKET_KEY];
		}
		return null;
	}
	return state.buckets[selfBucketKey(state)] ?? null;
}

/** The bucket key `resolveBucket` would return (for staleness lookup), or null. */
export function resolveBucketKey(state: RateLimitState, instanceId: string | null): string | null {
	if (instanceId != null) {
		if (state.buckets[instanceId]) return instanceId;
		if (state.selfInstanceId == null && state.buckets[SELF_BUCKET_KEY]) return SELF_BUCKET_KEY;
		return null;
	}
	const key = selfBucketKey(state);
	return state.buckets[key] ? key : null;
}

/** Whether a bucket's polled data has aged past `BUCKET_STALE_MS` (needs a refresh). */
export function isBucketStale(bucket: UsageBucket, now: number): boolean {
	return bucket.fetchedAt != null && now - bucket.fetchedAt > BUCKET_STALE_MS;
}

// ─── Persistence ─────────────────────────────────────────────────────────────

interface PersistedShape {
	buckets: Record<string, UsageBucket>;
	selfInstanceId: string | null;
	providerBuckets?: Record<string, ProviderUsageBucket>;
}

interface LoadedShape {
	buckets: Record<string, UsageBucket>;
	selfInstanceId: string | null;
	providerBuckets: Record<string, ProviderUsageBucket>;
}

function _load(): LoadedShape {
	try {
		const raw = localStorage.getItem(STORAGE_KEY_V3);
		if (raw) {
			const parsed = JSON.parse(raw) as PersistedShape;
			if (parsed && typeof parsed === 'object' && parsed.buckets && typeof parsed.buckets === 'object') {
				return {
					buckets: parsed.buckets,
					selfInstanceId: parsed.selfInstanceId ?? null,
					providerBuckets: parsed.providerBuckets ?? {},
				};
			}
			return { buckets: {}, selfInstanceId: null, providerBuckets: {} };
		}
		// v3 absent — migrate the legacy single-window map into a self bucket.
		const legacy = localStorage.getItem(LEGACY_STORAGE_KEY);
		if (legacy) {
			const windows = JSON.parse(legacy) as Record<string, UsageWindow>;
			localStorage.removeItem(LEGACY_STORAGE_KEY);
			if (windows && typeof windows === 'object' && Object.keys(windows).length > 0) {
				const bucket: UsageBucket = {
					windows,
					fetchedAt: null,
					available: true,
				};
				return { buckets: { [SELF_BUCKET_KEY]: bucket }, selfInstanceId: null, providerBuckets: {} };
			}
		}
		return { buckets: {}, selfInstanceId: null, providerBuckets: {} };
	} catch {
		return { buckets: {}, selfInstanceId: null, providerBuckets: {} }; // localStorage unavailable / malformed.
	}
}

function _save(state: RateLimitState) {
	try {
		const payload: PersistedShape = {
			buckets: state.buckets,
			selfInstanceId: state.selfInstanceId,
			providerBuckets: state.providerBuckets,
		};
		localStorage.setItem(STORAGE_KEY_V3, JSON.stringify(payload));
	} catch {
		// best-effort persistence
	}
}

const _loaded = _load();
const _store = createStore<RateLimitState>({
	buckets: _loaded.buckets,
	selfInstanceId: _loaded.selfInstanceId,
	providerBuckets: _loaded.providerBuckets,
	staleByInstance: {},
});

/**
 * Highest snapshot generation seen this session, PER install. In-memory (NOT
 * persisted) so a page reload — or a backend restart, which resets its counter
 * to 0 — starts fresh and never permanently rejects post-restart snapshots. A
 * snapshot entry with a strictly-lower generation than its install's high-water
 * mark is a stale straggler and is discarded.
 */
const _lastGenByInstance: Record<string, number> = {};

/**
 * Normalise a legacy top-level `fetched_at` (epoch SECONDS, float) into the
 * store's canonical epoch MILLISECONDS. Per-install `snapshots[].fetched_at`
 * already arrives in ms and is stored as-is.
 */
function _legacyFetchedMs(sec: number | null | undefined): number | null {
	if (sec == null || typeof sec !== 'number' || !Number.isFinite(sec)) return null;
	return Math.round(sec * 1000);
}

/** Build a window map from raw snapshot windows. */
function _windowsFromSnapshot(raw: SnapshotWindow[] | undefined, now: number): Record<string, UsageWindow> {
	const windows: Record<string, UsageWindow> = {};
	for (const w of raw ?? []) {
		if (!w?.type || typeof w.utilization !== 'number') continue;
		windows[w.type] = {
			type: w.type,
			utilization: _clampPct(w.utilization),
			resets_at: w.resets_at ?? null,
			received_at: now,
			source: 'poll',
		};
	}
	return windows;
}

export const rateLimit = {
	get current(): RateLimitState {
		return _store.current;
	},

	subscribe: _store.subscribe,

	createDerived: _store.createDerived,

	/** Apply a live SDK `rate_limit` event — refines a single window of the
	 *  emitting install's bucket. Routed by `instance_id`, falling back to the
	 *  self install when the event carries none (legacy backend). */
	applyLiveEvent(event: LiveRateLimitEvent): void {
		const { rate_limit_type, utilization } = event;
		// A data-less event (utilization null) must NOT create/wipe a window —
		// the CLI emits these before any usage is recorded.
		if (!rate_limit_type || utilization == null || typeof utilization !== 'number') return;
		const win: UsageWindow = {
			type: rate_limit_type,
			// SDK events report the USED fraction; the polled Claude feed — and every
			// consumer, e.g. `reportedRemainingPct` — treats a window's utilization as
			// quota REMAINING, so convert at ingest.
			utilization: 100 - _fractionToPct(utilization),
			resets_at: event.resets_at ?? null,
			received_at: Date.now(),
			source: 'live',
		};
		_store.update((s) => {
			const key = event.instance_id ?? s.selfInstanceId ?? SELF_BUCKET_KEY;
			const prev = s.buckets[key];
			const bucket: UsageBucket = {
				...(prev ?? { windows: {}, fetchedAt: null, available: true }),
				windows: { ...(prev?.windows ?? {}), [rate_limit_type]: win },
				available: true,
			};
			const buckets = { ...s.buckets, [key]: bucket };
			const next = { ...s, buckets };
			_save(next);
			return next;
		});
	},

	/**
	 * Mark the local (self) install's bucket stale — call at the start of an
	 * account switch so its pill dims until the fresh snapshot arrives. Scoped to
	 * the self bucket: an offloaded session on another install is unaffected.
	 */
	markStale(): void {
		_store.update((s) => ({
			...s,
			staleByInstance: { ...s.staleByInstance, [selfBucketKey(s)]: true },
		}));
	},

	/** Apply a polled snapshot (WS `usage_snapshot` or REST `/usage/limits`). */
	applySnapshot(snapshot: UsageSnapshot): void {
		const now = Date.now();

		// ── Provider usage (non-Claude) — full replace when present ─────────────
		// Providers are provider-scoped, not install-scoped, so none of the
		// per-install generation/merge logic applies: the endpoint returns the
		// authoritative current set. Fold FIRST (as its own update) — the Claude
		// paths below early-return, so an "after" placement would be unreachable
		// for a modern payload. When the `providers` key is ABSENT we leave the
		// existing map untouched, so a WS-only payload can't wipe it. The Claude
		// updates below carry `providerBuckets` forward unchanged.
		if (Array.isArray(snapshot.providers)) {
			_store.update((s) => {
				const providerBuckets: Record<string, ProviderUsageBucket> = {};
				for (const p of snapshot.providers!) {
					if (!p?.provider_id) continue;
					providerBuckets[p.provider_id] = {
						providerId: p.provider_id,
						providerName: p.provider_name ?? null,
						providerType: p.provider_type ?? null,
						planType: p.plan_type ?? null,
						windows: _windowsFromSnapshot(p.windows, now),
						credits: p.credits ?? null,
						fetchedAt: p.fetched_at ?? null,
					};
				}
				const next = { ...s, providerBuckets };
				_save(next);
				return next;
			});
		}

		// ── Modern path: per-install snapshots MERGE into the buckets map ───────
		// Require a non-empty array: an empty `snapshots: []` is not authoritative
		// (e.g. a transient backend DB read failure), so we fall through to the
		// legacy fold below rather than wiping every bucket.
		if (Array.isArray(snapshot.snapshots) && snapshot.snapshots.length > 0) {
			_store.update((s) => {
				const buckets: Record<string, UsageBucket> = {};
				const staleByInstance = { ...s.staleByInstance };
				const incomingKeys = new Set<string>();
				for (const entry of snapshot.snapshots!) {
					if (!entry?.instance_id) continue;
					const key = entry.instance_id;
					incomingKeys.add(key);
					const prev = s.buckets[key];

					// Per-install generation guard: a straggler from an older
					// generation is dropped — carry the previous bucket forward. A
					// null generation is accepted but must NOT lower the high-water
					// mark (that would re-admit older out-of-order snapshots).
					const gen = entry.generation;
					if (gen != null) {
						if (gen < (_lastGenByInstance[key] ?? -1)) {
							if (prev) buckets[key] = prev;
							continue;
						}
						_lastGenByInstance[key] = gen;
					}

					// A fresh snapshot for this install resolves any pending switch.
					staleByInstance[key] = false;

					const meta = {
						accountId: entry.account_id ?? null,
						accountEmail: entry.account_email ?? null,
						accountName: entry.account_name ?? null,
						deviceName: entry.device_name ?? null,
						planType: entry.plan_type ?? null,
						online: entry.online ?? null,
						generation: gen ?? null,
						fetchedAt: entry.fetched_at ?? null,
					};

					if (!entry.available) {
						// No usable credential: keep last-known windows rather than
						// flashing the pill away, but reflect availability.
						buckets[key] = {
							...meta,
							windows: prev?.windows ?? {},
							available: false,
						};
					} else {
						buckets[key] = {
							...meta,
							windows: _windowsFromSnapshot(entry.windows, now),
							available: true,
						};
					}
				}

				// Preserve existing buckets whose install is ABSENT from this
				// snapshot (e.g. a transient registry dropout, or a self bucket
				// restored via live events before the next full poll). Carry them
				// forward unchanged and mark them stale so the UI signals the data
				// is no longer confirmed by a fresh snapshot.
				for (const key of Object.keys(s.buckets)) {
					if (incomingKeys.has(key) || buckets[key]) continue;
					buckets[key] = s.buckets[key];
					staleByInstance[key] = true;
				}
				// Prune stale flags for keys that ended up with no bucket at all.
				for (const key of Object.keys(staleByInstance)) {
					if (!buckets[key]) delete staleByInstance[key];
				}

				const next: RateLimitState = {
					buckets,
					providerBuckets: s.providerBuckets,
					selfInstanceId: snapshot.self_instance_id ?? null,
					staleByInstance,
				};
				_save(next);
				return next;
			});
			return;
		}

		// ── Legacy / degraded fold: top-level windows → the CURRENT self bucket ──
		// Reached for a genuinely-legacy backend (no `snapshots` support) AND for a
		// degraded modern payload (`snapshots` omitted or empty, e.g. a transient
		// backend DB failure). Either way the payload describes only the LOCAL
		// install, so it folds NON-destructively into the store's current self
		// bucket: every other install's bucket, `selfInstanceId`, and the self
		// bucket's account/device metadata are all preserved. (Wiping them would
		// null selfInstanceId and arm resolveBucket's legacy self-fallback, serving
		// the LOCAL account's numbers for an offloaded session — the original
		// mislabel bug.) On a genuinely-legacy store selfInstanceId was never set,
		// so the fold key stays '__self__' and behavior is unchanged.
		_store.update((s) => {
			const key = selfBucketKey(s);
			const gen = snapshot.generation;
			if (gen == null) {
				// No generation tag (legacy backend / tests). Treat as authoritative
				// and reset the guard so it can't wedge on a stale high-water mark.
				_lastGenByInstance[key] = -1;
			} else {
				if (gen < (_lastGenByInstance[key] ?? -1)) return s; // straggler — drop
				_lastGenByInstance[key] = gen;
			}

			const staleByInstance = { ...s.staleByInstance, [key]: false };
			const prev = s.buckets[key];
			let bucket: UsageBucket;
			if (!snapshot.available) {
				// Keep last-known windows; reflect availability; clear the switch flag.
				bucket = {
					...(prev ?? { windows: {}, fetchedAt: null }),
					available: false,
					fetchedAt: _legacyFetchedMs(snapshot.fetched_at) ?? prev?.fetchedAt ?? null,
				};
			} else {
				// Spread `prev` so a modern self bucket keeps its account/device
				// metadata (labels) through a degraded payload; override only the
				// data fields the legacy payload actually carries.
				bucket = {
					...(prev ?? {}),
					windows: _windowsFromSnapshot(snapshot.windows, now),
					fetchedAt: _legacyFetchedMs(snapshot.fetched_at),
					available: true,
					generation: gen ?? null,
				};
			}
			const next: RateLimitState = {
				buckets: { ...s.buckets, [key]: bucket },
				providerBuckets: s.providerBuckets,
				selfInstanceId: s.selfInstanceId,
				staleByInstance,
			};
			_save(next);
			return next;
		});
	},

	/**
	 * Test-only: reset the store to a pristine empty state AND clear the
	 * per-install generation high-water marks. The module-level
	 * `_lastGenByInstance` otherwise leaks install keys between tests (the legacy
	 * reset path only clears the self key), letting an earlier test's generation
	 * wedge a later one.
	 */
	__resetForTests(): void {
		for (const k of Object.keys(_lastGenByInstance)) delete _lastGenByInstance[k];
		_store.set({ buckets: {}, providerBuckets: {}, selfInstanceId: null, staleByInstance: {} });
		_save(_store.current);
	},
};
