/**
 * Usage-limits store — per-INSTALL buckets of Claude subscription usage.
 *
 * The usage windows belong to whichever Claude login an install's CLI uses, and
 * in a clustered deployment (primary + worker sharing one Postgres) each install
 * may be signed in differently. A session can be offloaded to the worker, so the
 * usage a viewer cares about is the usage of the install that actually RUNS the
 * viewed session — not necessarily the local install's. This store therefore
 * keeps one `UsageBucket` PER install (keyed by `instance_id`) rather than a
 * single global window set. The widget resolves which bucket to show from the
 * currently viewed/composed session (see `usage-target.ts`).
 *
 * Fed by BOTH:
 *   1. A snapshot (`usage_snapshot` WS event + the REST `/usage/limits`
 *      endpoint) — the windows each install's CLI has reported so far, as the
 *      backend persisted them. Utilization is already 0–100 percent. The modern
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
 * `overage`). Buckets are persisted to localStorage so the usage pill/popover
 * stay populated across reloads.
 *
 * Plain `.ts` (no $state runes) so it can be imported before component init.
 *
 * Consumers: `ContextUsageBar.svelte` (chat-toolbar pill + popover) resolves a
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
	/**
	 * Epoch ms this window was last observed: the live event's arrival, or the
	 * backend's per-window `observed_at`. Staleness is per window, since one
	 * event dates one window. `null` = unknown: a Claude window persisted before
	 * per-window dating (no `observed_at`) must not borrow the snapshot's
	 * `fetched_at`, which any other window's report advances. Provider windows
	 * are fetched together, so they fall back to the snapshot's `fetched_at`.
	 */
	received_at: number | null;
	source: 'poll' | 'live';
	/** In a merged account bucket: the install whose reading this is. */
	reportedBy?: UsageReporter | null;
}

/** The install that reported a window of a merged account bucket. */
export interface UsageReporter {
	instanceId: string | null;
	deviceId: string | null;
	deviceName: string | null;
}

/** One install's usage snapshot: its window set plus where it belongs. */
export interface UsageBucket {
	/** Keyed by window type. */
	windows: Record<string, UsageWindow>;
	/** Epoch ms the backend last fetched this install's usage (staleness). */
	fetchedAt: number | null;
	/** Whether this install's CLI has reported any usage window yet. */
	available: boolean;
	/** Network device id of this install; sessions name their owner by it. */
	deviceId?: string | null;
	deviceName?: string | null;
	/** Owning install seen recently (heartbeat). null = unknown. */
	online?: boolean | null;
	/** Monotonic generation tag from the backend for THIS install (see below). */
	generation?: number | null;
	/** Opaque key of the Claude account this install's CLI is signed in to;
	 *  null/absent = unknown (not signed in, or an older backend). */
	accountKey?: string | null;
}

/**
 * Installs signed in to one Claude account, merged by the backend window by
 * window (newest `observed_at` wins). Each window names its reporter.
 */
export interface AccountBucket {
	accountKey: string;
	/** Bucket keys (install ids) of the member installs. */
	instanceIds: string[];
	windows: Record<string, UsageWindow>;
	fetchedAt: number | null;
	available: boolean;
}

/**
 * One non-Claude provider's usage snapshot (Codex/ChatGPT, OpenRouter, …).
 * Provider-scoped (not install-scoped), so none of the per-install
 * generation/merge logic applies — the endpoint returns the authoritative
 * current set and we full-replace on each poll.
 */
export type ProviderUsageStatus = 'fresh' | 'stale' | 'unavailable' | 'never_fetched';
export type ProviderUsageErrorKind =
	| 'billing_access_denied'
	| 'missing_credentials'
	| 'timeout'
	| 'unavailable'
	| 'unsupported';

/** What a reading means; never present one kind as another. */
export type UsageMeasure = 'rate_limit' | 'subscription_allowance' | 'reported_spend' | 'monetary_credit';
export type UsageSupport = 'documented' | 'response_metadata' | 'unofficial_subscription' | 'undocumented';

