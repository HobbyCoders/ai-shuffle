<script lang="ts">
	/** A collection whose open item is gone: the detail is a "Not found" state, as on the Hooks page. */
	import { settingsNav } from '$lib/settings/navigation.svelte';
	import CollectionPage from '../CollectionPage.svelte';
	import EmptyDetail from '../EmptyDetail.svelte';

	const selected = $derived(settingsNav.selectedItem('hooks'));
</script>

<CollectionPage category="hooks">
	{#snippet list()}
		<button type="button" data-settings-item="a" data-settings-nav-item>A</button>
	{/snippet}
	{#snippet detail()}
		{#if selected}
			<EmptyDetail title="Not found" message="This hook was deleted, or belongs to another project." />
		{:else}
			<EmptyDetail message="Select a hook." idle />
		{/if}
	{/snippet}
</CollectionPage>
