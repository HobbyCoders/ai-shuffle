<script lang="ts">
	/**
	 * ContextUsageBar — the single chat-toolbar usage pill. Merges the former
	 * ContextUsageBar (context-window %) and UsageLimitIndicator (plan usage
	 * limits) into ONE glass pill + ONE popover.
	 *
	 * The pill shows a small circular progress ring (context-window fraction),
	 * coloured by the severity of MAX(context %, resolved five-hour %), plus the
	 * rounded context percent. It is ALWAYS visible.
	 *
	 * The popover stacks, top → bottom:
	 *   1. Context window — total row with a collapsible per-category breakdown.
	 *      The breakdown is fetched from the live SDK (`/context-usage`) when the
	 *      POPOVER opens and refreshed at turn end (never polled mid-stream); the
	 *      poller estimate is the fallback before the first fetch lands. Expansion
	 *      is persisted in localStorage (`aihub.contextBreakdownOpen`).
	 *   2. Plan usage limits — the resolved install's rate-limit windows (from the
	 *      `rateLimit` store, resolved via `usage-target.ts`), each with a bar, %,
	 *      and reset countdown. "No usage data yet" when the bucket is empty.
	 *   3. Footer — breakdown status (left) + effective model label (right).
	 *
	 * Usage comes from $activeSession (contextUsed + per-type token fields), all
	 * set by the SSE-fed reducer. Rate-limit windows come from the `rateLimit`
	 * store (fed by the backend poller + live SDK events), resolved to the install
	 * that OWNS the viewed/composed session — an offloaded session shows the
	 * worker's usage.
	 *
	 * Popover interaction: a wrapper with `bind:this`, a document-level click +
	 * Escape listener that closes when the target is outside — works for desktop
	 * click and mobile tap (tap-away closes).
	 */
	import { onMount } from 'svelte';
	import { activeSession } from '$lib/stores/active-session';
	import { profiles } from '$lib/stores/app-data.svelte';
	import { getContextMaxForModel } from '$lib/constants/chat';
	import { getDefaultModel, getModelLabel } from '$lib/constants/models';
	import { getContextUsage } from '$lib/api/sessions';
	import {
		computeContextBreakdown,
		mapSdkContextUsage,
		decideBreakdownRefresh,
		formatTokenCount,
		usageSeverity,
		type ContextBreakdown,
		type BreakdownRefreshState,
		type UsageSeverity,
	} from './context-usage';
	import {
		rateLimit,
		orderedWindows,
		windowLabel,
		effectivePct,
		formatCountdown,
		formatAsOf,
		resolveBucket,
		resolveBucketKey,
		selfBucketKey,
		isBucketStale,
		type UsageBucket,
	} from '$lib/stores/rate-limit';
	import { usageTargetInstance } from '$lib/stores/usage-target';

	// ── Effective model → context window (200k / 1M) ──────────────────────
	const effectiveModel = $derived.by(() => {
		if ($activeSession.modelOverride) return $activeSession.modelOverride;
		const profile = $profiles.find((p) => p.id === $activeSession.profile);
		return profile?.config?.model || getDefaultModel();
	});
	const effectiveModelLabel = $derived(getModelLabel(effectiveModel));
	const contextMax = $derived(getContextMaxForModel(effectiveModel));

	// Context usage from activeSession — set by onPollApplied after each poll.
	const rawContextUsed = $derived($activeSession.contextUsed ?? 0);

	const breakdown = $derived(
		computeContextBreakdown({
			rawContextUsed,
			inputTokens: $activeSession.totalTokensIn ?? 0,
			outputTokens: $activeSession.totalTokensOut ?? 0,
			cacheCreationTokens: $activeSession.totalCacheCreationTokens ?? 0,
			cacheReadTokens: $activeSession.totalCacheReadTokens ?? 0,
			contextMax,
		})
	);

	// The pill's ring is driven by the poller estimate — never the live fetch.
	const contextPercent = $derived(breakdown.totalPercent);

	// ── Ring geometry (mirrors TodoList's progress ring) ──────────────────
	const RING_RADIUS = 6;
	const RING_CIRCUMFERENCE = 2 * Math.PI * RING_RADIUS;
	const ringDashoffset = $derived(
		RING_CIRCUMFERENCE - (Math.min(Math.max(contextPercent, 0), 100) / 100) * RING_CIRCUMFERENCE
	);

	// ── Popover open/close (click + tap-away) ─────────────────────────────
	let open = $state(false);
	let wrapperRef = $state<HTMLDivElement | null>(null);

	function toggle() {
		open = !open;
	}

	// ── Live SDK breakdown = the single display source for the context section ─
	let richBreakdown = $state<ContextBreakdown | null>(null);
	const sessionId = $derived($activeSession.sessionId);
	const isStreaming = $derived($activeSession.isStreaming);
	const displayBreakdown = $derived<ContextBreakdown>(richBreakdown ?? breakdown);
	const isRich = $derived(richBreakdown !== null);

	let currentController: AbortController | null = null;
	function cancelFetch() {
		currentController?.abort();
		currentController = null;
	}
	async function fetchBreakdown(sid: string) {
		cancelFetch();
		const controller = new AbortController();
		currentController = controller;
		try {
			const res = await getContextUsage(sid, { signal: controller.signal });
			if (controller.signal.aborted) return;
			if (res.available) richBreakdown = mapSdkContextUsage(res.usage);
		} catch {
			// Aborted or network error → keep whatever is on screen.
		}
	}

	// Refresh cadence: fetch on open, refresh on turn-end, clear on close. The
	// breakdown fetch is tied to the POPOVER opening — NOT to the chevron below.
	let prevRefresh: BreakdownRefreshState = { open: false, sessionId: null, isStreaming: false };
	$effect(() => {
		const next: BreakdownRefreshState = { open, sessionId, isStreaming };
		const action = decideBreakdownRefresh(prevRefresh, next);
		prevRefresh = next;

		switch (action) {
			case 'clear':
				cancelFetch();
				richBreakdown = null;
				break;
			case 'open-fetch':
				richBreakdown = null;
				fetchBreakdown(next.sessionId!);
				break;
			case 'refresh':
				fetchBreakdown(next.sessionId!);
				break;
			case 'idle':
				break;
		}
	});

	// ── Category breakdown expand state (persisted) ───────────────────────
	const BREAKDOWN_OPEN_KEY = 'aihub.contextBreakdownOpen';
	function loadBreakdownOpen(): boolean {
		try {
			return localStorage.getItem(BREAKDOWN_OPEN_KEY) === '1';
		} catch {
			return false;
		}
	}
	let breakdownOpen = $state(loadBreakdownOpen());
	function toggleBreakdown() {
		breakdownOpen = !breakdownOpen;
		try {
			localStorage.setItem(BREAKDOWN_OPEN_KEY, breakdownOpen ? '1' : '0');
		} catch {
			// best-effort persistence
		}
	}

	// ── Plan usage limits (resolved install's rate-limit windows) ─────────
	// Per-second tick — keeps countdowns fresh and flips windows to 0% on reset.
	// Runs only while the popover is open; refreshed once otherwise.
	let now: number = $state(Date.now());
	$effect(() => {
		now = Date.now();
		if (!open) return;
		const interval = setInterval(() => {
			now = Date.now();
		}, 1000);
		return () => clearInterval(interval);
	});

	interface WindowItem {
		type: string;
		label: string;
		pct: number;
		countdown: string;
		hasReset: boolean;
	}
	function itemsFor(bucket: UsageBucket): WindowItem[] {
		return orderedWindows(bucket.windows).map((w) => ({
			type: w.type,
			label: windowLabel(w.type),
			pct: effectivePct(w, now),
			countdown: formatCountdown(w.resets_at, now),
			hasReset: w.resets_at != null,
		}));
	}

	const target = $derived($usageTargetInstance);
	const resolvedBucket = $derived(resolveBucket($rateLimit, target));
	const resolvedKey = $derived(resolveBucketKey($rateLimit, target));
	const items = $derived.by<WindowItem[]>(() => (resolvedBucket ? itemsFor(resolvedBucket) : []));

	// Five-hour % of the resolved install (null when unknown) — feeds the ring
	// severity alongside the context percent.
	const fiveHourPct = $derived<number | null>(items.find((it) => it.type === 'five_hour')?.pct ?? null);
	const ringSeverity = $derived<UsageSeverity>(
		usageSeverity(Math.max(contextPercent, fiveHourPct ?? 0))
	);

	// ── Resolved-install header (device/account label) ────────────────────
	function staleLineFor(bucket: UsageBucket): string | null {
		const offline = bucket.online === false;
		const old = isBucketStale(bucket, now);
		if (!offline && !old) return null;
		const age = formatAsOf(bucket.fetchedAt, now);
		const asOf = age ? `as of ${age}` : 'as of unknown';
		return offline ? `offline · ${asOf}` : asOf;
	}
	const isSelf = $derived(resolvedKey != null && resolvedKey === selfBucketKey($rateLimit));
	const deviceLabel = $derived(resolvedBucket?.deviceName ?? null);
	const accountLabel = $derived(resolvedBucket?.accountName ?? resolvedBucket?.accountEmail ?? null);
	const staleLine = $derived(resolvedBucket ? staleLineFor(resolvedBucket) : null);
	// Show the header when we know who this belongs to, or when it's a resolved
	// remote install. A bucket-less (no data) resolve shows no header.
	const showSectionHeader = $derived(
		!!resolvedBucket && (!!(deviceLabel || accountLabel) || !isSelf)
	);

	// ── Document-level click/tap-away + Escape ────────────────────────────
	function handleClickOutside(e: MouseEvent) {
		if (open && wrapperRef && !wrapperRef.contains(e.target as Node)) {
			open = false;
		}
	}
	function handleEscape(e: KeyboardEvent) {
		if (e.key === 'Escape' && open) open = false;
	}

	onMount(() => {
		document.addEventListener('click', handleClickOutside);
		document.addEventListener('keydown', handleEscape);
		return () => {
			document.removeEventListener('click', handleClickOutside);
			document.removeEventListener('keydown', handleEscape);
			cancelFetch();
		};
	});