/** Backend registry entry describing where a usage reading comes from. */
export interface UsageSourceInfo {
	id: string;
	label: string;
	scope: string;
	scope_label: string;
	measure: UsageMeasure;
	measure_label: string;
	collection: 'passive' | 'polled';
	support: UsageSupport;
	support_label: string;
	dashboard_url: string;
	min_interval_seconds?: number | null;
	/** Figures this source cannot report (shown as unavailable). */
	unavailable?: string[];
}

/** The local Claude subscription reading's source and last attempt. */
export interface ClaudeUsageSourceState {
	source: UsageSourceInfo | null;
	/** Epoch ms of the last upstream attempt (success or failure). */
	attemptedAt: number | null;
	lastErrorKind: ProviderUsageErrorKind | null;
}

export interface ProviderUsageBucket {
	source?: string | null;
	scope?: string | null;
	sourceInfo?: UsageSourceInfo | null;
	/** Epoch ms of the last attempt; later than `fetchedAt` after a failure. */
	lastAttemptAt?: number | null;
	/** Epoch ms before which a refresh serves this cached reading. */
	cooldownUntil?: number | null;
	status?: ProviderUsageStatus;
	lastErrorKind?: ProviderUsageErrorKind | null;
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
	/** Keyed by account key: installs sharing one Claude account, merged. */
	accountBuckets?: Record<string, AccountBucket>;
	/** The local install's instance id, when the backend reports it. null in the
	 *  legacy path — the self bucket is then keyed `'__self__'`. */
	selfInstanceId: string | null;
	/** The local install's network device id, when it has one. */
	selfDeviceId?: string | null;
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
	/** The CLI's verdict: `allowed`, `allowed_warning` or `rejected` (exhausted). */
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
	/** Epoch ms the CLI last reported this window (Claude windows only). */
	observed_at?: number | null;
}

/** A merged account window: also names the install that reported it. */
export interface SnapshotAccountWindow extends SnapshotWindow {
	instance_id?: string | null;
	device_id?: string | null;
	device_name?: string | null;
}

/** One entry of the snapshot's `accounts` array (two or more installs). */
export interface SnapshotAccount {
	account_key: string;
	instance_ids?: (string | null)[];
	device_ids?: string[];
	windows: SnapshotAccountWindow[];
	fetched_at?: number | null;
	available: boolean;
	generation?: number | null;
}

/** One install's entry in the modern `snapshots` array. */
export interface SnapshotInstance {
	instance_id: string;
	/** Network device id of the install (absent off a network / on old backends). */
	device_id?: string | null;
	device_name?: string | null;
	online?: boolean | null;
	windows: SnapshotWindow[];
	/** Epoch MILLISECONDS (the legacy top-level `fetched_at` is seconds). */
	fetched_at?: number | null;
	available: boolean;
	generation?: number | null;
	/** Opaque Claude account key (absent from older backends = no key). */
	account_key?: string | null;
}

/**
 * One non-Claude provider's entry in a polled snapshot's `providers` array
 * (U4 wire shape — the `raw` debug blob is never sent). `windows` reuses the
 * Claude `SnapshotWindow` shape (`type` / `utilization` 0–100 / `resets_at`).
 */
export interface ProviderUsageEntry {
	source?: string | null;
	scope?: string | null;
	source_info?: UsageSourceInfo | null;
	last_success_at?: number | null;
	last_attempt_at?: number | null;
	cooldown_until?: number | null;
	status?: ProviderUsageStatus;
	last_error_kind?: ProviderUsageErrorKind | null;
	provider_id: string;
	provider_name?: string | null;
	provider_type?: string | null;
	plan_type?: string | null;
	windows?: SnapshotWindow[];
	credits?: Record<string, unknown> | null;
	fetched_at?: number | null;
}

export interface UsageSnapshot {
	provider_refreshing?: boolean;
	/** Local Claude reading: registry source, last attempt (epoch SECONDS,
	 *  like the legacy `fetched_at`) and why it failed. */
	claude_source?: UsageSourceInfo | null;
	attempted_at?: number | null;
	last_error_kind?: ProviderUsageErrorKind | null;
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
	/** The local install's network device id. */
	self_device_id?: string | null;
	/** Installs on one Claude account, merged by the backend. Replaces
	 *  `accountBuckets` with every modern (`snapshots`) payload. */
	accounts?: SnapshotAccount[];
	/** Non-Claude provider usage. Full-replaces `providerBuckets` when present;
	 *  when the key is ABSENT the store leaves the existing map untouched (so a
	 *  WS-only payload without providers can't wipe it). Additive; empty for old
	 *  or provider-less installs; null when the backend could not read them. */
	providers?: ProviderUsageEntry[] | null;
}

