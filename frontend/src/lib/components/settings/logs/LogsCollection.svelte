<script lang="ts">
	/**
	 * Logs collection. Stub from F0; slice D builds the list and details.
	 */
	import { ScrollText } from 'lucide-svelte';
	import CollectionPage from '../shell/CollectionPage.svelte';
	import EmptyDetail from '../shell/EmptyDetail.svelte';

	let filter = $state('');
</script>

<CollectionPage category="logs" bind:filter filterPlaceholder="Search logs" filterIcon="search">
	{#snippet list()}
		<p class="st-empty">No log entries yet.</p>
	{/snippet}
	{#snippet detail()}
		<EmptyDetail icon={ScrollText} message="Select a log entry." />
	{/snippet}
</CollectionPage>
