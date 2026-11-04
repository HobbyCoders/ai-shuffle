<script lang="ts">
	/**
	 * A connected provider's first row in its card: mark and name (opens the
	 * provider), then "Add account" at the right where there is one to add.
	 */
	import { settingsNav } from '$lib/settings/navigation.svelte';
	import { moveListFocus } from '../rows/ListItem.svelte';
	import AddKindMenu from './AddKindMenu.svelte';
	import ProviderTile from './ProviderTile.svelte';
	import { addableKinds, type ProviderView } from './providers-model';
	import { startSetup } from './provider-actions';

	interface Props {
		provider: ProviderView;
	}

	let { provider }: Props = $props();

	const selected = $derived(settingsNav.selectedItem('providers') === provider.item);
	const kinds = $derived(addableKinds(provider));
</script>

<div class="pgh" aria-current={selected ? 'true' : undefined}>
	<button
		type="button"
		class="pgn"
		data-settings-item={provider.item}
		data-settings-nav-item
		onclick={() => settingsNav.selectItem(provider.item, 'providers')}
		onkeydown={moveListFocus}
	>
		<ProviderTile {provider} bare />
		<span class="pname">{provider.name}</span>
	</button>
	<span class="pgr">
		{#if provider.pending}
			<span class="cnt checking" role="status">Checking…</span>
		{:else}
			<AddKindMenu
				providerName={provider.name}
				{kinds}
				disabled={provider.draft}
				onpick={(k) => startSetup(provider, k.flavor)}
			/>
		{/if}
	</span>
</div>

<style>
	.pgh {
		display: flex;
		align-items: center;
		justify-content: space-between;
		min-height: 44px;
	}
	.pgh:has(.pgn:hover) {
		background: var(--st-hover);
	}
	.pgh[aria-current='true'] {
		background: var(--st-accent);
	}
	.pgn {
		display: flex;
		flex: 1;
		align-items: center;
		align-self: stretch;
		gap: var(--pv-glyph-gap);
		min-width: 0;
		padding: 0 8px 0 var(--pv-row-inset);
		border: 0;
		background: none;
		text-align: left;
		color: var(--st-fg);
		border-radius: inherit;
	}
	.pgn:focus-visible {
		outline-offset: -2px;
	}
	.pname {
		display: block;
		font-weight: 500;
		font-size: 13.5px;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	/* "Add account" ends on the chevrons' edge, past its own 8px of padding. */
	.pgr {
		display: flex;
		align-items: center;
		gap: 4px;
		flex: none;
		min-width: 0;
		padding-right: calc(var(--pv-row-inset) - 8px);
	}
	.cnt {
		padding-right: 8px;
		font-size: 12px;
		color: var(--st-subtle);
		white-space: nowrap;
	}
	.checking {
		animation: pgh-pulse 1.4s ease-in-out infinite;
	}
	@keyframes pgh-pulse {
		50% {
			opacity: 0.45;
		}
	}
	@media (prefers-reduced-motion: reduce) {
		.checking {
			animation: none;
		}
	}
</style>
