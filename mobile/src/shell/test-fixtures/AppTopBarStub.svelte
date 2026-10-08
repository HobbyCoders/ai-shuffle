<script lang="ts">
	/** Test double for AppTopBar: shows which shell props reached it, and renders its actions. */
	import type { Snippet } from 'svelte';
	let props: { isAdmin?: boolean; onOpenSession: (id: string) => void; onOpenSpotlight?: () => void; hideGlobalStt?: boolean; actions?: Snippet } = $props();
</script>

<header data-testid="app-top-bar" data-admin={String(props.isAdmin)} data-hide-stt={String(props.hideGlobalStt)}>
	<button type="button" onclick={() => props.onOpenSession('s-1')}>Bar open session</button>
	<button type="button" onclick={() => props.onOpenSpotlight?.()}>Bar spotlight</button>
	{#if props.actions}<div data-testid="bar-actions">{@render props.actions()}</div>{/if}
</header>
