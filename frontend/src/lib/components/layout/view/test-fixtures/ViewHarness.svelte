<script lang="ts">
	import PanelHost from './PanelHost.svelte';
	import ViewPage from './ViewPage.svelte';
	let { active = true, contentFirst = false, readOnly = true, host = true }: { active?: boolean; contentFirst?: boolean; readOnly?: boolean; host?: boolean } = $props();
</script>
{#if host}<PanelHost />{/if}
<ViewPage {active} {contentFirst} {readOnly} />
