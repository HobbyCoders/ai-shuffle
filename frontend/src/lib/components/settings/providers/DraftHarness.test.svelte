<script lang="ts">
	/** Test harness: renders the live draft's detail, as the collection does. */
	import { providers } from '$lib/stores/providers.svelte';
	import DraftDetail from './DraftDetail.svelte';
	import { providerDrafts } from './drafts.svelte';
	import { buildProviders } from './providers-model';

	let { item }: { item: string } = $props();

	const draft = $derived(providerDrafts.get(item));
	const provider = $derived.by(() => {
		const s = providers.current;
		const views = buildProviders({
			rows: s.providers ?? [],
			claude: s.claude ?? null,
			fal: { keySet: false, masked: '', enabled: true },
			drafts: providerDrafts.list,
			draftGateways: providerDrafts.gateways
		});
		return views.find((p) => p.id === draft?.providerId);
	});
</script>

{#if draft && provider}
	<DraftDetail {draft} {provider} />
{:else}
	<p data-testid="draft-gone">Draft finished</p>
{/if}
