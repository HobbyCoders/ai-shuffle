<script lang="ts">
	/**
	 * Default models collection. Stub from F0; slice C builds the list and details.
	 */
	import { Sparkles } from 'lucide-svelte';
	import CollectionPage from '../shell/CollectionPage.svelte';
	import EmptyDetail from '../shell/EmptyDetail.svelte';

	let filter = $state('');
</script>

<CollectionPage category="models" bind:filter>
	{#snippet list()}
		<p class="st-empty">No tasks to show yet.</p>
	{/snippet}
	{#snippet detail()}
		<EmptyDetail icon={Sparkles} message="Select a task." />
	{/snippet}
</CollectionPage>
