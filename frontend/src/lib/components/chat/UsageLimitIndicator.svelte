<script lang="ts">
	/**
	 * UsageLimitIndicator — compact glass pill showing the current 5-hour usage
	 * window utilization, sitting next to <ContextUsageBar /> in the controls dock.
	 *
	 * Data source: the `rateLimit` store, fed by the WS `case 'rate_limit'` handler.
	 * The CLI emits `rate_limit` events only when rate-limit *state changes* (e.g.
	 * transitioning toward `allowed_warning`), NOT proactively at all times — so
	 * this indicator renders NOTHING until a five-hour event has arrived. It never
	 * shows a fake 0%.
	 *
	 * Window selection: we key on `rate_limit_type === 'five_hour'` (canonical SDK
	 * value — see RateLimitType in claude_agent_sdk types). Other windows
	 * (seven_day, overage, …) are ignored here.
	 *
	 * Utilization: the SDK reports a fraction 0.0–1.0. We defensively normalize in
	 * case a 0–100 value ever arrives (mirrors RateLimitBanner's old logic).
	 *
	 * Lifecycle: drives a 1s tick that refreshes the reset countdown and calls
	 * `rateLimit.gcExpired()` so stale events auto-clear once `resets_at` passes.
	 * This replaces the per-second ticker that used to live in RateLimitBanner.
	 */
	import { rateLimit } from '$lib/stores/rate-limit';
	import { Clock } from 'lucide-svelte';

	// Canonical SDK value for the 5-hour ("session") rate-limit window.
	const FIVE_HOUR_TYPE = 'five_hour';

	const rlState = $derived($rateLimit);
	// Only surface the event when it describes the five-hour window.
	const event = $derived(
		rlState.event && rlState.event.rate_limit_type === FIVE_HOUR_TYPE ? rlState.event : null
	);

	// Per-second tick — keeps the reset countdown fresh and GCs expired events so
	// the pill disappears once the window has reset.
	let now: number = $state(Date.now());

	$effect(() => {
		if (!event) return;
		now = Date.now();
		const interval = setInterval(() => {
			now = Date.now();
			rateLimit.gcExpired(now);
		}, 1000);
		return () => clearInterval(interval);
	});

	/**
	 * Parse `resets_at` into epoch ms. The field is typed as a string but the CLI
	 * may emit an ISO string, epoch seconds, or epoch milliseconds — handle all.
	 */
	function parseResetMs(resetsAt: string | number | undefined): number | null {
		if (resetsAt == null) return null;
		if (typeof resetsAt === 'number') {
			// Heuristic: < 1e12 means seconds, otherwise milliseconds.
			return resetsAt < 1e12 ? resetsAt * 1000 : resetsAt;
		}
		const trimmed = resetsAt.trim();
		if (trimmed === '') return null;
		// Pure-numeric string → epoch.
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

	// Normalize utilization to an integer 0–100. SDK sends a 0–1 fraction; we also
	// accept a 0–100 percentage defensively.
	const utilizationPct = $derived.by(() => {
		const u = event?.utilization;
		if (u == null) return null;
		const pct = u <= 1 ? u * 100 : u;
		return Math.min(100, Math.max(0, Math.round(pct)));
	});

	const countdown = $derived(formatCountdown(event?.resets_at, now));

	// Severity by proximity to the cap: neutral <70%, amber 70–90%, red >90%.
	const severity = $derived.by<'neutral' | 'warning' | 'danger'>(() => {
		const pct = utilizationPct;
		if (pct == null) return 'neutral';
		if (pct > 90) return 'danger';
		if (pct >= 70) return 'warning';
		return 'neutral';
	});

	const tooltip = $derived.by(() => {
		if (!event) return '';
		const pct = utilizationPct;
		const head = pct != null ? `5-hour usage: ${pct}%` : '5-hour usage';
		const reset = countdown ? ` · resets in ${countdown}` : '';
		return `${head}${reset}`;
	});
</script>

{#if event && utilizationPct !== null}
	<div class="usage-indicator usage-limit {severity}" title={tooltip}>
		<span class="limit-icon" aria-hidden="true">
			<Clock size={12} />
		</span>
		<div class="usage-bar">
			<div class="usage-fill" style="width: {utilizationPct}%"></div>
		</div>
		<span class="usage-text">{utilizationPct}%</span>
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
