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
	<span class="tile"><ProviderTile {provider} dashed /></span>
	<span class="nm">{provider.name}</span>
	<span class="sm">{kinds}</span>
</button>

<style>
	.nc {
		display: grid;
		grid-template-columns: auto auto minmax(0, 1fr);
		align-items: center;
		gap: 10px;
		width: 100%;
		min-height: 40px;
		padding: 4px 10px 4px 14px;
		border: 0;
		border-radius: var(--st-radius-md);
		background: none;
		text-align: left;
	}
	.nc:hover {
		background: var(--st-hover);
	}
	.nc[aria-current='true'] {
		background: var(--st-accent);
	}
	.nc:focus-visible {
		outline-offset: -2px;
	}
	.tile :global(.st-tile) {
		width: 22px;
		height: 22px;
		font-size: 11px;
		color: var(--st-mfg);
	}
	.tile :global(svg) {
		width: 12px;
		height: 12px;
	}
	.tile {
		display: flex;
	}
	.nm {
		font-size: 13.5px;
		font-weight: 450;
		color: var(--st-mfg);
		white-space: nowrap;
	}
	.sm {
		font-size: 11.5px;
		color: var(--st-subtle);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
</style>
