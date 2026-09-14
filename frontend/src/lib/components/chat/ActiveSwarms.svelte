<script lang="ts">
	import { ChevronDown, LoaderCircle } from 'lucide-svelte';
	import { swarms } from '$lib/stores/swarms';
	import { chatPreferences } from '$lib/stores/chatPreferences.svelte';
	import { isSwarmActive } from '$lib/utils/swarm';
	import SwarmCard from './SwarmCard.svelte';

	let { sessionId }: { sessionId?: string | null } = $props();
	const expanded = $derived(!chatPreferences.current.activeWorkersCollapsed);
	const active = $derived(Object.values($swarms.bySession[sessionId ?? ''] ?? {}).filter(isSwarmActive));
	const running = $derived(active.reduce((count, swarm) => count + swarm.tasks.filter(task => task.status === 'running').length, 0));
	// The parent passes a store-backed prop. Track the ID's value, not every
	// session-store update (messages, streaming deltas, and worker receipts).
	const hydrationSessionId = $derived(sessionId ?? null);
	$effect(() => {
		if (hydrationSessionId) void swarms.hydrate(hydrationSessionId);
	});
</script>

{#if active.length}
	<section class="active-swarms" aria-label="Active workers">
		<button class="workers-toggle" aria-expanded={expanded} onclick={() => chatPreferences.setActiveWorkersCollapsed(expanded)}>
			<LoaderCircle size={13} class="worker-spinner" aria-hidden="true" />
			<span>{running ? `${running} ${running === 1 ? 'worker' : 'workers'} running` : 'Workers queued'}</span>
			<ChevronDown size={12} class={expanded ? 'expanded' : ''} aria-hidden="true" />
		</button>
		{#if expanded}
			<div class="workflows">
				{#each active as swarm (swarm.id)}
					<SwarmCard {sessionId} input={{ swarm_id: swarm.id }} status="running" expanded={true} />
				{/each}
			</div>
		{/if}
	</section>
{/if}

<style>
	.active-swarms { min-width: 0; }
	.workers-toggle { display: flex; align-items: center; gap: 8px; padding: 6px 0; border: 0; background: transparent; color: var(--muted-foreground); font-size: 12px; cursor: pointer; }
	.workers-toggle:hover { color: var(--foreground); }
	.workers-toggle:focus-visible { outline: 2px solid var(--ring); outline-offset: 3px; border-radius: 3px; }
	.workflows { padding: 6px 0 4px 20px; min-width: 0; }
	:global(.worker-spinner) { animation: worker-spin 1s linear infinite; }
	:global(.workers-toggle .expanded) { transform: rotate(180deg); }
	@keyframes worker-spin { to { transform: rotate(360deg); } }
	@media (prefers-reduced-motion: reduce) { :global(.worker-spinner) { animation: none; } }
</style>
