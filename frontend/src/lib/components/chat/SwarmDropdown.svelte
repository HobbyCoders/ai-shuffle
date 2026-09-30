<script lang="ts">
	import { Workflow, ChevronDown } from 'lucide-svelte';
	import { buildShell } from '$lib/utils/platform';
	import { swarms } from '$lib/stores/swarms';
	import { isSwarmActive } from '$lib/utils/swarm';
	import SwarmCard from './SwarmCard.svelte';

	let { sessionId, standalone = false, hydrateOnly = false }: {
		sessionId?: string | null;
		standalone?: boolean;
		hydrateOnly?: boolean;
	} = $props();
	const workflows = $derived(Object.values($swarms.bySession[sessionId ?? ''] ?? {})
		.sort((a, b) => Date.parse(b.updated_at) - Date.parse(a.updated_at)));
	const activeCount = $derived(workflows.filter(isSwarmActive).length);
	let disclosure = $state<HTMLDetailsElement | undefined>();
	function closeOnEscape(event: KeyboardEvent) {
		if (standalone && event.key === 'Escape' && disclosure?.open) {
			disclosure.open = false;
			disclosure.querySelector('summary')?.focus({ preventScroll: true });
		}
	}
	function closeOutside(event: MouseEvent) {
		if (standalone && disclosure?.open && event.target instanceof Node && !disclosure.contains(event.target)) disclosure.open = false;
	}
	$effect(() => { if (sessionId) void swarms.hydrate(sessionId); });
</script>

<svelte:window onkeydown={closeOnEscape} onclick={closeOutside} />

{#if sessionId && !hydrateOnly}
	<details bind:this={disclosure} class="swarm-dropdown" class:standalone class:mobile={buildShell() === 'mobile'}>
		<summary aria-label="Swarm workflows">
			<Workflow size={16} strokeWidth={1.6} />
			<span>Swarm workflows</span>
			{#if activeCount}<small>{activeCount} active</small>{/if}
			<ChevronDown size={14} class="chevron" />
		</summary>
		<div class="swarm-list" aria-label="Swarm workflow history">
			{#each workflows as workflow (workflow.id)}
				<SwarmCard {sessionId} result={JSON.stringify({ swarm: workflow })} status={isSwarmActive(workflow) ? 'running' : workflow.status === 'completed' ? 'complete' : 'error'} />
			{:else}
				<p>No swarm workflows in this chat yet.</p>
			{/each}
		</div>
	</details>
{/if}

<style>
	.swarm-dropdown { min-width: 0; border-top: 1px solid var(--border); }
	summary { display: flex; align-items: center; gap: 8px; min-height: 36px; padding: 0 12px; list-style: none; cursor: pointer; font-size: 12px; }
	summary::-webkit-details-marker { display: none; }
	summary:focus-visible { outline: 2px solid var(--ring); outline-offset: -2px; }
	summary :global(svg) { flex-shrink: 0; }
	summary small { color: var(--muted-foreground); margin-left: auto; }
	summary :global(.chevron) { margin-left: auto; }
	.swarm-list { display: grid; gap: 8px; min-width: 0; padding: 8px; }
	.swarm-list p { margin: 0; padding: 8px; color: var(--muted-foreground); font-size: 12px; }
	.standalone { position: relative; border: 0; }
	.mobile > summary { min-height: 44px; }
	.standalone > summary { min-height: 28px; border: 1px solid var(--border); border-radius: 6px; background: var(--card); }
	.standalone > .swarm-list { position: absolute; right: 0; top: calc(100% + 6px); z-index: 50; width: min(380px, calc(100vw - 24px)); max-height: calc(100dvh - 120px); overflow: auto; border: 1px solid var(--border); border-radius: 8px; background: var(--card); box-shadow: var(--shadow-l); }
	.standalone.mobile > summary { min-height: 44px; }
	.standalone.mobile > .swarm-list { position: fixed; top: 110px; left: 12px; right: 12px; width: auto; }
	@media (max-width: 700px) { summary, .standalone > summary { min-height: 44px; } .standalone > .swarm-list { position: fixed; top: 110px; left: 12px; right: 12px; width: auto; } }
</style>
