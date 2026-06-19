<script lang="ts">
	/**
	 * UsageLimitIndicator — compact glass pill showing the current 5-hour usage
	 * window, sitting next to <ContextUsageBar /> in the controls dock.
	 *
	 * Renders nothing until the first reading WITH REAL DATA arrives — the CLI only
	 * reports a 5-hour utilization once you're meaningfully into a window (and emits
	 * data-less utilization=null events before then), so an empty pill would just
	 * take up toolbar space ~90% of the time. Once a real reading lands it stays
	 * visible (the value persists across reloads).
	 *
	 * Data source: the `rateLimit` store's persisted `lastFiveHour` reading, fed by
	 * the WS `case 'rate_limit'` handler. The CLI only reports 5-hour usage when the
	 * rate-limit *status changes* (there's no proactive/queryable live feed), so this
	 * pill shows the MOST RECENT reported value at all times — not a second-by-second
	 * figure. It refreshes whenever a new reading arrives and drops to 0% once the
	 * window rolls over (`resets_at` passes → a fresh 5-hour window).
	 *
	 * Window selection: keyed on `rate_limit_type === 'five_hour'` (the store only
	 * stores five-hour readings into `lastFiveHour`).
	 *
	 * Utilization: the SDK reports a fraction 0.0–1.0; we defensively accept 0–100 too.
	 */
	import { rateLimit } from '$lib/stores/rate-limit';
	import { Clock } from 'lucide-svelte';

	// Last-known five-hour reading (persisted across reloads).
	const reading = $derived($rateLimit.lastFiveHour);

	// A reading only counts as data once it carries a numeric `utilization`. The
	// CLI also emits five-hour events with utilization=null (the window opened but
	// nothing's been used yet); those must never surface the pill — not even after
	// the window later rolls over, which would otherwise force a hollow "0%".
	// (Regression after #372: the windowReset branch below resurrected such empty
	// readings once resets_at passed.)
	const hasReading = $derived(reading != null && typeof reading.utilization === 'number');

	// Per-second tick — keeps the reset countdown fresh and lets the pill flip to
	// 0% the moment the window rolls over. Only runs while we have a real reading.
	let now: number = $state(Date.now());
	$effect(() => {
		if (!hasReading) return;
		now = Date.now();
		const interval = setInterval(() => {
			now = Date.now();
		}, 1000);
		return () => clearInterval(interval);
	});

	/** Parse `resets_at` (ISO string, epoch seconds, or epoch ms) into epoch ms. */
	function parseResetMs(resetsAt: string | number | undefined): number | null {
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

	function formatCountdown(resetsAt: string | undefined, nowMs: number): string {
		const target = parseResetMs(resetsAt);
		if (target == null) return '';
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

	const resetMs = $derived(reading ? parseResetMs(reading.resets_at) : null);
	// Window has rolled over → a fresh 5-hour window has begun (≈0% used).
	const windowReset = $derived(resetMs != null && resetMs <= now);

	// Last-known utilization normalized to an integer 0–100 (SDK sends 0–1).
	const rawPct = $derived.by(() => {
		const u = reading?.utilization;
		if (u == null) return null;
		const pct = u <= 1 ? u * 100 : u;
		return Math.min(100, Math.max(0, Math.round(pct)));
	});

	// Display %: 0 right after a window reset, otherwise the last-known value.
	// null when there's no real reading yet (pill stays hidden — see hasReading).
	const displayPct = $derived(hasReading ? (windowReset ? 0 : rawPct) : null);
	const hasValue = $derived(displayPct != null);

	const countdown = $derived(reading && !windowReset ? formatCountdown(reading.resets_at, now) : '');

	// Severity by proximity to the cap: neutral <70%, amber 70–90%, red >90%.
	const severity = $derived.by<'neutral' | 'warning' | 'danger'>(() => {
		if (displayPct == null) return 'neutral';
		if (displayPct > 90) return 'danger';
		if (displayPct >= 70) return 'warning';
		return 'neutral';
	});

	const tooltip = $derived.by(() => {
		if (displayPct == null) return '5-hour usage — updates as you use Claude';
		if (windowReset) return '5-hour usage: 0% · new window';
		const reset = countdown ? ` · resets in ${countdown}` : '';
		return `5-hour usage: ${displayPct}%${reset}`;
	});
</script>

{#if hasValue}
	<div class="usage-indicator usage-limit {severity}" title={tooltip}>
		<span class="limit-icon" aria-hidden="true">
			<Clock size={12} />
		</span>
		<div class="usage-bar">
			<div class="usage-fill" style="width: {displayPct ?? 0}%"></div>
		</div>
		<span class="usage-text">{displayPct}%</span>
	</div>
{/if}

<style>
	/* Glass pill — mirrors ContextUsageBar's .usage-indicator for visual parity. */
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

	/* ===== Severity — this pill warns as it nears the cap (context bar stays neutral) ===== */
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

	/* Responsive: on mobile hide the bar, show only the icon + percentage. */
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
