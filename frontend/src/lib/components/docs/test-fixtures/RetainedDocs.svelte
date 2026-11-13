<script lang="ts">
	// Docs and another ViewShell destination retained side by side, as MainPanel
	// keeps them, with AppPage's history follower and the layout choosing which shows.
	import { setContext, untrack } from 'svelte';
	import { page } from '$app/state';
	import { layout } from '$lib/stores/layout.svelte';
	import { followDocsHistory } from '$lib/docs/history';
	import { FEATURE_PANEL_CONTEXT } from '$lib/components/layout/feature-panel-context';
	import ViewPage from '$lib/components/layout/view/test-fixtures/ViewPage.svelte';
	import DocsCard from '../DocsCard.svelte';
	let { initialItemId = null }: { initialItemId?: string | null } = $props();
	const docs = $derived(layout.current.activeView?.type === 'docs');
	setContext(FEATURE_PANEL_CONTEXT, { get active() { return docs; } });
	let first = true;
	$effect(() => {
		const state = page.state;
		untrack(() => {
			followDocsHistory(state, first);
			first = false;
		});
	});
</script>
<DocsCard mobile {initialItemId} />
<ViewPage active={!docs} />
