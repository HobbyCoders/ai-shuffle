<script lang="ts">
	/**
	 * SwarmHistory — the chat's swarm workflows as SwarmCards, newest first.
	 *
	 * The running-tasks sheet shows finished workflows here (stages, errors and
	 * each member's transcript), so a swarm stays reviewable after it leaves
	 * the running list (#1816). `finishedOnly` hides active workflows, which
	 * the list already shows as rows.
	 */
	import { swarms } from '$lib/stores/swarms';
	import { isSwarmActive } from '$lib/utils/swarm';
	import SwarmCard from './SwarmCard.svelte';

	let { sessionId, finishedOnly = false }: { sessionId: string; finishedOnly?: boolean } = $props();

	const workflows = $derived(
		Object.values($swarms.bySession[sessionId] ?? {})
			.filter((w) => !finishedOnly || !isSwarmActive(w))
			.sort((a, b) => Date.parse(b.updated_at) - Date.parse(a.updated_at))
	);
	const hydrationId = $derived(sessionId);
	$effect(() => {
		if (hydrationId) void swarms.hydrate(hydrationId);
	});
</script>

<div class="swarm-history" aria-label="Swarm workflows">
	{#each workflows as workflow (workflow.id)}
		<SwarmCard
			{sessionId}
			input={{ action: 'status', swarm_id: workflow.id }}
			status={isSwarmActive(workflow) ? 'running' : workflow.status === 'completed' ? 'complete' : 'error'}
		/>
	{:else}
		<p>No swarm workflows in this chat yet.</p>
	{/each}
</div>

<style>
	.swarm-history { display: grid; gap: 8px; min-width: 0; }
	p { margin: 0; padding: 8px; color: var(--muted-foreground); font-size: 12px; }
</style>
