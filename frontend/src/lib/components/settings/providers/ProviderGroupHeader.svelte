<script lang="ts">
	/**
	 * A connected provider's quiet label, outside its card: mark and name (opens
	 * the provider), then "Add account" at the right.
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
		<ProviderTile {provider} />
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
		gap: 8px;
		min-height: 32px;
		margin-bottom: 6px;
	}
	.pgn {
		display: inline-flex;
		align-items: center;
		gap: 8px;
		min-width: 0;
		height: 32px;
		padding: 0;
		border: 0;
		background: none;
		text-align: left;
		color: var(--st-fg);
		border-radius: var(--st-radius-sm);
	}
	.pgh[aria-current='true'] .pgn,
	.pgn:hover {
		text-decoration: underline;
		text-underline-offset: 3px;
	}
	.pname {
		display: block;
		font-weight: 500;
		font-size: 13px;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.pgr {
		display: flex;
		align-items: center;
		gap: 4px;
		flex: none;
		min-width: 0;
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
	@media (pointer: coarse) {
		.pgn {
			min-height: 44px;
		}
	}
</style>
