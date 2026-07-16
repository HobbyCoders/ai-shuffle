<script lang="ts">
	/**
	 * ContextUsageBar — the single chat-toolbar usage pill. Merges the former
	 * ContextUsageBar (context-window %) and UsageLimitIndicator (plan usage
	 * limits) into ONE glass pill + ONE popover.
	 *
	 * The pill is a single circular progress ring (context-window fraction),
	 * coloured by the severity of MAX(context %, resolved five-hour %). No inline
	 * text — the percentage rides on the button's aria-label. It is ALWAYS visible.
	 *
	 * The popover is a wide, compact panel; every data row is one line (label left,
	 * value right) with a thin full-width bar beneath. It stacks, top → bottom:
	 *   1. Context window — header total plus an always-shown per-category
	 *      breakdown. The breakdown is fetched from the SDK (`/context-usage`) when
	 *      the POPOVER opens and refreshed at turn end (never polled mid-stream);
	 *      `source: "persisted"` (final snapshot from the session's last turn end)
	 *      is treated as a valid rich breakdown. The poller estimate is the fallback
	 *      before the first fetch lands — a fresh session shows em-dash rows.
	 *   2. Plan usage limits — the resolved install's rate-limit windows (from the
	 *      `rateLimit` store, resolved via `usage-target.ts`), each with a bar, %,
	 *      and reset text inline. "No usage data yet" when the bucket is empty.
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
		breakdownStatusLabel,
		formatTokenCount,
		usageSeverity,
		type ContextBreakdown,
		type BreakdownRefreshState,
		type BreakdownSource,
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

	// ── SDK/persisted breakdown = the single display source for the context section ─
	let richBreakdown = $state<ContextBreakdown | null>(null);
	// Where the current rich breakdown came from — drives the footer status. Both
	// "live" and "persisted" render an identical breakdown; only the label differs.
	let richSource = $state<BreakdownSource>(null);
	const sessionId = $derived($activeSession.sessionId);
	const isStreaming = $derived($activeSession.isStreaming);
	const displayBreakdown = $derived<ContextBreakdown>(richBreakdown ?? breakdown);

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
			if (res.available) {
				richBreakdown = mapSdkContextUsage(res.usage);
				richSource = res.source;
			}
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
				richSource = null;
				break;
			case 'open-fetch':
				richBreakdown = null;
				richSource = null;
				fetchBreakdown(next.sessionId!);
				break;
			case 'refresh':
				fetchBreakdown(next.sessionId!);
				break;
			case 'idle':
				break;
		}
	});

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
		aria-label="Context and usage — {Math.round(contextPercent)}% of context window used"
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
	</button>

	{#if open}
		<div class="usage-popover" role="dialog" aria-label="Context and usage">
			<!-- ═══ Context window ═══ -->
			<div class="ctx-section">
				<div class="row-line">
					<span class="row-label">Context window</span>
					<span class="ctx-value">
						{formatTokenCount(displayBreakdown.totalUsed)} / {formatTokenCount(
							displayBreakdown.contextMax
						)}
						<span class="ctx-pct {displayBreakdown.severity}"
							>({Math.round(displayBreakdown.totalPercent)}%)</span
						>
					</span>
				</div>
				<div class="row-bar">
					<div
						class="row-fill {displayBreakdown.severity}"
						style="width: {displayBreakdown.totalPercent}%"
					></div>
				</div>

				{#each displayBreakdown.rows as row (row.key)}
					<div class="breakdown-row">
						<div class="row-line">
							<span class="breakdown-label">
								{row.label}{#if row.subline}<span class="row-sub"> · {row.subline}</span>{/if}
							</span>
							<span class="breakdown-value">{row.empty ? '—' : formatTokenCount(row.tokens)}</span>
						</div>
						<div class="row-bar mini">
							<div class="row-fill" style="width: {row.empty ? 0 : row.barPercent}%"></div>
						</div>
					</div>
				{/each}
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
						<div class="row-line">
							<span class="row-label">{item.label}</span>
							<span class="row-value">
								<span class="row-reset-inline">
									{#if item.countdown}
										resets in {item.countdown}
									{:else if item.hasReset}
										resets soon
									{:else}
										no reset window
									{/if}
								</span>
								<span class="row-pct {rowSeverity}">{item.pct}%</span>
							</span>
						</div>
						<div class="row-bar">
							<div class="row-fill {rowSeverity}" style="width: {item.pct}%"></div>
						</div>
					</div>
				{/each}
			{:else}
				<div class="row-reset no-data">No usage data yet</div>
			{/if}

			<!-- ═══ Footer ═══ -->
			<div class="popover-footer">
				<span class="footer-status">{breakdownStatusLabel(richSource, isStreaming)}</span>
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
		display: inline-flex;
		align-items: center;
		justify-content: center;
		min-width: 32px;
		min-height: 32px;
		padding: 5px;
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
		/* Foreground-mix reads slightly lighter than the toolbar in dark mode and
		   slightly darker in light mode, so the unfilled ring stays visible in both. */
		stroke: color-mix(in oklch, var(--foreground) 12%, var(--background));
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

	/* ===== Popover — wide + compact, opens upward above the pill (dock sits at
	   the bottom). Width is clamped to the viewport so it never overflows on
	   mobile. Per CSS, min-width is resolved after max-width and wins when the
	   two conflict — so the viewport clamp must live inside min-width too, or a
	   narrow viewport would still force the popover to the raw 340px floor. ===== */
	.usage-popover {
		position: absolute;
		bottom: calc(100% + 6px);
		left: 0;
		z-index: 50;
		width: max-content;
		min-width: min(340px, calc(100vw - 24px));
		max-width: min(380px, calc(100vw - 24px));
		padding: 12px 14px;
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
		gap: 5px;
	}

	/* One-line row: label left, value right — shared by every data row. */
	.row-line {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		gap: 12px;
	}

	.ctx-value {
		font-family: var(--font-mono);
		font-size: 11px;
		color: var(--muted-foreground);
		font-variant-numeric: tabular-nums;
		white-space: nowrap;
	}
	.ctx-pct {
		font-weight: 600;
		color: var(--foreground);
	}
	.ctx-pct.warning {
		color: var(--warning);
	}
	.ctx-pct.danger {
		color: var(--destructive);
	}

	/* Per-category breakdown rows — indented + smaller, always shown. */
	.breakdown-row {
		display: flex;
		flex-direction: column;
		gap: 3px;
		padding-left: 12px;
	}
	.breakdown-label {
		font-size: 11px;
		color: var(--muted-foreground);
		white-space: nowrap;
	}
	.row-sub {
		font-size: 10px;
		opacity: 0.65;
	}
	.breakdown-value {
		font-family: var(--font-mono);
		font-size: 10.5px;
		color: var(--muted-foreground);
		font-variant-numeric: tabular-nums;
		white-space: nowrap;
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

	.row-label {
		font-size: 12px;
		color: var(--foreground);
		white-space: nowrap;
	}

	/* Usage-row value: reset text (muted) + percentage (emphasized), inline right. */
	.row-value {
		display: inline-flex;
		align-items: baseline;
		gap: 8px;
		white-space: nowrap;
	}
	.row-reset-inline {
		font-size: 10.5px;
		color: var(--muted-foreground);
	}

	.row-pct {
		font-family: var(--font-mono);
		font-size: 11px;
		font-weight: 600;
		color: var(--foreground);
		font-variant-numeric: tabular-nums;
	}
	.row-pct.warning {
		color: var(--warning);
	}
	.row-pct.danger {
		color: var(--destructive);
	}

	/* Thin full-width track — same foreground-mix as the pill ring so unfilled
	   bars stay visible in both themes. */
	.row-bar {
		width: 100%;
		height: 3px;
		background: color-mix(in oklch, var(--foreground) 12%, var(--background));
		border-radius: 999px;
		overflow: hidden;
	}
	.row-bar.mini {
		height: 2px;
	}

	.row-fill {
		height: 100%;
		background: var(--primary);
		border-radius: 999px;
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
