<script lang="ts">
	/** Persistent live-turn status. It is intentionally independent from tool rows,
	 * so a think gap can never make the transcript appear frozen. */
	import { onDestroy, onMount } from 'svelte';
	import { formatDuration } from '$lib/utils/work-log';

	interface Props {
		startedAt?: string | null;
	}

	let { startedAt = null }: Props = $props();
	let now = $state(Date.now());
	let timer: ReturnType<typeof setInterval> | null = null;

	onMount(() => {
		timer = setInterval(() => { now = Date.now(); }, 1000);
	});

	onDestroy(() => {
		if (timer) clearInterval(timer);
	});

	const elapsed = $derived.by(() => {
		if (!startedAt) return null;
		const start = Date.parse(startedAt);
		if (!Number.isFinite(start)) return null;
		return formatDuration(Math.max(0, now - start));
	});
</script>

<div class="turn-activity" role="status" aria-label="Assistant is working">
	<span class="activity-dot" aria-hidden="true"></span>
	<span>{elapsed ? `Working for ${elapsed}` : 'Working…'}</span>
</div>

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
