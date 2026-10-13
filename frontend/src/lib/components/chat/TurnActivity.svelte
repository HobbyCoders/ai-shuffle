<script lang="ts" module>
	import type { LiveCompaction } from '$lib/stores/session/v2/reducer';

	/** How long a failed compaction's reason stays on the activity line. */
	export const COMPACTION_FAILURE_VISIBLE_MS = 8000;

	/** Why the compaction is running, when known: who asked, else the trigger. */
	export function compactionReason(c: LiveCompaction): string | null {
		if (c.requestedBy === 'app') return 'requested by AI Shuffle';
		if (c.requestedBy === 'user' || c.trigger === 'manual') return 'manual';
		if (c.trigger === 'auto') return 'auto';
		return null;
	}

	/** True while the compaction line should show instead of (or without) "Working". */
	export function compactionVisible(c: LiveCompaction | null | undefined, now: number): boolean {
		if (!c) return false;
		if (c.phase === 'running') return true;
		const at = c.at ? Date.parse(c.at) : NaN;
		// No timestamp: the next turn boundary clears it anyway.
		return !Number.isFinite(at) || now - at < COMPACTION_FAILURE_VISIBLE_MS;
	}
</script>

<script lang="ts">
	/** Persistent live-turn status. It is intentionally independent from tool rows,
	 * so a think gap can never make the transcript appear frozen. While the
	 * conversation is being compacted it says so instead of "Working". */
	import { onDestroy, onMount } from 'svelte';
	import { formatDuration } from '$lib/utils/work-log';

	interface Props {
		startedAt?: string | null;
		compaction?: LiveCompaction | null;
		/** False when only a compaction may show: with nothing to report the
		 *  line renders nothing instead of "Working…". */
		working?: boolean;
	}

	let { startedAt = null, compaction = null, working = true }: Props = $props();
	let now = $state(Date.now());
	let timer: ReturnType<typeof setInterval> | null = null;

	onMount(() => {
		timer = setInterval(() => { now = Date.now(); }, 1000);
	});

	onDestroy(() => {
		if (timer) clearInterval(timer);
	});

	function since(iso: string | null): string | null {
		if (!iso) return null;
		const start = Date.parse(iso);
		if (!Number.isFinite(start)) return null;
		return formatDuration(Math.max(0, now - start));
	}

	const elapsed = $derived(since(startedAt));
	const shownCompaction = $derived(compactionVisible(compaction, now) ? compaction : null);
	const label = $derived.by(() => {
		const c = shownCompaction;
		if (!c) return elapsed ? `Working for ${elapsed}` : 'Working…';
		const reason = compactionReason(c);
		if (c.phase === 'failed') {
			return `Compaction failed${c.error ? `: ${c.error}` : ''}`;
		}
		const took = since(c.at);
		return `Compacting context…${reason ? ` (${reason})` : ''}${took ? ` ${took}` : ''}`;
	});
</script>

{#if shownCompaction || working}
<div
	class="turn-activity"
	class:compacting={shownCompaction?.phase === 'running'}
	class:compaction-failed={shownCompaction?.phase === 'failed'}
	role="status"
	aria-label={shownCompaction ? 'Context compaction' : 'Assistant is working'}
	data-testid="turn-activity"
>
	<span class="activity-dot" aria-hidden="true"></span>
	<span>{label}</span>
</div>
{/if}

<style>
	.turn-activity {
		display: flex;
		align-items: center;
		gap: 8px;
		min-height: 30px;
		padding: 3px 8px 7px;
		border-bottom: 1px solid color-mix(in oklch, var(--border) 72%, transparent);
		font-size: 12.5px;
		line-height: 1.5;
		font-variant-numeric: tabular-nums;
		color: var(--muted-foreground);
		animation: activity-enter 180ms ease-out both;
	}

	.activity-dot {
		width: 5px;
		height: 5px;
		border-radius: 999px;
		background: var(--primary);
		box-shadow: 0 0 0 3px color-mix(in oklch, var(--primary) 12%, transparent);
		animation: activity-breathe 1.8s ease-in-out infinite;
	}

	.compaction-failed .activity-dot {
		background: var(--destructive);
		box-shadow: none;
		animation: none;
	}

	@keyframes activity-enter {
		from { opacity: 0; transform: translateY(2px); }
		to { opacity: 1; transform: translateY(0); }
	}

	@keyframes activity-breathe {
		50% { opacity: 0.38; }
	}

	@media (prefers-reduced-motion: reduce) {
		.turn-activity,
		.activity-dot { animation: none; }
	}
</style>
