<script lang="ts">
	/**
	 * ContextUsageBar - Shows context window usage as a glass pill with percentage,
	 * plus a click/tap popover that breaks the total down by token type. The pill
	 * itself stays neutral-coloured (no usage-based colour changes); severity
	 * colouring lives only on the total row inside the popover.
	 *
	 * Resolves the effective model (override → profile → default) to determine
	 * whether the context window is 200k (standard) or 1M (extended).
	 *
	 * IMPORTANT: Usage comes from $activeSession (contextUsed + the per-type token
	 * fields), all set by the onPollApplied callback (session-poller →
	 * active-session). Do NOT import from session-state.svelte.ts directly — that
	 * .svelte.ts file suffers from dual module evaluation (see KNOWN_ISSUES Root
	 * Cause 10), causing reads to hit a different instance than the poller writes.
	 *
	 * The popover interaction mirrors UsageLimitIndicator: a wrapper with
	 * `bind:this`, a document-level click + Escape listener that closes when the
	 * target is outside — works for desktop click and mobile tap (tap-away closes).
	 */
	import { onMount } from 'svelte';
	import { activeSession } from '$lib/stores/active-session';
	import { profiles } from '$lib/stores/app-data.svelte';
	import { getContextMaxForModel } from '$lib/constants/chat';
	import { getDefaultModel } from '$lib/constants/models';
	import { getContextUsage } from '$lib/api/sessions';
	import {
		computeContextBreakdown,
		mapSdkContextUsage,
		formatTokenCount,
		type ContextBreakdown,
	} from './context-usage';

	// Resolve effective model: override → profile config → default
	const effectiveModel = $derived.by(() => {
		if ($activeSession.modelOverride) return $activeSession.modelOverride;
		const profile = $profiles.find((p) => p.id === $activeSession.profile);
		return profile?.config?.model || getDefaultModel();
	});

	// Full context window (200k or 1M) — shown as the bar maximum so users
	// understand their position within the real model limit.
	const contextMax = $derived(getContextMaxForModel(effectiveModel));

	// Context usage from activeSession — set by onPollApplied after each poll.
	// contextUsed = per-turn context tokens (input + cache_creation + cache_read),
	// NOT cumulative totalTokensIn which would massively overcount.
	const rawContextUsed = $derived($activeSession.contextUsed ?? 0);

	// Total + per-token-type breakdown (pure module keeps the math testable).
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

	// The pill shows the total percent; it stays themed (no severity colouring).
	// The pill is ALWAYS driven by the poller estimate — never the live fetch.
	const contextPercent = $derived(breakdown.totalPercent);

	// ── Popover open/close (click + tap-away) ─────────────────────────────
	let open = $state(false);
	let wrapperRef = $state<HTMLDivElement | null>(null);

	function toggle() {
		open = !open;
	}

	// ── Live SDK breakdown (fetched on open; falls back to the estimate) ───
	// Populated only after a successful `available: true` fetch. Null while
	// pending, on failure, or on `available: false` — so the popover renders the
	// estimate meanwhile with no spinner or layout jump. Never cached across opens.
	let richBreakdown = $state<ContextBreakdown | null>(null);

	// What the POPOVER renders: the live breakdown once it arrives, else the
	// estimate. The pill above is untouched by this.
	const displayBreakdown = $derived<ContextBreakdown>(richBreakdown ?? breakdown);
	const isRich = $derived(richBreakdown !== null);

	// Fetch on every open for the current session; abort/ignore when the popover
	// closes, the component unmounts, or the session changes (effect re-run
	// aborts the previous controller). Reading `open` + sessionId here makes the
	// effect re-run on either change.
	$effect(() => {
		const sid = $activeSession.sessionId;
		if (!open || !sid) {
			richBreakdown = null;
			return;
		}
		// Reset so a stale prior-session breakdown never lingers under the fresh
		// fetch; the estimate shows until (and unless) this fetch succeeds.
		richBreakdown = null;
		const controller = new AbortController();
		(async () => {
			try {
				const res = await getContextUsage(sid, { signal: controller.signal });
				if (controller.signal.aborted) return;
				richBreakdown = res.available ? mapSdkContextUsage(res.usage) : null;
			} catch {
				// Aborted, network error, or a shape we can't parse → stay on the
				// estimate fallback. Nothing to surface to the user.
				if (!controller.signal.aborted) richBreakdown = null;
			}
		})();
		return () => controller.abort();
	});

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
		};
	});
</script>

<div class="usage-context-wrapper" bind:this={wrapperRef}>
	<button
		type="button"
		class="usage-indicator"
		title="{formatTokenCount(breakdown.totalUsed)} / {formatTokenCount(contextMax)} tokens — click for breakdown"
		aria-haspopup="dialog"
		aria-expanded={open}
		onclick={toggle}
	>
		<div class="usage-bar">
			<div class="usage-fill" style="width: {contextPercent}%"></div>
		</div>
		<span class="usage-text">{Math.round(contextPercent)}%</span>
	</button>

	{#if open}
		<div class="usage-popover" role="dialog" aria-label="Context usage">
			<div class="popover-title">Context usage</div>

			<div class="popover-row">
				<div class="row-head">
					<span class="row-label">Context window</span>
					<span class="row-pct {displayBreakdown.severity}"
						>{Math.round(displayBreakdown.totalPercent)}%</span
					>
				</div>
				<div class="row-bar">
					<div
						class="row-fill {displayBreakdown.severity}"
						style="width: {displayBreakdown.totalPercent}%"
					></div>
				</div>
				<div class="row-reset">
					{formatTokenCount(displayBreakdown.totalUsed)} of
					{formatTokenCount(displayBreakdown.contextMax)} tokens
				</div>
			</div>

			{#each displayBreakdown.rows as row (row.key)}
				<div class="popover-row">
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

			<div class="popover-footer">
				{isRich ? 'live breakdown' : 'estimated from last turn'}
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
		gap: 8px;
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

	.usage-bar {
		width: 40px;
		height: 4px;
		background: color-mix(in oklch, var(--muted) 50%, var(--background));
		border-radius: calc(var(--radius, 0.75rem) * 0.17);
		overflow: hidden;
	}

	.usage-fill {
		height: 100%;
		background: var(--primary);
		border-radius: calc(var(--radius, 0.75rem) * 0.17);
		transition: width 0.3s ease;
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

	/* Mode line — distinguishes the live SDK breakdown from the estimate. */
	.popover-footer {
		font-size: 10px;
		font-style: italic;
		letter-spacing: 0.02em;
		color: var(--muted-foreground);
		opacity: 0.75;
		text-align: right;
	}

	/* Responsive: mobile layout adjustments */
	@media (max-width: 639px) {
		/* Hide the usage bar on mobile, only show percentage (popover unaffected) */
		.usage-bar {
			display: none;
		}

		/* Make usage indicator more compact */
		.usage-indicator {
			padding: 4px 6px;
			gap: 0;
		}
	}
</style>