</script>

<div class="usage-context-wrapper" bind:this={wrapperRef}>
	<button
		type="button"
		class="usage-indicator"
		title="{formatTokenCount(breakdown.totalUsed)} / {formatTokenCount(contextMax)} tokens — click for details"
		aria-label="Context and usage"
		aria-haspopup="dialog"
		aria-expanded={open}
		onclick={toggle}
	>
		<svg class="usage-ring {ringSeverity}" viewBox="0 0 16 16" aria-hidden="true">
			<circle class="ring-track" cx="8" cy="8" r={RING_RADIUS} fill="none" stroke-width="2" />
			<circle
				class="ring-progress"
				cx="8"
				cy="8"
				r={RING_RADIUS}
				fill="none"
				stroke-width="2"
				stroke-linecap="round"
				stroke-dasharray={RING_CIRCUMFERENCE}
				stroke-dashoffset={ringDashoffset}
			/>
		</svg>
		<span class="usage-text">{Math.round(contextPercent)}%</span>
	</button>

	{#if open}
		<div class="usage-popover" role="dialog" aria-label="Context and usage">
			<!-- ═══ Context window ═══ -->
			<div class="ctx-section">
				<div class="ctx-header">
					<span class="row-label">Context window</span>
					<div class="ctx-summary">
						<span class="ctx-tokens">
							{formatTokenCount(displayBreakdown.totalUsed)} / {formatTokenCount(
								displayBreakdown.contextMax
							)}
							(<span class="ctx-pct {displayBreakdown.severity}"
								>{Math.round(displayBreakdown.totalPercent)}%</span
							>)
						</span>
						<button
							type="button"
							class="ctx-chevron"
							class:expanded={breakdownOpen}
							aria-expanded={breakdownOpen}
							aria-label={breakdownOpen ? 'Hide breakdown' : 'Show breakdown'}
							onclick={toggleBreakdown}
						>
							<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" aria-hidden="true">
								<path
									stroke-linecap="round"
									stroke-linejoin="round"
									stroke-width="2"
									d="M19 9l-7 7-7-7"
								/>
							</svg>
						</button>
					</div>
				</div>
				<div class="row-bar">
					<div
						class="row-fill {displayBreakdown.severity}"
						style="width: {displayBreakdown.totalPercent}%"
					></div>
				</div>

				{#if breakdownOpen}
					{#each displayBreakdown.rows as row (row.key)}
						<div class="popover-row breakdown-row">
							<div class="row-head">
								<span class="row-label">{row.label}</span>
								<span class="row-pct">{formatTokenCount(row.tokens)}</span>
							</div>
							<div class="row-bar">
								<div class="row-fill" style="width: {row.barPercent}%"></div>
							</div>
							{#if row.subline}
								<div class="row-reset">{row.subline}</div>
							{/if}
						</div>
					{/each}
				{/if}
			</div>

			<div class="popover-divider"></div>

			<!-- ═══ Plan usage limits ═══ -->
			<div class="popover-title">Plan usage limits</div>
			{#if showSectionHeader}
				<div class="popover-section">
					<div class="section-head">
						{#if deviceLabel}
							<span class="section-device">{deviceLabel}</span>
						{/if}
						{#if accountLabel}
							<span class="section-account">{accountLabel}</span>
						{/if}
						{#if !deviceLabel && !accountLabel}
							<span class="section-device">{isSelf ? 'This device' : 'Other device'}</span>
						{/if}
					</div>
					{#if staleLine}
						<div class="section-stale">{staleLine}</div>
					{/if}
				</div>
			{/if}
			{#if items.length > 0}
				{#each items as item (item.type)}
					{@const rowSeverity = usageSeverity(item.pct)}
					<div class="popover-row">
						<div class="row-head">
							<span class="row-label">{item.label}</span>
							<span class="row-pct {rowSeverity}">{item.pct}%</span>
						</div>
						<div class="row-bar">
							<div class="row-fill {rowSeverity}" style="width: {item.pct}%"></div>
						</div>
						<div class="row-reset">
							{#if item.countdown}
								resets in {item.countdown}
							{:else if item.hasReset}
								resets soon
							{:else}
								no reset window
							{/if}
						</div>
					</div>
				{/each}
			{:else}
				<div class="row-reset no-data">No usage data yet</div>
			{/if}

			<!-- ═══ Footer ═══ -->
			<div class="popover-footer">
				<span class="footer-status">
					{#if isRich}
						{isStreaming ? 'breakdown · refreshes at turn end' : 'live breakdown'}
					{:else}
						estimated from last turn
					{/if}
				</span>
				<span class="footer-model">{effectiveModelLabel}</span>
			</div>
		</div>
	{/if}
</div>

<style>
	.usage-context-wrapper {
		position: relative;
		display: inline-flex;
	}

	/* ===== USAGE INDICATOR - Glass styling to match desktop =====
	   Rendered as a button here (clickable), so reset the button chrome. */
	.usage-indicator {
		display: flex;
		align-items: center;
		gap: 6px;
		padding: 4px 8px;
		background: var(--glass-bg);
		backdrop-filter: blur(var(--glass-blur, 12px));
		-webkit-backdrop-filter: blur(var(--glass-blur, 12px));
		border: 1px solid var(--glass-border);
		border-radius: calc(var(--radius, 0.75rem) * 0.5);
		font: inherit;
		cursor: pointer;
		transition: border-color 0.2s ease, background 0.2s ease;
	}
	.usage-indicator:hover {
		background: color-mix(in oklch, var(--glass-bg) 80%, var(--foreground) 6%);
	}

	/* ===== Circular progress ring (context-window fraction) ===== */
	.usage-ring {
		width: 15px;
		height: 15px;
		flex-shrink: 0;
		transform: rotate(-90deg);
		transform-origin: center;
	}
	.ring-track {
		stroke: color-mix(in oklch, var(--muted) 50%, var(--background));
	}
	.ring-progress {
		stroke: var(--primary);
		transition: stroke-dashoffset 0.3s ease, stroke 0.2s ease;
	}
	.usage-ring.warning .ring-progress {
		stroke: var(--warning);
	}
	.usage-ring.danger .ring-progress {
		stroke: var(--destructive);
	}

	.usage-text {
		font-family: var(--font-mono);
		font-size: 11px;
		font-weight: 500;
		color: var(--muted-foreground);
		letter-spacing: 0.02em;
	}

	/* ===== Popover — opens upward above the pill (dock sits at the bottom) ===== */
	.usage-popover {
		position: absolute;
		bottom: calc(100% + 6px);
		left: 0;
		z-index: 50;
		min-width: 240px;
		max-width: 300px;
		padding: 10px;
		display: flex;
		flex-direction: column;
		gap: 10px;
		background: var(--popover, var(--card, var(--background)));
		border: 1px solid var(--glass-border, var(--border));
		border-radius: calc(var(--radius, 0.75rem) * 0.66);
		box-shadow: 0 8px 24px rgb(0 0 0 / 0.28);
	}

	.popover-title {
		font-size: 11px;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--muted-foreground);
	}

	/* ===== Context section ===== */
	.ctx-section {
		display: flex;
		flex-direction: column;
		gap: 6px;
	}
	.ctx-header {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		gap: 8px;
	}
	.ctx-summary {
		display: flex;
		align-items: center;
		gap: 4px;
	}
	.ctx-tokens {
		font-size: 10.5px;
		color: var(--muted-foreground);
		white-space: nowrap;
	}
	.ctx-pct {
		font-family: var(--font-mono);
		font-weight: 600;
	}
	.ctx-pct.warning {
		color: var(--warning);
	}
	.ctx-pct.danger {
		color: var(--destructive);
	}
	.ctx-chevron {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 18px;
		height: 18px;
		padding: 0;
		background: transparent;
		border: none;
		border-radius: 4px;
		cursor: pointer;
		color: var(--muted-foreground);
		transition: background 0.15s ease, color 0.15s ease;
	}
	.ctx-chevron:hover {
		background: var(--hover-overlay);
		color: var(--foreground);
	}
	.ctx-chevron svg {
		width: 14px;
		height: 14px;
		transition: transform 0.2s ease;
	}
	.ctx-chevron.expanded svg {
		transform: rotate(180deg);
	}

	.breakdown-row {
		margin-top: 2px;
	}

	.popover-divider {
		height: 1px;
		background: var(--glass-border, var(--border));
		margin: 0 -10px;
	}

	.popover-row {
		display: flex;
		flex-direction: column;
		gap: 4px;
	}

	.row-head {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		gap: 8px;
	}

	.row-label {
		font-size: 12px;
		color: var(--foreground);
	}

	.row-pct {
		font-family: var(--font-mono);
		font-size: 11px;
		font-weight: 600;
		color: var(--muted-foreground);
	}
	.row-pct.warning {
		color: var(--warning);
	}
	.row-pct.danger {
		color: var(--destructive);
	}

	.row-bar {
		width: 100%;
		height: 5px;
		background: color-mix(in oklch, var(--muted) 50%, var(--background));
		border-radius: 3px;
		overflow: hidden;
	}

	.row-fill {
		height: 100%;
		background: var(--primary);
		border-radius: 3px;
		transition: width 0.3s ease;
	}
	.row-fill.warning {
		background: var(--warning);
	}
	.row-fill.danger {
		background: var(--destructive);
	}

	.row-reset {
		font-size: 10.5px;
		color: var(--muted-foreground);
	}
	.row-reset.no-data {
		font-style: italic;
		opacity: 0.8;
	}

	/* Resolved-install header — device/account label (shown when named or remote). */
	.popover-section {
		display: flex;
		flex-direction: column;
		gap: 2px;
	}
	.section-head {
		display: flex;
		align-items: baseline;
		gap: 6px;
		flex-wrap: wrap;
	}
	.section-device {
		font-size: 11px;
		font-weight: 600;
		color: var(--foreground);
	}
	.section-account {
		font-size: 10.5px;
		color: var(--muted-foreground);
	}
	.section-stale {
		font-size: 10px;
		color: var(--warning);
	}

	/* Footer — breakdown status (left) + effective model label (right). */
	.popover-footer {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		gap: 8px;
		font-size: 10px;
		letter-spacing: 0.02em;
		color: var(--muted-foreground);
		opacity: 0.75;
	}
	.footer-status {
		font-style: italic;
	}
	.footer-model {
		font-family: var(--font-mono);
		white-space: nowrap;
	}
</style>
