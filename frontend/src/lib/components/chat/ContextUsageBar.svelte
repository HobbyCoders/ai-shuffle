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
	 * The popover is a wide, compact panel. It stacks, top → bottom:
	 *   1. Context window — a collapsible header (persisted in localStorage under
	 *      `aihub.contextBreakdownOpen`, default expanded) over ONE segmented bar.
	 *      Expanded: a contiguous segment per category + a dim autocompact-buffer
	 *      segment (the bare track tail is the free space), plus per-category rows
	 *      (swatch + label + tokens + percent) followed by a trailing, muted Model
	 *      row (no swatch, no percent — just the effective model label). Collapsed:
	 *      a single severity-coloured fill sized to the total percent — no
	 *      per-category colour breakdown. The breakdown is fetched from the SDK
	 *      (`/context-usage`) on session load and refreshed at turn end (never
	 *      polled mid-stream) so the pill ring can prefer it; `source: "persisted"`
	 *      (final snapshot from the session's last turn end) is treated as a valid
	 *      rich breakdown. The poller estimate is the fallback before the first
	 *      fetch lands — a fresh session shows em-dash rows.
	 *   2. Plan usage limits — the resolved install's rate-limit windows (from the
	 *      `rateLimit` store, resolved via `usage-target.ts`), each with a bar, %,
	 *      and reset text inline. "No usage data yet" when the bucket is empty.
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
	// Provider id off the active session's profile config — lets a provider's
	// declared context_window win over the /1m/ heuristic (null = native Claude).
	const effectiveProviderId = $derived.by(() => {
		const profile = $profiles.find((p) => p.id === $activeSession.profile);
		return profile?.config?.provider ?? null;
	});
	const contextMax = $derived(getContextMaxForModel(effectiveModel, effectiveProviderId));

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

	// ── Popover open/close (click + tap-away) ─────────────────────────────
	let open = $state(false);
	let wrapperRef = $state<HTMLDivElement | null>(null);

	function toggle() {
		open = !open;
	}

	// ── SDK/persisted breakdown = the single display source for the context section ─
	let richBreakdown = $state<ContextBreakdown | null>(null);
	const sessionId = $derived($activeSession.sessionId);
	const isStreaming = $derived($activeSession.isStreaming);
	const displayBreakdown = $derived<ContextBreakdown>(richBreakdown ?? breakdown);

	// The pill ring prefers the rich breakdown when loaded (its totalPercent folds
	// the buffer in the same way the popover header does — so they can't disagree),
	// falling back to the poller estimate before the first fetch lands.
	const contextPercent = $derived(displayBreakdown.totalPercent);

	// ── Ring geometry (mirrors TodoList's progress ring) ──────────────────
	const RING_RADIUS = 6;
	const RING_CIRCUMFERENCE = 2 * Math.PI * RING_RADIUS;
	const ringDashoffset = $derived(
		RING_CIRCUMFERENCE - (Math.min(Math.max(contextPercent, 0), 100) / 100) * RING_CIRCUMFERENCE
	);

	// Segmented progress bar: one contiguous segment per row whose tokens are
	// actually part of totalUsed (row.inBar — excludes the grayed Free space row,
	// em-dash placeholders, and, on the estimate path, Output tokens, which is
	// session-cumulative and NOT part of context used), then one dim segment for
	// the folded autocompact buffer. Remaining track = free space (no segment).
	interface BarSegment {
		key: string;
		widthPct: number;
		color: string;
		dim: boolean;
	}
	const barSegments = $derived.by<BarSegment[]>(() => {
		const max = displayBreakdown.contextMax;
		const segs: BarSegment[] = displayBreakdown.rows
			.filter((r) => r.inBar && r.tokens > 0)
			.map((r) => ({ key: r.key, widthPct: r.barPercent, color: r.color, dim: false }));
		const bufferPct = max > 0 ? Math.min((displayBreakdown.autocompactTokens / max) * 100, 100) : 0;
		if (bufferPct > 0) {
			segs.push({
				key: '__autocompact',
				widthPct: bufferPct,
				color: 'color-mix(in oklch, var(--foreground) 28%, var(--background))',
				dim: true,
			});
		}
		return segs;
	});

	let loadedSessionId: string | null = null;
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
				loadedSessionId = sid;
			}
		} catch {
			// Aborted or network error → keep whatever is on screen.
		} finally {
			if (currentController === controller) currentController = null;
		}
	}

	// Refresh cadence: fetch on session load (even while closed, so the ring is
	// accurate), on popover open (if not already loaded), and at turn end; reset
	// synchronously on session change so no stale cross-session breakdown flashes.
	let prevRefresh: BreakdownRefreshState = { open: false, sessionId: null, isStreaming: false };
	$effect(() => {
		const next: BreakdownRefreshState = { open, sessionId, isStreaming };
		const action = decideBreakdownRefresh(prevRefresh, next);
		prevRefresh = next;

		switch (action) {
			case 'clear':
				cancelFetch();
				richBreakdown = null;
				loadedSessionId = null;
				break;
			case 'session-fetch':
				cancelFetch();
				richBreakdown = null;
				loadedSessionId = null;
				fetchBreakdown(next.sessionId!);
				break;
			case 'open-fetch':
				// Only fetch if nothing is loaded/in-flight for this session already.
				if (loadedSessionId !== next.sessionId && !currentController) {
					fetchBreakdown(next.sessionId!);
				}
				break;
			case 'refresh':
				fetchBreakdown(next.sessionId!);
				break;
			case 'idle':
				break;
		}
	});

	// ── Category breakdown collapse state (persisted, default expanded) ────
	const BREAKDOWN_OPEN_KEY = 'aihub.contextBreakdownOpen';
	function loadBreakdownOpen(): boolean {
		try {
			// Absent → expanded; only an explicit '0' collapses.
			return localStorage.getItem(BREAKDOWN_OPEN_KEY) !== '0';
		} catch {
			return true;
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

	// Provider credits (non-Claude): render a single muted line from the opaque
	// credits blob when there's no window denominator to draw a bar, or when the
	// provider reports meaningful credit balances. All reads are shape-guarded.
	function creditsSummary(credits: Record<string, unknown> | null): string | null {
		if (!credits || typeof credits !== 'object') return null;
		if (credits.unlimited === true) return 'Credits: unlimited';
		const used = typeof credits.usage === 'number' ? credits.usage : null;
		const left = typeof credits.limit_remaining === 'number' ? credits.limit_remaining : null;
		if (used != null && left != null) return `$${used.toFixed(2)} used · $${left.toFixed(2)} left`;
		if (used != null) return `$${used.toFixed(2)} used`;
		if (left != null) return `$${left.toFixed(2)} left`;
		// Codex credits shape carries a plain balance instead of usage/limit.
		const balance = typeof credits.balance === 'number' ? credits.balance : null;
		if (balance != null) return `Credits: $${balance.toFixed(2)}`;
		return null;
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
		title="{formatTokenCount(displayBreakdown.totalUsed)} / {formatTokenCount(
			displayBreakdown.contextMax
		)} tokens — click for details"
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
				<button
					type="button"
					class="ctx-header"
					aria-expanded={breakdownOpen}
					aria-label="Context window breakdown — {breakdownOpen ? 'collapse' : 'expand'}"
					onclick={toggleBreakdown}
				>
					<span class="row-label">Context window</span>
					<span class="ctx-value">
						{formatTokenCount(displayBreakdown.totalUsed)} / {formatTokenCount(
							displayBreakdown.contextMax
						)}
						<span class="ctx-pct {displayBreakdown.severity}"
							>({Math.round(displayBreakdown.totalPercent)}%)</span
						>
					</span>
					<svg
						class="ctx-chevron {breakdownOpen ? '' : 'collapsed'}"
						viewBox="0 0 24 24"
						fill="none"
						stroke="currentColor"
						aria-hidden="true"
					>
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
					</svg>
				</button>

				<!-- Segmented bar (always shown). Expanded: one segment per category + a
				     dim buffer segment; the bare track tail is the free space. Collapsed:
				     a single severity-coloured fill, same total width as the header %. -->
				<div class="ctx-segbar" aria-hidden="true">
					{#if breakdownOpen}
						{#each barSegments as seg (seg.key)}
							<span
								class="ctx-seg {seg.dim ? 'dim' : ''}"
								style="width: {seg.widthPct}%; background: {seg.color}"
							></span>
						{/each}
					{:else}
						<span
							class="ctx-seg solid {displayBreakdown.severity}"
							style="width: {displayBreakdown.totalPercent}%"
						></span>
					{/if}
				</div>

				{#if breakdownOpen}
					{#each displayBreakdown.rows as row (row.key)}
						<div class="breakdown-row {row.muted ? 'muted' : ''}">
							<span
								class="cat-swatch {row.empty ? 'neutral' : ''}"
								style="background: {row.color}"
							></span>
							<span class="breakdown-label">
								{row.label}{#if row.subline}<span class="row-sub"> · {row.subline}</span>{/if}
							</span>
							<span class="breakdown-value">
								{#if row.empty}
									—
								{:else}
									{formatTokenCount(row.tokens)}
									{#if row.showPercent}
										<span class="cat-pct">{row.barPercent.toFixed(1)}%</span>
									{:else}
										<span class="cat-pct">—</span>
									{/if}
								{/if}
							</span>
						</div>
					{/each}
					<div class="breakdown-row muted model-row">
						<span class="breakdown-label">Model</span>
						<span class="breakdown-value">{effectiveModelLabel}</span>
					</div>
				{/if}
			</div>

			<div class="popover-divider"></div>

			<!-- ═══ Plan usage limits ═══ -->
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

			<!-- ═══ Provider usage (non-Claude) ═══ -->
			{#each Object.values($rateLimit.providerBuckets ?? {}) as pb (pb.providerId)}
				<div class="popover-section">
					<div class="section-head">
						<span class="section-device">{pb.providerName ?? pb.providerType ?? 'Provider'}</span>
						{#if pb.planType}
							<span class="section-account">{pb.planType}</span>
						{/if}
					</div>
				</div>
				{#each orderedWindows(pb.windows) as w (w.type)}
					{@const pct = effectivePct(w, now)}
					{@const sev = usageSeverity(pct)}
					{@const countdown = formatCountdown(w.resets_at, now)}
					<div class="popover-row">
						<div class="row-line">
							<span class="row-label">{windowLabel(w.type)}</span>
							<span class="row-value">
								<span class="row-reset-inline">
									{#if countdown}
										resets in {countdown}
									{:else if w.resets_at != null}
										resets soon
									{:else}
										no reset window
									{/if}
								</span>
								<span class="row-pct {sev}">{pct}%</span>
							</span>
						</div>
						<div class="row-bar">
							<div class="row-fill {sev}" style="width: {pct}%"></div>
						</div>
					</div>
				{/each}
				{@const credits = creditsSummary(pb.credits)}
				{#if credits}
					<div class="row-reset">{credits}</div>
				{/if}
			{/each}
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

	/* ===== Context section ===== */
	.ctx-section {
		display: flex;
		flex-direction: column;
		gap: 7px;
	}

	/* Collapsible header — full-width button: label left, value + chevron right. */
	.ctx-header {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		gap: 10px;
		width: 100%;
		padding: 0;
		background: none;
		border: none;
		font: inherit;
		text-align: left;
		color: inherit;
		cursor: pointer;
	}
	.ctx-header:focus-visible {
		outline: 2px solid var(--ring, var(--primary));
		outline-offset: 2px;
		border-radius: 3px;
	}
	.ctx-chevron {
		width: 13px;
		height: 13px;
		flex-shrink: 0;
		align-self: center;
		color: var(--muted-foreground);
		transition: transform 200ms ease;
	}
	.ctx-chevron.collapsed {
		transform: rotate(-90deg);
	}

	/* One-line row: label left, value right — shared by the plan-usage data rows. */
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

	/* Single segmented progress bar — contiguous category segments + dim buffer,
	   the remaining track is the free space. Same track colour as the pill ring. */
	.ctx-segbar {
		display: flex;
		width: 100%;
		height: 6px;
		border-radius: 999px;
		overflow: hidden;
		background: color-mix(in oklch, var(--foreground) 12%, var(--background));
	}
	.ctx-seg {
		height: 100%;
		flex-shrink: 0;
		min-width: 0;
		transition: width 0.3s ease;
	}
	/* Collapsed state: one plain severity-coloured fill (same vars as the ring/pct). */
	.ctx-seg.solid {
		background: var(--primary);
	}
	.ctx-seg.solid.warning {
		background: var(--warning);
	}
	.ctx-seg.solid.danger {
		background: var(--destructive);
	}

	/* Per-category breakdown rows — one line: swatch + label (left) + value (right). */
	.breakdown-row {
		display: flex;
		align-items: center;
		gap: 8px;
	}
	.cat-swatch {
		width: 8px;
		height: 8px;
		flex-shrink: 0;
		border-radius: 2px;
	}
	.cat-swatch.neutral {
		border: 1px solid color-mix(in oklch, var(--foreground) 20%, var(--background));
	}
	.breakdown-label {
		flex: 1 1 auto;
		min-width: 0;
		font-size: 11px;
		color: var(--muted-foreground);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.row-sub {
		font-size: 10px;
		opacity: 0.65;
	}
	.breakdown-value {
		flex-shrink: 0;
		font-family: var(--font-mono);
		font-size: 10.5px;
		color: var(--muted-foreground);
		font-variant-numeric: tabular-nums;
		white-space: nowrap;
	}
	.cat-pct {
		color: var(--foreground);
		font-weight: 600;
	}
	/* Grayed "Free space" row — de-emphasised label, value, and percent. */
	.breakdown-row.muted .breakdown-label,
	.breakdown-row.muted .breakdown-value,
	.breakdown-row.muted .cat-pct {
		color: var(--muted-foreground);
		font-weight: 400;
		opacity: 0.75;
	}
	/* Model row — trailing info line in the expanded breakdown, set off from the
	   category/Free-space rows above it with a hairline separator. */
	.model-row {
		margin-top: 2px;
		padding-top: 6px;
		border-top: 1px dashed var(--glass-border, var(--border));
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
</style>
