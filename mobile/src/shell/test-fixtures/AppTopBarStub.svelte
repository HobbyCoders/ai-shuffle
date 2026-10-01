<script lang="ts">
	/** Test double for AppTopBar: shows which shell props reached it. */
	let props: { isAdmin?: boolean; onOpenSession: (id: string) => void; onOpenSpotlight?: () => void; hideGlobalStt?: boolean } = $props();
</script>

<header data-testid="app-top-bar" data-admin={String(props.isAdmin)} data-hide-stt={String(props.hideGlobalStt)}>
	<button type="button" onclick={() => props.onOpenSession('s-1')}>Bar open session</button>
	<button type="button" onclick={() => props.onOpenSpotlight?.()}>Bar spotlight</button>
</header>
