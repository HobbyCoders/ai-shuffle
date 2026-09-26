<script lang="ts">
	/** Test double for AppTopBar: shows which shell props reached it. */
	let props: { isAdmin?: boolean; onOpenSession: (id: string) => void; onOpenSpotlight?: () => void } = $props();
</script>

<div data-testid="app-top-bar" data-admin={String(props.isAdmin)}>
	<button type="button" onclick={() => props.onOpenSession('s-1')}>Bar open session</button>
	<button type="button" onclick={() => props.onOpenSpotlight?.()}>Bar spotlight</button>
</div>
