<script lang="ts">
	/**
	 * The Providers list: under "Connected", each provider is a quiet label above
	 * its own card of credentials; under "Not connected", one card of the rest.
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

<div class="pl">
{#if groups.connected.length}
	<ListGroupLabel label="Connected" />
	{#each groups.connected as provider (provider.id)}
		<div class="pv" role="group" aria-label={provider.name}>
			<ProviderGroupHeader {provider} />
			<div class="pg st-card">
				{#each provider.credentials as credential (credential.item)}
					<CredentialRow {credential} {provider} />
				{/each}
			</div>
		</div>
	{/each}
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
</div>

<style>
	/* One text edge: glyph column, then group names, card text and rows. */
	.pl {
		--pv-glyph: 16px;
		--pv-glyph-gap: 10px;
		--pv-row-inset: 16px;
	}
	/* With the list header's 12px, 24px under the filter. */
	.pl > :global(.st-lg:first-child) {
		margin-top: 4px;
	}
	.pg {
		position: relative;
	}
	.pv + .pv {
		margin-top: 20px;
	}
	/* Hairline between every row; the card's own edge ends the list. */
	.pg > :global(* + *) {
		border-top: 1px solid var(--st-border);
	}
</style>
