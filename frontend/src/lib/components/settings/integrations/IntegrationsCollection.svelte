<script lang="ts">
	/**
	 * Integrations collection. Stub from F0; slice D builds the list and details.
	 */
	import { Plug } from 'lucide-svelte';
	import CollectionPage from '../shell/CollectionPage.svelte';
	import EmptyDetail from '../shell/EmptyDetail.svelte';

	let filter = $state('');
</script>

<CollectionPage category="integrations" bind:filter>
	{#snippet list()}
		<p class="st-empty">No integrations yet.</p>
	{/snippet}
	{#snippet detail()}
		<EmptyDetail icon={Plug} message="Select an integration." />
	{/snippet}
</CollectionPage>
