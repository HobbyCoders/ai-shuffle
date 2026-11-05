<script lang="ts">
	import PanelHost from './PanelHost.svelte';
	import ViewPage from './ViewPage.svelte';
	let { active = true, readOnly = true, host = true }: { active?: boolean; readOnly?: boolean; host?: boolean } = $props();
</script>
{#if host}<PanelHost />{/if}
<ViewPage {active} {readOnly} />