// Bumped to v3 when the backend started reporting Claude window utilization as
// quota REMAINING: any cached v2 bucket holds percent-USED numbers that would
// render as "1% left" on a barely-touched window until the next poll lands.
// A fresh key discards them outright rather than showing a wrong figure first.
const STORAGE_KEY_V3 = 'usage-limit-buckets-v3';
const LEGACY_STORAGE_KEY = 'usage-limit-windows';

// Human labels per known window type. Unknown types get a humanized fallback.
const WINDOW_LABELS: Record<string, string> = {
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
const WINDOW_ORDER = [
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

/** Normalise an SDK used fraction (0–1, above 1 when over the limit) to a 0–100 percent. */
function _fractionToPct(u: number): number {
	return _clampPct(u * 100);
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
	// Backend timestamps are UTC. `Date.parse` reads a zone-less date-time as
	// the browser's LOCAL time, which would shift the reset by the UTC offset.
	const naive = /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?$/.test(trimmed);
	const parsed = Date.parse(naive ? `${trimmed.replace(' ', 'T')}Z` : trimmed);
	return Number.isNaN(parsed) ? null : parsed;
}

/**
 * Round a 0–100 percent for display without claiming an edge it has not
 * reached: 0.4 shows as 1 (not "0% left" while quota remains) and 99.6 as 99
 * (not "100% left" once some was used). Exact 0 and 100 stay as they are.
 */
function _displayPct(value: number): number {
	const pct = _clampPct(value);
	const rounded = Math.round(pct);
	if (rounded === 0 && pct > 1e-6) return 1;
	if (rounded === 100 && pct < 100 - 1e-6) return 99;
	return rounded;
}

/**
 * Whether the window's reset time has passed. Its last report then describes
 * the previous period: the window has refilled, and the next report (which may
 * never come — Claude's weekly Fable bucket is reported only on Fable turns)
 * describes the new one.
 */
export function windowRolledOver(w: Pick<UsageWindow, 'resets_at'>, nowMs: number): boolean {
	const resetMs = parseResetMs(w.resets_at);
	return resetMs != null && resetMs <= nowMs;
}

/** Used quota as last reported; 0 once the window has reset. */
export function effectivePct(w: UsageWindow, nowMs: number): number {
	if (windowRolledOver(w, nowMs)) return 0;
	return _displayPct(w.utilization);
}

/** Remaining quota as last reported; full once the window has reset. */
export function reportedRemainingPct(w: UsageWindow, nowMs: number): number {
	if (windowRolledOver(w, nowMs)) return 100;
	return _displayPct(w.utilization);
}

/** Remaining quota from a used-quota report; full once the window has reset. */
export function remainingFromUsedPct(w: UsageWindow, nowMs: number): number {
	if (windowRolledOver(w, nowMs)) return 100;
	return _displayPct(100 - _clampPct(w.utilization));
}

/** Format a countdown like "3h 12m" / "12m 4s"; '' once the reset has passed. */
export function formatCountdown(resetsAt: string | null | undefined, nowMs: number): string {
	const target = parseResetMs(resetsAt);
	if (target == null) return '';
	const diffMs = target - nowMs;
	if (diffMs <= 0) return '';
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
function orderedWindows(windows: Record<string, UsageWindow>): UsageWindow[] {
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
	const key = resolveBucketKey(state, instanceId);
	if (key == null) return null;
	const bucket = state.buckets[key];
	const account = accountBucketFor(state, key);
	if (!bucket || !account) return bucket ?? null;
	// The same Claude account on several installs: show the freshest reading
	// of each window, whichever install reported it. The target's own
	// device fields stay, so labels and staleness still name the target.
	return {
		...bucket,
		windows: account.windows,
		fetchedAt: account.fetchedAt ?? bucket.fetchedAt,
		available: account.available || bucket.available,
	};
}

/** The merged account bucket an install's bucket belongs to, if any. */
export function accountBucketFor(state: RateLimitState, bucketKey: string): AccountBucket | null {
	const accountKey = state.buckets[bucketKey]?.accountKey;
	if (!accountKey) return null;
	const account = state.accountBuckets?.[accountKey];
	return account && account.instanceIds.includes(bucketKey) ? account : null;
}

/**
 * Names of the installs whose readings a bucket shows, newest first; empty
 * for a plain per-install bucket (its windows carry no reporter).
 */
export function bucketReporters(bucket: UsageBucket): string[] {
	const newest = new Map<string, number>();
	for (const w of Object.values(bucket.windows ?? {})) {
		const name = w.reportedBy?.deviceName || w.reportedBy?.deviceId || w.reportedBy?.instanceId;
		if (!name) continue;
		const at = windowObservedAtKnown(w) ? w.received_at : -1;
		newest.set(name, Math.max(newest.get(name) ?? -Infinity, at));
	}
	return [...newest.entries()].sort((a, b) => b[1] - a[1]).map(([name]) => name);
}

/**
 * The bucket key `resolveBucket` would return (for staleness lookup), or null.
 * A target may be an install id or a network device id (sessions name their
 * owner by device id; buckets are keyed by install id and carry `deviceId`).
 */
export function resolveBucketKey(state: RateLimitState, instanceId: string | null): string | null {
	if (instanceId != null) {
		if (state.buckets[instanceId]) return instanceId;
		const byDevice = Object.keys(state.buckets).find((k) => state.buckets[k]?.deviceId === instanceId);
		if (byDevice) return byDevice;
		if (instanceId === state.selfDeviceId) {
			const key = selfBucketKey(state);
			return state.buckets[key] ? key : null;
		}
		if (state.selfInstanceId == null && state.buckets[SELF_BUCKET_KEY]) return SELF_BUCKET_KEY;
		return null;
	}
	const key = selfBucketKey(state);
	return state.buckets[key] ? key : null;
}

/** Whether a usage target (install or device id; null = local) is this install. */
export function isSelfTarget(state: RateLimitState, target: string | null): boolean {
	return target == null || target === state.selfInstanceId || (target != null && target === state.selfDeviceId);
}

/** Whether a window's observation time is known (a finite epoch ms). */
export function windowObservedAtKnown<W extends Pick<UsageWindow, 'received_at'>>(w: W): w is W & { received_at: number } {
	return typeof w.received_at === 'number' && Number.isFinite(w.received_at);
}

/**
 * Windows whose shown value depends on their last report. A window past its
 * reset shows as refilled whatever it last reported, so its age (often days
 * for the weekly Fable bucket) says nothing about the bucket's freshness.
 * Without `nowMs` every window counts.
 */
function reportedWindows(bucket: UsageBucket, nowMs?: number): UsageWindow[] {
	const all = Object.values(bucket.windows ?? {});
	return nowMs == null ? all : all.filter((w) => !windowRolledOver(w, nowMs));
}

/**
 * Claude's main windows, reported with every poll and turn. The narrower ones
 * (per-model weekly, Fable, overage) can go hours without a report (#2117).
 */
const MAIN_WINDOWS = new Set(['five_hour', 'seven_day']);

/**
 * The reported windows that date the bucket: its main windows when it has
 * any, so one rarely reported window cannot age the whole bucket (#2117).
 * A bucket without main windows (a single live event, another provider) is
 * dated by all of them.
 */
function datingWindows(bucket: UsageBucket, nowMs?: number): UsageWindow[] {
	const reported = reportedWindows(bucket, nowMs);
	const main = reported.filter((w) => MAIN_WINDOWS.has(w.type));
	return main.length > 0 ? main : reported;
}

/**
 * Dated reported windows checked well before the bucket's check time
 * (more than `BUCKET_STALE_MS` earlier), oldest first, so a caller can name
 * their own age next to it.
 */
export function bucketLaggingWindows(bucket: UsageBucket, nowMs?: number): UsageWindow[] {
	const observedAt = bucketObservedAt(bucket, nowMs);
	if (observedAt == null) return [];
	return reportedWindows(bucket, nowMs)
		.filter(windowObservedAtKnown)
		.filter((w) => observedAt - w.received_at > BUCKET_STALE_MS)
		.sort((a, b) => a.received_at - b.received_at);
}

/** Whether any of the bucket's reported windows has an unknown observation time. */
export function bucketHasUndatedWindow(bucket: UsageBucket, nowMs?: number): boolean {
	return reportedWindows(bucket, nowMs).some((w) => !windowObservedAtKnown(w));
}

/**
 * When the bucket's OLDEST dated reported window was last observed (epoch ms),
 * counting only its main windows when it has any (see `datingWindows`), or
 * the bucket's `fetchedAt` when it has no dated windows. A live event dates
 * only its own window, so the other windows keep their age. Undated windows
 * are reported by `bucketHasUndatedWindow`, never by borrowing `fetchedAt`.
 */
export function bucketObservedAt(bucket: UsageBucket, nowMs?: number): number | null {
	const times = datingWindows(bucket, nowMs)
		.filter(windowObservedAtKnown)
		.map((w) => w.received_at);
	if (times.length > 0) return Math.min(...times);
	if (bucketHasUndatedWindow(bucket, nowMs)) return null;
	// Only refilled windows remain: date the bucket by its newest report.
	const all = Object.values(bucket.windows ?? {}).filter(windowObservedAtKnown).map((w) => w.received_at);
	return all.length > 0 ? Math.max(...all) : bucket.fetchedAt;
}

/**
 * A provider row's status as of `nowMs`. The backend dates `fresh` when it
 * answers, but rows also come from localStorage and outlive a failed refresh,
 * so a `fresh` reading older than its source's interval (the backend's own
 * threshold, 300 s by default) is shown as `stale`.
 */
export function providerStatusAt(
	pb: Pick<ProviderUsageBucket, 'status' | 'fetchedAt' | 'sourceInfo'>,
	nowMs: number
): ProviderUsageStatus | undefined {
	if (pb.status !== 'fresh' || pb.fetchedAt == null) return pb.status;
	const intervalMs = (pb.sourceInfo?.min_interval_seconds || 300) * 1000;
	return nowMs - pb.fetchedAt > intervalMs ? 'stale' : 'fresh';
}

/** Whether any of a bucket's windows has aged past `BUCKET_STALE_MS` (needs a refresh). */
export function isBucketStale(bucket: UsageBucket, now: number): boolean {
	// An undated window's reading is of unknown age: never present it as fresh.
	if (bucketHasUndatedWindow(bucket, now)) return true;
	const observedAt = bucketObservedAt(bucket, now);
	return observedAt != null && now - observedAt > BUCKET_STALE_MS;
}

// ─── Persistence ─────────────────────────────────────────────────────────────

interface PersistedShape {
	buckets: Record<string, UsageBucket>;
	selfInstanceId: string | null;
	selfDeviceId?: string | null;
	providerBuckets?: Record<string, ProviderUsageBucket>;
	accountBuckets?: Record<string, AccountBucket>;
}

interface LoadedShape {
	buckets: Record<string, UsageBucket>;
	selfInstanceId: string | null;
	selfDeviceId?: string | null;
	providerBuckets: Record<string, ProviderUsageBucket>;
	accountBuckets?: Record<string, AccountBucket>;
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
					selfDeviceId: parsed.selfDeviceId ?? null,
					providerBuckets: parsed.providerBuckets ?? {},
					accountBuckets: parsed.accountBuckets ?? {},
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
			selfDeviceId: state.selfDeviceId ?? null,
			providerBuckets: state.providerBuckets,
			accountBuckets: state.accountBuckets ?? {},
		};
		localStorage.setItem(STORAGE_KEY_V3, JSON.stringify(payload));
	} catch {
		// best-effort persistence
	}
}

/** Runtime-only (not persisted): where the local Claude reading comes from. */
const _claudeSource = createStore<ClaudeUsageSourceState>({
	source: null,
	attemptedAt: null,
	lastErrorKind: null,
});
export const claudeUsageSource = { subscribe: _claudeSource.subscribe };

const _loaded = _load();
const _store = createStore<RateLimitState>({
	buckets: _loaded.buckets,
	selfInstanceId: _loaded.selfInstanceId,
	selfDeviceId: _loaded.selfDeviceId ?? null,
	providerBuckets: _loaded.providerBuckets,
	accountBuckets: _loaded.accountBuckets ?? {},
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

/**
 * Build a window map from raw snapshot windows. Each window is dated by its own
 * `observed_at`. Without one, a provider window (`perWindowDates: false`, the
 * whole set fetched together) falls back to the snapshot's `fetchedAt` (epoch
 * ms), else `now`; a Claude window (`perWindowDates: true`) stays undated,
 * because its snapshot's `fetched_at` moves whenever any OTHER window reports.
 */
function _windowsFromSnapshot(
	raw: SnapshotWindow[] | undefined,
	now: number,
	fetchedAt: number | null | undefined,
	perWindowDates: boolean
): Record<string, UsageWindow> {
	const windows: Record<string, UsageWindow> = {};
	for (const w of raw ?? []) {
		if (!w?.type || typeof w.utilization !== 'number') continue;
		const observed =
			typeof w.observed_at === 'number' && Number.isFinite(w.observed_at) ? w.observed_at : null;
		windows[w.type] = {
			type: w.type,
			utilization: _clampPct(w.utilization),
			resets_at: w.resets_at ?? null,
			received_at: observed ?? (perWindowDates ? null : (fetchedAt ?? now)),
			source: 'poll',
		};
	}
	return windows;
}

/**
 * Account buckets from a snapshot's `accounts`. A window the previous bucket
 * of the same account holds with a strictly newer reading (a live event that
 * arrived since, or a straggling older snapshot) is kept.
 */
function _accountBucketsFromSnapshot(
	raw: SnapshotAccount[] | undefined,
	prev: Record<string, AccountBucket> | undefined,
	now: number
): Record<string, AccountBucket> {
	const accounts: Record<string, AccountBucket> = {};
	for (const a of raw ?? []) {
		if (!a?.account_key) continue;
		const windows = _windowsFromSnapshot(a.windows, now, a.fetched_at, true);
		for (const w of a.windows ?? []) {
			if (!w?.type || !windows[w.type]) continue;
			windows[w.type].reportedBy = {
				instanceId: w.instance_id ?? null,
				deviceId: w.device_id ?? null,
				deviceName: w.device_name ?? null,
			};
		}
		for (const old of Object.values(prev?.[a.account_key]?.windows ?? {})) {
			const incoming = windows[old.type];
			if (
				incoming &&
				windowObservedAtKnown(old) &&
				(!windowObservedAtKnown(incoming) || old.received_at > incoming.received_at)
			) {
				windows[old.type] = old;
			}
		}
		accounts[a.account_key] = {
			accountKey: a.account_key,
			instanceIds: (a.instance_ids ?? []).filter((id): id is string => !!id),
			windows,
			fetchedAt: a.fetched_at ?? null,
			available: !!a.available,
		};
	}
	return accounts;
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
		if (!rate_limit_type) return;
		// `rejected` means the CLI cut this window off: exhausted, whether or not
		// the event carries a utilization.
		const rejected = event.status === 'rejected';
		// A data-less event (utilization null) must NOT create/wipe a window —
		// the CLI emits these before any usage is recorded.
		if (!rejected && (utilization == null || typeof utilization !== 'number')) return;
		const now = Date.now();
		const win: UsageWindow = {
			type: rate_limit_type,
			// SDK events report the USED fraction; the polled Claude feed — and every
			// consumer, e.g. `reportedRemainingPct` — treats a window's utilization as
			// quota REMAINING, so convert at ingest.
			utilization: rejected ? 0 : 100 - _fractionToPct(utilization as number),
			resets_at: event.resets_at ?? null,
			received_at: now,
			source: 'live',
		};
		_store.update((s) => {
			const key = event.instance_id ?? s.selfInstanceId ?? SELF_BUCKET_KEY;
			const prev = s.buckets[key];
			const bucket: UsageBucket = {
				...(prev ?? { windows: {}, fetchedAt: null, available: true }),
				windows: { ...(prev?.windows ?? {}), [rate_limit_type]: win },
				// `received_at` dates this window only: an unchanged reading is still
				// current, but the bucket's other windows keep their own age.
				available: true,
			};
			const buckets = { ...s.buckets, [key]: bucket };
			// A live reading is the newest of its account: it takes that window
			// of the merged bucket too, naming this install as the reporter.
			let accountBuckets = s.accountBuckets;
			const account = accountBucketFor({ ...s, buckets }, key);
			if (account && accountBuckets) {
				const reportedBy: UsageReporter = {
					instanceId: key,
					deviceId: bucket.deviceId ?? null,
					deviceName: bucket.deviceName ?? null,
				};
				accountBuckets = {
					...accountBuckets,
					[account.accountKey]: {
						...account,
						windows: { ...account.windows, [rate_limit_type]: { ...win, reportedBy } },
						available: true,
					},
				};
			}
			const next = { ...s, buckets, accountBuckets };
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

		// REST payloads describe the local Claude reading's source; WS payloads
		// carry only the attempt fields. Absent keys leave the last value.
		if (snapshot.claude_source !== undefined || snapshot.attempted_at !== undefined) {
			_claudeSource.update((prev) => ({
				source: snapshot.claude_source !== undefined ? (snapshot.claude_source ?? null) : prev.source,
				attemptedAt:
					snapshot.attempted_at !== undefined ? _legacyFetchedMs(snapshot.attempted_at) : prev.attemptedAt,
				lastErrorKind:
					snapshot.attempted_at !== undefined ? (snapshot.last_error_kind ?? null) : prev.lastErrorKind,
			}));
		}

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
						source: p.source ?? null,
						scope: p.scope ?? null,
						sourceInfo: p.source_info ?? null,
						lastAttemptAt: p.last_attempt_at ?? p.fetched_at ?? null,
						cooldownUntil: p.cooldown_until ?? null,
						status: p.status,
						lastErrorKind: p.last_error_kind ?? null,
						providerName: p.provider_name ?? null,
						providerType: p.provider_type ?? null,
						planType: p.plan_type ?? null,
						windows: _windowsFromSnapshot(p.windows, now, p.fetched_at, false),
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
					let sameGeneration = false;
					if (gen != null) {
						if (gen < (_lastGenByInstance[key] ?? -1)) {
							if (prev) buckets[key] = prev;
							continue;
						}
						sameGeneration = gen === _lastGenByInstance[key];
						_lastGenByInstance[key] = gen;
					}

					// A fresh snapshot for this install resolves any pending switch.
					staleByInstance[key] = false;

					const meta = {
						deviceId: entry.device_id ?? null,
						deviceName: entry.device_name ?? null,
						online: entry.online ?? null,
						generation: gen ?? null,
						fetchedAt: entry.fetched_at ?? null,
						accountKey: entry.account_key ?? null,
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
						const windows = _windowsFromSnapshot(entry.windows, now, entry.fetched_at, true);
						// An unchanged generation means nothing changed server-side since
						// the last snapshot, so a live window received since is newer.
						if (sameGeneration) {
							for (const w of Object.values(prev?.windows ?? {})) {
								if (w.source === 'live') windows[w.type] = w;
							}
						}
						buckets[key] = { ...meta, windows, available: true };
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
					accountBuckets: _accountBucketsFromSnapshot(snapshot.accounts, s.accountBuckets, now),
					selfInstanceId: snapshot.self_instance_id ?? null,
					selfDeviceId: snapshot.self_device_id ?? null,
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
					windows: _windowsFromSnapshot(snapshot.windows, now, _legacyFetchedMs(snapshot.fetched_at), true),
					fetchedAt: _legacyFetchedMs(snapshot.fetched_at),
					available: true,
					generation: gen ?? null,
				};
			}
			const next: RateLimitState = {
				buckets: { ...s.buckets, [key]: bucket },
				providerBuckets: s.providerBuckets,
				accountBuckets: s.accountBuckets,
				selfInstanceId: s.selfInstanceId,
				selfDeviceId: s.selfDeviceId ?? null,
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
		_store.set({
			buckets: {},
			providerBuckets: {},
			accountBuckets: {},
			selfInstanceId: null,
			selfDeviceId: null,
			staleByInstance: {},
		});
		_claudeSource.set({ source: null, attemptedAt: null, lastErrorKind: null });
		_save(_store.current);
	},
};
