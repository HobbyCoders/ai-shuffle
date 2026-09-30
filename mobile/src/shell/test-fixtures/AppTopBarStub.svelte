<script lang="ts">
	/** Test double for AppTopBar: shows which shell props reached it. */
	import type { Snippet } from 'svelte';
	let props: { isAdmin?: boolean; onOpenSession: (id: string) => void; onOpenSpotlight?: () => void; actions?: Snippet; hideGlobalStt?: boolean } = $props();
</script>

<header data-testid="app-top-bar" data-admin={String(props.isAdmin)} data-hide-stt={String(props.hideGlobalStt)}>
	{#if props.actions}{@render props.actions()}{/if}
	<button type="button" onclick={() => props.onOpenSession('s-1')}>Bar open session</button>
	<button type="button" onclick={() => props.onOpenSpotlight?.()}>Bar spotlight</button>
</header>
