<script lang="ts">
	/**
	 * ContextUsageBar — the single chat-toolbar usage pill. Merges the former
	 * ContextUsageBar (context-window %) and UsageLimitIndicator (plan usage
	 * limits) into ONE glass pill + ONE popover.
	 *
	 * The pill is a single circular progress ring (context-window fraction),
	 * coloured by the current chat's context severity. No inline text — the
	 * percentage rides on the button's aria-label. It is ALWAYS visible.
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
	import {
		getDefaultModel,
		getModelLabel,
		decodeProviderModel,
		isPresetModel,
		getProviders,
		getProviderDisplay,
		getClaudeDisplay,
		getClaudeAuth,
		type ProviderDisplay
	} from '$lib/constants/models';
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
		displayWindows,
		windowLabel,
		effectivePct,
		usedPct,
		formatCountdown,
		resolveBucket,
		resolveBucketKey,
		selfBucketKey,
		type UsageBucket,
		type UsageWindow,
	} from '$lib/stores/rate-limit';
	import { usageTargetInstance } from '$lib/stores/usage-target';

	// ── Effective model → context window (200k / 1M) ──────────────────────
	// A composer override may be a `prov:<providerId>:<modelId>` composite
	// (PA13's cross-provider switch) — decode it once and let BOTH the model id
	// and the provider id come from the pick, mirroring the backend's resolution.
	const overrideComposite = $derived(decodeProviderModel($activeSession.modelOverride));
	const effectiveModel = $derived.by(() => {
		if (overrideComposite) return overrideComposite.modelId;
		if ($activeSession.modelOverride) return $activeSession.modelOverride;
		const profile = $profiles.find((p) => p.id === $activeSession.profile);
		return profile?.config?.model || getDefaultModel();
	});
	// The label reads the RAW selection so a composite round-trips to its
	// provider-scoped label rather than a bare id.
	const effectiveModelLabel = $derived(
		getModelLabel($activeSession.modelOverride ?? effectiveModel)
	);
	// Provider id for the context-window lookup (null = native Claude), resolved
	// with the same precedence the backend uses: an explicit composite override
	// wins; a plain native-preset override CLEARS the binding for the turn; else
	// the session's live provider, else the profile's configured provider.
	const effectiveProviderId = $derived.by(() => {
		if (overrideComposite) return overrideComposite.providerId;
		if ($activeSession.modelOverride && isPresetModel($activeSession.modelOverride)) return null;
		if ($activeSession.liveProvider) return $activeSession.liveProvider;
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

	// ── Per-account usage-section collapse state (persisted, default expanded) ──
	// One entry per section id: the literal `'plan'` for the resolved Claude
	// install, and `provider:<id>` for each provider bucket. Mirrors the
	// context-breakdown collapse pattern — absent/any non-'0' value → expanded,
	// only an explicit '0' collapses. The record is the reactive cache; a toggle
	// writes it (and best-effort persists), so reads prefer it over localStorage.
	const SECTION_OPEN_PREFIX = 'aihub.usageSectionOpen:';
	let sectionOpen = $state<Record<string, boolean>>({});
	function isSectionOpen(id: string): boolean {
		if (id in sectionOpen) return sectionOpen[id];
		try {
			return localStorage.getItem(SECTION_OPEN_PREFIX + id) !== '0';
		} catch {
			return true;
		}
	}
	function toggleSection(id: string) {
		const next = !isSectionOpen(id);
		sectionOpen[id] = next;
		try {
			localStorage.setItem(SECTION_OPEN_PREFIX + id, next ? '1' : '0');
		} catch {
			// best-effort persistence
		}
	}

	// ── Plan usage limits (resolved install's rate-limit windows) ─────────
	// Claude subscription bars show used-of-100 (usedPct), not remaining.
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
		return displayWindows(bucket.windows).map((w) => ({
			type: w.type,
			label: windowLabel(w.type),
			pct: usedPct(w, now),
			countdown: formatCountdown(w.resets_at, now),
			hasReset: w.resets_at != null,
		}));
	}

	// Provider credits (non-Claude): render a single muted line from the opaque
	// credits blob when there's no window denominator to draw a bar, or when the
	// provider reports meaningful credit balances. All reads are shape-guarded.
	/** A finite number from the untyped credits blob, else null. */
	function creditsNum(credits: Record<string, unknown> | null, key: string): number | null {
		const v = credits?.[key];
		return typeof v === 'number' && Number.isFinite(v) ? v : null;
	}

	function creditsSummary(credits: Record<string, unknown> | null): string | null {
		if (!credits || typeof credits !== 'object') return null;
		// An account credit BALANCE (OpenRouter's /credits ledger) is the headline
		// figure and outranks the key-scoped usage/limit fields below it.
		const remaining = creditsNum(credits, 'balance_remaining');
		if (remaining != null) {
			const spent = creditsNum(credits, 'total_usage');
			return spent != null
				? `$${remaining.toFixed(2)} left · $${spent.toFixed(2)} used`
				: `$${remaining.toFixed(2)} left`;
		}
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

	// Compact one-value credits chip for a collapsed provider header — shown
	// whenever the account balance is known, and otherwise as the fallback when
	// the provider has no rate-limit windows to draw a pct from. Same field
	// semantics as creditsSummary: the account balance first, then usage,
	// key-limit remaining, and a plain balance.
	function creditsShort(credits: Record<string, unknown> | null): string | null {
		if (!credits || typeof credits !== 'object') return null;
		const remaining = creditsNum(credits, 'balance_remaining');
		if (remaining != null) return `$${remaining.toFixed(2)} left`;
		const used = typeof credits.usage === 'number' ? credits.usage : null;
		if (used != null) return `$${used.toFixed(2)} used`;
		const left = typeof credits.limit_remaining === 'number' ? credits.limit_remaining : null;
		if (left != null) return `$${left.toFixed(2)} left`;
		const balance = typeof credits.balance === 'number' ? credits.balance : null;
		if (balance != null) return `$${balance.toFixed(2)}`;
		return null;
	}

	const target = $derived($usageTargetInstance);
	const resolvedBucket = $derived(resolveBucket($rateLimit, target));
	const resolvedKey = $derived(resolveBucketKey($rateLimit, target));
	const items = $derived.by<WindowItem[]>(() => (resolvedBucket ? itemsFor(resolvedBucket) : []));

	const ringSeverity = $derived<UsageSeverity>(usageSeverity(contextPercent));

	// ── Resolved-install header (brand + auth-kind subtext) ───────────────
	// Every section header in this popover reads BRAND NAME + a subtext saying
	// how that account authenticates, from the shared naming rules in
	// `constants/models` — so the popover, the composer's model dropdown and the
	// agents-UI picker can never drift apart.
	//
	// Claude: "Claude" + its subscription plan ("max"/"pro"), or "API" on
	// API-key auth. Two sources feed that, in this order:
	//
	//   1. the RESOLVED INSTALL's own usage snapshot (`planType`, joined from
	//      that install's `claude_accounts` row) — authoritative for any
	//      install, including a remote worker on a different account;
	//   2. the local registry's `claude` block — it describes THIS install only,
	//      so it is consulted just for the self install (it is also the only
	//      carrier of API-key auth, which snapshots never report).
	//
	// With neither known the subtext falls back to the resolved install's device
	// name / "Other device" hint rather than dropping that disambiguation.
	const isSelf = $derived(resolvedKey != null && resolvedKey === selfBucketKey($rateLimit));
	const deviceLabel = $derived(resolvedBucket?.deviceName ?? null);
	const claudeDisplay = $derived<ProviderDisplay>(
		getClaudeDisplay({
			planType: resolvedBucket?.planType ?? (isSelf ? getClaudeAuth().plan_type : null),
			authKind: isSelf ? getClaudeAuth().auth_kind : null
		})
	);
	const claudeSubtext = $derived(
		claudeDisplay.subtext ?? deviceLabel ?? (isSelf ? null : 'Other device')
	);

	// Provider sections: prefer the hydrated registry row (its `auth_kind` is
	// derived from the row's auth_mode, so a `custom` row on Codex auth is
	// classified correctly), falling back to the usage bucket's own fields when
	// the registry hasn't hydrated.
	function providerDisplayFor(pb: {
		providerId: string;
		providerName: string | null;
		providerType: string | null;
		planType: string | null;
	}): ProviderDisplay {
		const entry = getProviders().find((p) => p.id === pb.providerId);
		return getProviderDisplay({
			name: entry?.name ?? pb.providerName ?? pb.providerType ?? 'Provider',
			provider_type: entry?.provider_type ?? pb.providerType,
			auth_kind: entry?.auth_kind ?? null,
			plan_type: entry?.plan_type ?? pb.planType
		});
	}

	/**
	 * True when this provider's window feed reports REMAINING quota, so its
	 * display needs the remaining → used inversion. Only the ChatGPT/Codex
	 * subscription feed behaves that way. Everything else already reports
	 * used-of-100: Grok's billing payload (creditUsagePercent / usagePercent),
	 * API-key header captures, and OpenRouter's credit_limit.
	 */
	function reportsRemaining(pb: { providerId: string; providerType: string | null }): boolean {
		const entry = getProviders().find((p) => p.id === pb.providerId);
		const type = entry?.provider_type ?? pb.providerType ?? '';
		if (type !== 'chatgpt') return false;
		const kind = entry?.auth_kind ?? 'subscription';
		return kind === 'subscription';
	}

	function providerWindowPct(
		pb: { providerId: string; providerType: string | null },
		w: UsageWindow,
		nowMs: number
	): number {
		return reportsRemaining(pb) ? usedPct(w, nowMs) : effectivePct(w, nowMs);
	}

	// Claude plan-usage collapse: the rows follow the 'plan' section toggle.
	const planRowsVisible = $derived(isSectionOpen('plan'));
	// Compact summary shown on the collapsed header: the MAX pct across the plan
	// windows (null → omit, e.g. an empty bucket).
	const planMaxPct = $derived<number | null>(
		items.length ? Math.max(...items.map((it) => it.pct)) : null
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
			<circle class="ring-track" cx="8" cy="8" r={RING_RADIUS} fill="none" stroke-width="2.4" />
			<circle
				class="ring-progress"
				cx="8"
				cy="8"
				r={RING_RADIUS}
				fill="none"
				stroke-width="2.4"
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

			<!-- ═══ Plan usage limits (Claude) ═══ -->
			<!-- The header always renders (provider parity): the "Claude" brand +
			     its auth-kind subtext (plan / "API"), falling back to the resolved
			     device name or "Other device". It is the collapse toggle for the window rows;
			     collapsed shows the max plan pct on the right and hides the rows +
			     "No usage data" line. No account naming, no "as of" staleness line. -->
			<div class="popover-section">
				<button
					type="button"
					class="sec-header"
					aria-expanded={planRowsVisible}
					aria-label="Plan usage limits — {planRowsVisible ? 'collapse' : 'expand'}"
					onclick={() => toggleSection('plan')}
				>
					<span class="sec-head-labels">
						<span class="section-device">{claudeDisplay.name}</span>
						{#if claudeSubtext}
							<span class="section-account">{claudeSubtext}</span>
						{/if}
					</span>
					{#if !planRowsVisible && planMaxPct != null}
						<span class="row-pct {usageSeverity(planMaxPct)} sec-summary">{planMaxPct}%</span>
					{/if}
					<svg
						class="ctx-chevron {planRowsVisible ? '' : 'collapsed'}"
						viewBox="0 0 24 24"
						fill="none"
						stroke="currentColor"
						aria-hidden="true"
					>
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
					</svg>
				</button>
			</div>
			{#if planRowsVisible}
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
			{/if}

			<!-- ═══ Provider usage (non-Claude) ═══ -->
			<!-- Each bucket's head is its collapse toggle (name + planType stay);
			     collapsed shows the bucket's max window pct and hides both the window
			     rows and the credits line. ChatGPT/Codex subscription windows invert
			     remaining → used; Grok / API-key / credit windows already report
			     used-of-100 and pass through unchanged. -->
			{#each Object.values($rateLimit.providerBuckets ?? {}) as pb (pb.providerId)}
				{@const sectionId = `provider:${pb.providerId}`}
				{@const pbDisplay = providerDisplayFor(pb)}
				{@const pbOpen = isSectionOpen(sectionId)}
				{@const pbWindows = displayWindows(pb.windows)}
				{@const pbMaxPct = pbWindows.length
					? Math.max(...pbWindows.map((w) => providerWindowPct(pb, w, now)))
					: null}
				<div class="popover-section">
					<button
						type="button"
						class="sec-header"
						aria-expanded={pbOpen}
						aria-label="{pbDisplay.name} usage — {pbOpen ? 'collapse' : 'expand'}"
						onclick={() => toggleSection(sectionId)}
					>
						<span class="sec-head-labels">
							<span class="section-device">{pbDisplay.name}</span>
							{#if pbDisplay.subtext}
								<span class="section-account">{pbDisplay.subtext}</span>
							{/if}
						</span>
						{#if !pbOpen}
							{@const pbCreditsShort = creditsShort(pb.credits)}
							<!-- An account credit balance is the headline the user cares
							     about, so it outranks the window pct (which an OpenRouter
							     key-spend limit would otherwise produce). -->
							{#if creditsNum(pb.credits, 'balance_remaining') != null && pbCreditsShort}
								<span class="sec-summary sec-credits">{pbCreditsShort}</span>
							{:else if pbMaxPct != null}
								<span class="row-pct {usageSeverity(pbMaxPct)} sec-summary">{pbMaxPct}%</span>
							{:else if pbCreditsShort}
								<span class="sec-summary sec-credits">{pbCreditsShort}</span>
							{/if}
						{/if}
						<svg
							class="ctx-chevron {pbOpen ? '' : 'collapsed'}"
							viewBox="0 0 24 24"
							fill="none"
							stroke="currentColor"
							aria-hidden="true"
						>
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
						</svg>
					</button>
				</div>
				{#if pbOpen}
					{#each pbWindows as w (w.type)}
						{@const pct = providerWindowPct(pb, w, now)}
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

	/* ===== USAGE INDICATOR — the mockup's bare `.ctxring` =====
	   It sits inside the composer island's glass foot row, so it carries no
	   surface of its own: just the ring, on a hit target big enough to click.
	   Rendered as a button (clickable), so reset the button chrome. */
	.usage-indicator {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 26px;
		height: 26px;
		margin-left: 2px;
		padding: 0;
		background: transparent;
		border: none;
		border-radius: 50%;
		font: inherit;
		cursor: pointer;
		transition: background-color 0.13s ease;
	}
	.usage-indicator:hover {
		background: var(--scrim);
	}
	.usage-indicator:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 1px;
	}

	/* ===== Circular progress ring (context-window fraction) ===== */
	.usage-ring {
		width: 16px;
		height: 16px;
		flex-shrink: 0;
		transform: rotate(-90deg);
		transform-origin: center;
	}
	.ring-track {
		stroke: var(--border-strong);
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

	/* Collapsible account/provider section header — a full-width button mirroring
	   .ctx-header: label group left, optional max-pct summary + chevron right. */
	.sec-header {
		display: flex;
		align-items: center;
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
	.sec-header:focus-visible {
		outline: 2px solid var(--ring, var(--primary));
		outline-offset: 2px;
		border-radius: 3px;
	}
	/* Device/account (or provider name/planType) labels — wrap as the original
	   .section-head did; the stale line rides along and wraps to a new line. */
	.sec-head-labels {
		display: flex;
		align-items: baseline;
		gap: 6px;
		flex-wrap: wrap;
		flex: 1 1 auto;
		min-width: 0;
	}
	/* Collapsed-header summary — same look as a plan-usage row percent. */
	.sec-summary {
		flex-shrink: 0;
	}
	/* Credits fallback summary — dollars carry no severity, so stay neutral. */
	.sec-credits {
		font-family: var(--font-mono);
		font-size: 11px;
		font-weight: 600;
		color: var(--muted-foreground);
		font-variant-numeric: tabular-nums;
		white-space: nowrap;
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
</style>
