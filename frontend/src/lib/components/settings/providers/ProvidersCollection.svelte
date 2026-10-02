<script lang="ts">
	/**
	 * Providers collection. Stub from F0; slice B builds the list and details.
	 */
	import { KeyRound } from 'lucide-svelte';
	import CollectionPage from '../shell/CollectionPage.svelte';
	import EmptyDetail from '../shell/EmptyDetail.svelte';

	let filter = $state('');
</script>

<CollectionPage category="providers" bind:filter>
	{#snippet list()}
		<p class="st-empty">No providers connected yet.</p>
	{/snippet}
	{#snippet detail()}
		<EmptyDetail icon={KeyRound} message="Select a provider." />
	{/snippet}
</CollectionPage>
