<script lang="ts">
	// Docs and another ViewShell destination retained side by side, as MainPanel keeps them.
	import { setContext } from 'svelte';
	import { FEATURE_PANEL_CONTEXT } from '$lib/components/layout/feature-panel-context';
	import ViewPage from '$lib/components/layout/view/test-fixtures/ViewPage.svelte';
	import DocsCard from '../DocsCard.svelte';
	let { docs = true, initialItemId = null }: { docs?: boolean; initialItemId?: string | null } = $props();
	setContext(FEATURE_PANEL_CONTEXT, { get active() { return docs; } });
</script>
<DocsCard mobile {initialItemId} />
<ViewPage active={!docs} />
