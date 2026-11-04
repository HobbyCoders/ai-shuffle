<script lang="ts">
	/**
	 * The Providers list: one card of connected credentials, grouped under a
	 * quiet provider label, then one card of the providers with nothing connected.
	 */
	import ListGroupLabel from '../rows/ListGroupLabel.svelte';
	import CredentialRow from './CredentialRow.svelte';
	import NotConnectedRow from './NotConnectedRow.svelte';
	import ProviderGroupHeader from './ProviderGroupHeader.svelte';
	import { filterProviders, splitProviders, type ProviderView } from './providers-model';

	interface Props {
		views: ProviderView[];
		filter?: string;
	}

	let { views, filter = '' }: Props = $props();

	const groups = $derived(splitProviders(filterProviders(views, filter)));
</script>

{#if groups.connected.length}
	<div class="pg st-card">
		{#each groups.connected as provider (provider.id)}
			<div class="pv" role="group" aria-label={provider.name}>
				<ProviderGroupHeader {provider} />
				{#each provider.credentials as credential (credential.item)}
					<CredentialRow {credential} {provider} />
				{/each}
			</div>
		{/each}
	</div>
{/if}

{#if groups.notConnected.length}
	<ListGroupLabel label="Not connected" />
	<div class="pg st-card">
		{#each groups.notConnected as provider (provider.id)}
			<NotConnectedRow {provider} />
		{/each}
	</div>
{/if}

{#if !groups.connected.length && !groups.notConnected.length}
	<p class="pv-empty">Nothing matches.</p>
{/if}

<style>
	.pg {
		position: relative;
		margin-bottom: 12px;
	}
	/* Hairline between every row; the card's own edge ends the list. */
	.pg > :global(* + *),
	.pv > :global(* + *) {
		border-top: 1px solid var(--st-border);
	}
</style>
