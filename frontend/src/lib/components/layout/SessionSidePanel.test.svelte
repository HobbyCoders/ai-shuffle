<script lang="ts">
	import type { ComponentProps } from 'svelte';
	import SessionSidePanel from './SessionSidePanel.svelte';
	import LazyPanel from '../deck/lazy/LazyPanel.svelte';
	import type { LazyComponentHandle } from '../deck/lazy/lazy-component';
	let { handle, ...props }: Omit<ComponentProps<typeof SessionSidePanel>, 'children'> & { handle?: LazyComponentHandle } = $props();
</script>

<SessionSidePanel {...props}>
	{#snippet children(panelActions)}
		{#if handle}
			<LazyPanel {handle} label="changed files" skeletonDelayMs={0} fallbackActions={panelActions} />
		{:else}
			<header aria-label="Content toolbar">Panel content {@render panelActions()}</header>
		{/if}
	{/snippet}
</SessionSidePanel>
