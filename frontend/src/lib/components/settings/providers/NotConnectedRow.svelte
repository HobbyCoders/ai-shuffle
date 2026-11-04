<script lang="ts">
	/** A provider with nothing connected: compact row in the "Not connected" group. */
	import { settingsNav } from '$lib/settings/navigation.svelte';
	import { moveListFocus } from '../rows/ListItem.svelte';
	import ProviderTile from './ProviderTile.svelte';
	import { kindsOf, type ProviderView } from './providers-model';

	interface Props {
		provider: ProviderView;
	}

	let { provider }: Props = $props();
	const selected = $derived(settingsNav.selectedItem('providers') === provider.item);
	const kinds = $derived(
		kindsOf(provider.id)
			.map((k) => k.label)
			.join(', ')
	);
</script>

<button
	type="button"
	class="nc"
	aria-current={selected ? 'true' : undefined}
	data-settings-item={provider.item}
	data-settings-nav-item
	onclick={() => settingsNav.selectItem(provider.item, 'providers')}
	onkeydown={moveListFocus}
>
	<span class="tile"><ProviderTile {provider} /></span>
	<span class="tx">
		<span class="nm">{provider.name}</span>
		<span class="sm">{kinds}</span>
	</span>
	<span class="st-btn sm connect" aria-hidden="true">Connect</span>
</button>

<style>
	.nc {
		display: grid;
		grid-template-columns: auto minmax(0, 1fr) auto;
		align-items: center;
		gap: 10px;
		width: 100%;
		min-height: 56px;
		padding: 6px 12px;
		border: 0;
		background: none;
		text-align: left;
	}
	.nc:hover {
		background: var(--st-hover);
	}
	.nc:hover .connect {
		color: var(--st-fg);
		border-color: var(--st-mfg);
	}
	.nc[aria-current='true'] {
		background: var(--st-accent);
	}
	.nc:focus-visible {
		outline-offset: -2px;
	}
	.tile {
		display: flex;
	}
	.tx {
		display: grid;
		gap: 1px;
		min-width: 0;
	}
	.nm {
		font-size: 13.5px;
		font-weight: 500;
		white-space: nowrap;
	}
	.sm {
		font-size: 13px;
		color: var(--st-mfg);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
</style>
