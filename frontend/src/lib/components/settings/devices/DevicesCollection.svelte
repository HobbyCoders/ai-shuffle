<script lang="ts">
	/**
	 * Devices collection. Stub from F0; slice D builds the list and details.
	 */
	import { MonitorSmartphone } from 'lucide-svelte';
	import CollectionPage from '../shell/CollectionPage.svelte';
	import EmptyDetail from '../shell/EmptyDetail.svelte';

	let filter = $state('');
</script>

<CollectionPage category="devices" bind:filter>
	{#snippet list()}
		<p class="st-empty">No devices yet.</p>
	{/snippet}
	{#snippet detail()}
		<EmptyDetail icon={MonitorSmartphone} message="Select a device." />
	{/snippet}
</CollectionPage>
