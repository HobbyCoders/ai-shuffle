<script lang="ts">
	/** Test double for $lib Sidebar: exposes each navigation callback as a button. */
	import type { Snippet } from 'svelte';

	let {
		isAdmin = false,
		onNewSession,
		onSettledOpenSession,
		onOpenSession,
		onOpenFeature,
		onOpenSpotlight,
		headerAction
	}: {
		isAdmin?: boolean;
		onNewSession: () => void;
		onSettledOpenSession: () => void;
		onOpenSession: (sessionId: string) => void;
		onOpenFeature: (type: 'settings') => void;
		onOpenSpotlight?: () => void;
		headerAction?: Snippet;
	} = $props();
</script>

<div data-testid="sidebar" data-admin={String(isAdmin)}>
	{@render headerAction?.()}
	<button type="button" onclick={() => onNewSession()}>Stub new session</button>
	<button type="button" onclick={() => onSettledOpenSession()}>Stub settled session</button>
	<button type="button" onclick={() => onOpenSession('s-2')}>Stub open session</button>
	<button type="button" onclick={() => onOpenFeature('settings')}>Stub open settings</button>
	{#if onOpenSpotlight}
		<button type="button" onclick={() => onOpenSpotlight?.()}>Stub spotlight</button>
	{/if}
	<button
		type="button"
		onclick={() => window.dispatchEvent(new CustomEvent('open-session', { detail: { sessionId: 's-3' } }))}
	>Stub notification</button>
</div>
