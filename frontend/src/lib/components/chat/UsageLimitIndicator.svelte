<script lang="ts">
	/**
	 * UsageLimitIndicator — compact glass pill (next to <ContextUsageBar /> in the
	 * controls dock) showing ONLY the 5-hour ('five_hour') Claude usage window,
	 * plus a click/tap popover listing ALL current limit windows with a progress
	 * bar, %, and reset countdown.
	 *
	 * The pill headline is pinned to the 5-hour window and never falls back to
	 * another window: if `five_hour` isn't known yet the pill stays hidden. The
	 * weekly (`seven_day`) and Fable/Opus windows only appear inside the popover.
	 *
	 * Data source: the `rateLimit` store, fed by the backend usage poller
	 * (`usage_snapshot` WS event + the `/usage/limits` REST fetch on load) and by
	 * live SDK `rate_limit` events. Utilization is normalised to 0–100 percent in
	 * the store, so both sources land on the same scale.
	 *
	 * The pill renders once the 5-hour window is known (data persists across
	 * reloads). Each window flips to 0% the moment its `resets_at` passes.
	 *
	 * Popover interaction mirrors OverrideDropdown: a wrapper with `bind:this`, a
	 * document-level click + Escape listener that closes when the target is
	 * outside — works for desktop click and mobile tap (tap-away closes).
	 */
	import { onMount } from 'svelte';
	import { Clock } from 'lucide-svelte';
	import {
		rateLimit,
		orderedWindows,
		windowLabel,
		effectivePct,
		formatCountdown,
	} from '$lib/stores/rate-limit';

	// Per-second tick — keeps countdowns fresh and flips windows to 0% on reset.
	let now: number = $state(Date.now());
	$effect(() => {
		now = Date.now();
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

	const items = $derived.by<WindowItem[]>(() =>
		orderedWindows($rateLimit.windows).map((w) => ({
			type: w.type,
			label: windowLabel(w.type),
			pct: effectivePct(w, now),
			countdown: formatCountdown(w.resets_at, now),
			hasReset: w.resets_at != null,
		}))
	);

	// Headline: pinned to the 5-hour window only — never falls back to another
	// window. Stays null (pill hidden) until the 5-hour window is known.
	const headline = $derived.by<WindowItem | null>(
		() => items.find((it) => it.type === 'five_hour') ?? null
	);

	const hasValue = $derived(headline != null);
	const displayPct = $derived(headline?.pct ?? 0);

	// Dim the pill between an account switch and the new account's fresh snapshot
	// so we never present the previous account's numbers as current.
	const stale = $derived($rateLimit.stale ?? false);

	function severityFor(pct: number): 'neutral' | 'warning' | 'danger' {
		if (pct > 90) return 'danger';
		if (pct >= 70) return 'warning';
		return 'neutral';
	}
	const severity = $derived(headline ? severityFor(headline.pct) : 'neutral');

	const tooltip = $derived(
		headline
			? `${headline.label}: ${headline.pct}%${headline.countdown ? ` · resets in ${headline.countdown}` : ''} — click for all limits`
			: ''
	);

	// ── Popover open/close (click + tap-away) ─────────────────────────────
	let open = $state(false);
	let wrapperRef = $state<HTMLDivElement | null>(null);

	function toggle(e: MouseEvent) {
		e.stopPropagation();
		open = !open;
	}

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

{#if hasValue}
	<div class="usage-limit-wrapper" bind:this={wrapperRef}>
		<button
			type="button"
			class="usage-indicator usage-limit {severity}"
			class:stale
			title={stale ? 'Refreshing usage for the new account…' : tooltip}
			aria-busy={stale}
			aria-haspopup="dialog"
			aria-expanded={open}
			onclick={toggle}
		>
			<span class="limit-icon" aria-hidden="true">
				<Clock size={12} />
			</span>
			<div class="usage-bar">
				<div class="usage-fill" style="width: {displayPct}%"></div>
			</div>
			<span class="usage-text">{displayPct}%</span>
		</button>

		{#if open}
			<div class="usage-popover" role="dialog" aria-label="Usage limits">
				<div class="popover-title">Usage limits</div>
				{#each items as item (item.type)}
					{@const rowSeverity = severityFor(item.pct)}
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
			</div>
		{/if}
	</div>
{/if}

<style>
	.usage-limit-wrapper {
		position: relative;
		display: inline-flex;
	}

	/* Glass pill — mirrors ContextUsageBar's .usage-indicator for visual parity.
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

	/* Stale: dimmed + gently pulsing while the new account's usage is fetched. */
	.usage-indicator.stale {
		opacity: 0.5;
		animation: usage-stale-pulse 1.4s ease-in-out infinite;
	}
	@keyframes usage-stale-pulse {
		0%,
		100% {
			opacity: 0.4;
		}
		50% {
			opacity: 0.65;
		}
	}
	@media (prefers-reduced-motion: reduce) {
		.usage-indicator.stale {
			animation: none;
		}
	}

	.limit-icon {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		color: var(--muted-foreground);
		flex-shrink: 0;
	}

	.usage-bar {
		width: 36px;
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

	/* ===== Severity — this pill warns as it nears the cap ===== */
	.usage-limit.warning {
		border-color: color-mix(in oklch, var(--warning) 45%, var(--glass-border));
	}
	.usage-limit.warning .limit-icon,
	.usage-limit.warning .usage-text {
		color: var(--warning);
	}
	.usage-limit.warning .usage-fill {
		background: var(--warning);
	}

	.usage-limit.danger {
		border-color: color-mix(in oklch, var(--destructive) 55%, var(--glass-border));
	}
	.usage-limit.danger .limit-icon,
	.usage-limit.danger .usage-text {
		color: var(--destructive);
	}
	.usage-limit.danger .usage-fill {
		background: var(--destructive);
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

	/* Responsive: on mobile hide the compact bar, keep icon + % + popover. */
	@media (max-width: 639px) {
		.usage-bar {
			display: none;
		}
		.usage-indicator {
			padding: 4px 6px;
			gap: 4px;
		}
	}
</style>
