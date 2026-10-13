<script lang="ts">
	import { swarms } from '$lib/stores/swarms';
	import { isSwarmActive } from '$lib/utils/swarm';
	import SwarmCard from './SwarmCard.svelte';

	let { sessionId }: { sessionId?: string | null } = $props();
	const active = $derived(Object.values($swarms.bySession[sessionId ?? ''] ?? {}).filter(isSwarmActive));
	// The parent passes a store-backed prop. Track the ID's value, not every
	// session-store update (messages, streaming deltas, and worker receipts).
	const hydrationSessionId = $derived(sessionId ?? null);
	$effect(() => {
		if (hydrationSessionId) void swarms.hydrate(hydrationSessionId);
	});
</script>

{#if active.length}
	<section class="active-swarms" aria-label="Active workers">
		{#each active as swarm (swarm.id)}
			<SwarmCard {sessionId} input={{ swarm_id: swarm.id }} status="running" expanded={true} />
		{/each}
	</section>
{/if}

<style>
	.active-swarms { display: grid; gap: 6px; min-width: 0; padding: 6px 0 4px; }
</style>
