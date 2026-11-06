<script lang="ts">
	/**
	 * The Providers list: under "Connected", one card per provider, its name and
	 * "Add account" first and its accounts below; under "Not connected", one card
	 * of the rest. Glyphs and names sit on one x in both.
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
		<div class="pg st-card" role="group" aria-label={provider.name}>
			<ProviderGroupHeader {provider} />
			{#each provider.credentials as credential (credential.item)}
				<CredentialRow {credential} {provider} />
			{/each}
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
	/* The card's text edge (17px from its outside, 16px on the phone), then the
	   glyph column. Accounts have no glyph, so they start on that edge. */
	.pl {
		--pv-glyph: 16px;
		--pv-glyph-gap: 10px;
		--pv-row-inset: 16px;
	}
	:global(.settings-shell[data-stacked]) .pl {
		--pv-row-inset: 15px;
	}
	/* With the list header's 12px, 24px under the filter. */
	.pl > :global(.st-lg:first-child) {
		margin-top: 4px;
	}
	.pg {
		position: relative;
	}
	.pg + .pg {
		margin-top: 16px;
	}
	.pl > :global(.st-lg:not(:first-child)) {
		padding-top: 32px;
	}
	/* Hairline between every row; the card's own edge ends the list. */
	.pg > :global(* + *) {
		border-top: 1px solid var(--st-border);
	}
</style>
