<script lang="ts">
	/**
	 * The Providers list: each connected provider with the credentials it has,
	 * on the connector line, then the providers with nothing connected.
	 */
	import ListGroupLabel from '../rows/ListGroupLabel.svelte';
	import CredentialRow from './CredentialRow.svelte';
	import NotConnectedRow from './NotConnectedRow.svelte';
	import ProviderGroupHeader from './ProviderGroupHeader.svelte';
	import { filterProviders, lineState, splitProviders, type ProviderView } from './providers-model';

	interface Props {
		views: ProviderView[];
		filter?: string;
	}

	let { views, filter = '' }: Props = $props();

	const groups = $derived(splitProviders(filterProviders(views, filter)));
</script>

{#each groups.connected as provider (provider.id)}
	{@const line = lineState(provider.credentials)}
	<div class="pg" role="group" aria-label={provider.name}>
		<ProviderGroupHeader {provider} lit={line.lit} />
		{#each provider.credentials as credential, i (credential.item)}
			<CredentialRow {credential} on={line.rows[i].on} nextOn={line.rows[i].nextOn} last={line.rows[i].last} />
		{/each}
	</div>
{/each}

{#if groups.notConnected.length}
	<ListGroupLabel label="Not connected" />
	{#each groups.notConnected as provider (provider.id)}
		<NotConnectedRow {provider} />
	{/each}
{/if}

{#if !groups.connected.length && !groups.notConnected.length}
	<p class="pv-empty">Nothing matches.</p>
{/if}

<style>
	.pg {
		position: relative;
	}
</style>
