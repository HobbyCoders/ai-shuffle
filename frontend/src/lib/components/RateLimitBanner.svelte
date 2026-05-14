<script lang="ts">
	/**
	 * RateLimitBanner — thin amber banner that appears above `<ChatInput>` when a
	 * `rate_limit` WS event is active. Reads from `rateLimit` store.
	 *
	 * Slice C (SDK 0.1.65→0.1.81 upgrade). Auto-clears when `resets_at` is in the
	 * past via `rateLimit.gcExpired()`; the per-second ticker keeps the countdown
	 * label fresh until then.
	 */
	import { rateLimit } from '$lib/stores/rate-limit';
	import { Hourglass, X } from 'lucide-svelte';

	// Subscribe to the store and recompute on every change.
	const rlState = $derived($rateLimit);
	const event = $derived(rlState.event);

	// Distinct key per event; the banner shows when there's an event AND the
	// dismiss key doesn't match the active event.
	const eventKey = $derived(
		event ? `${event.rate_limit_type}|${event.resets_at ?? ''}|${event.received_at}` : null
	);
	const visible = $derived(
		Boolean(event && (!rlState.dismissedEventKey || rlState.dismissedEventKey !== eventKey))
	);

	// Tick once per second to drive the countdown label + GC.
	let now: number = $state(Date.now());

	$effect(() => {
		if (!visible) return;
		now = Date.now();
		const interval = setInterval(() => {
			now = Date.now();
			rateLimit.gcExpired(now);
		}, 1000);
		return () => clearInterval(interval);
	});

	function formatCountdown(resetsAt: string | undefined, nowMs: number): string {
		if (!resetsAt) return '';
		const target = Date.parse(resetsAt);
		if (Number.isNaN(target)) return '';
		const diffMs = target - nowMs;
		if (diffMs <= 0) return 'now';
		const totalSec = Math.ceil(diffMs / 1000);
		const h = Math.floor(totalSec / 3600);
		const m = Math.floor((totalSec % 3600) / 60);
		const s = totalSec % 60;
		if (h > 0) return `${h}h ${m}m`;
		if (m > 0) return `${m}m ${s}s`;
		return `${s}s`;
	}

	const countdown = $derived(formatCountdown(event?.resets_at, now));
	// Backend may send utilization as 0–1 (ratio) or 0–100 (percentage).
	// Normalize to 0–100 inclusive.
	const utilizationPct = $derived.by(() => {
		const u = event?.utilization;
		if (u == null) return null;
		const pct = u <= 1 ? u * 100 : u;
		return Math.min(100, Math.max(0, Math.round(pct)));
	});

	function handleDismiss() {
		rateLimit.dismiss();
	}
</script>

{#if visible && event}
	<div class="rate-limit-banner" role="status" aria-live="polite">
		<div class="rl-row">
			<div class="rl-icon" aria-hidden="true">
				<Hourglass size={14} />
			</div>
			<div class="rl-content">
				<div class="rl-title">
					<span class="rl-headline">Rate limited</span>
					<span class="rl-sep" aria-hidden="true">·</span>
					<span class="rl-type">{event.rate_limit_type}</span>
				</div>
				{#if countdown}
					<div class="rl-countdown">resets in {countdown}</div>
				{/if}
			</div>
			<button
				type="button"
				class="rl-dismiss"
				onclick={handleDismiss}
				aria-label="Dismiss rate-limit banner"
				title="Dismiss"
			>
				<X size={14} />
			</button>
		</div>
		{#if utilizationPct !== null}
			<div class="rl-bar" aria-hidden="true">
				<div class="rl-bar-fill" style="width: {utilizationPct}%;"></div>
			</div>
		{/if}
	</div>
{/if}

<style>
	.rate-limit-banner {
		display: flex;
		flex-direction: column;
		gap: 6px;
		width: 100%;
		max-width: 100%;
		padding: 8px 12px;
		background: color-mix(in oklch, var(--warning, #d97706) 10%, var(--card));
		border-top: 1px solid color-mix(in oklch, var(--warning, #d97706) 30%, var(--border));
		border-bottom: 1px solid color-mix(in oklch, var(--warning, #d97706) 30%, var(--border));
		color: var(--foreground);
		box-sizing: border-box;
		animation: rl-slide-down 0.2s ease-out;
	}

	@keyframes rl-slide-down {
		from {
			opacity: 0;
			transform: translateY(-6px);
		}
		to {
			opacity: 1;
			transform: translateY(0);
		}
	}

	.rl-row {
		display: flex;
		align-items: center;
		gap: 10px;
	}

	.rl-icon {
		flex-shrink: 0;
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 22px;
		height: 22px;
		border-radius: 6px;
		background: color-mix(in oklch, var(--warning, #d97706) 25%, transparent);
		color: var(--warning, #d97706);
	}

	.rl-content {
		flex: 1;
		min-width: 0;
		display: flex;
		flex-direction: column;
		gap: 1px;
	}

	.rl-title {
		display: flex;
		align-items: center;
		gap: 6px;
		font-size: 0.75rem;
		line-height: 1.2;
		flex-wrap: wrap;
	}

	.rl-headline {
		font-weight: 600;
		color: var(--warning, #d97706);
	}

	.rl-sep {
		color: var(--muted-foreground);
	}

	.rl-type {
		color: var(--foreground);
		font-family: var(--font-mono);
		font-size: 0.7rem;
	}

	.rl-countdown {
		font-size: 0.6875rem;
		color: var(--muted-foreground);
		font-variant-numeric: tabular-nums;
	}

	.rl-dismiss {
		flex-shrink: 0;
		display: inline-flex;
		align-items: center;
		justify-content: center;
		min-width: 32px;
		min-height: 32px;
		padding: 4px;
		background: transparent;
		border: 0;
		border-radius: 6px;
		color: var(--muted-foreground);
		cursor: pointer;
		transition: all 0.15s ease;
	}

	.rl-dismiss:hover {
		color: var(--foreground);
		background: color-mix(in oklch, var(--foreground) 8%, transparent);
	}

	.rl-dismiss:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
	}

	.rl-bar {
		height: 3px;
		width: 100%;
		background: color-mix(in oklch, var(--warning, #d97706) 15%, var(--card));
		border-radius: 999px;
		overflow: hidden;
	}

	.rl-bar-fill {
		height: 100%;
		background: var(--warning, #d97706);
		border-radius: 999px;
		transition: width 1s linear;
	}

	/* Mobile: ensure dismiss button stays at 44px tap target spec. */
	@media (max-width: 639px) {
		.rl-dismiss {
			min-width: 44px;
			min-height: 44px;
		}
	}
</style>
