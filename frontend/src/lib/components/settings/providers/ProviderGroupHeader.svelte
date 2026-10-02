<script lang="ts">
	/**
	 * A connected provider's header: tile and name (opens the provider), then
	 * Claude's "Subscription | API key | Off" or the "N on" count with the "+".
	 * The connector line starts under the tile.
	 */
	import { settingsNav } from '$lib/settings/navigation.svelte';
	import { providers } from '$lib/stores/providers.svelte';
	import type { ClaudeAuthMode } from '$lib/api/claude-auth';
	import { moveListFocus } from '../rows/ListItem.svelte';
	import Segmented from '../rows/Segmented.svelte';
	import AddKindMenu from './AddKindMenu.svelte';
	import ProviderTile from './ProviderTile.svelte';
	import { addableKinds, MODE_OPTIONS, type ProviderView } from './providers-model';
	import { changeClaudeMode, startSetup } from './provider-actions';

	interface Props {
		provider: ProviderView;
		/** The stub under the tile leads into a credential that is on. */
		lit: boolean;
	}

	let { provider, lit }: Props = $props();

	const selected = $derived(settingsNav.selectedItem('providers') === provider.item);
	const kinds = $derived(addableKinds(provider));
	const mode = $derived(providers.current.claude?.mode ?? 'subscription');
</script>

<div class="pgh" class:lit class:bare={provider.credentials.length === 0} aria-current={selected ? 'true' : undefined}>
	<button
		type="button"
		class="pgn"
		data-settings-item={provider.item}
		data-settings-nav-item
		onclick={() => settingsNav.selectItem(provider.item, 'providers')}
		onkeydown={moveListFocus}
	>
		<ProviderTile {provider} ring={selected ? 'accent' : 'bg'} />
		<span class="pname">{provider.name}</span>
	</button>
	{#if provider.claude}
		<span class="mode">
			<Segmented
				size="sm"
				label="Claude runs on"
				options={MODE_OPTIONS}
				value={mode}
				disabled={!providers.current.claude}
				onchange={(v: ClaudeAuthMode) => changeClaudeMode(v)}
			/>
		</span>
		<span></span>
	{:else}
		<span class="cnt">{provider.draft ? 'Setting up' : `${provider.onCount} on`}</span>
		<AddKindMenu
			providerName={provider.name}
			{kinds}
			disabled={provider.draft}
			onpick={(k) => startSetup(provider, k.flavor)}
		/>
	{/if}
</div>

<style>
	.pgh {
		position: relative;
		display: grid;
		grid-template-columns: minmax(0, auto) minmax(0, 1fr) auto;
		align-items: center;
		gap: 8px;
		height: 44px;
		padding-right: 6px;
		margin-top: 8px;
		border-radius: var(--st-radius-md);
	}
	.pgh[aria-current='true'] {
		background: var(--st-accent);
	}
	.pgh:hover:not([aria-current='true']) {
		background: var(--st-hover);
	}
	/* Connector stub from under the tile to the first credential. */
	.pgh::after {
		content: '';
		position: absolute;
		left: 25px;
		top: 35px;
		bottom: 0;
		width: var(--st-line);
		margin-left: calc(var(--st-line) / -2);
		background: var(--st-border-strong);
		transition: background 0.25s;
	}
	.pgh.lit::after {
		background: var(--st-primary);
	}
	.pgh.bare::after {
		display: none;
	}
	.pgn {
		display: flex;
		align-items: center;
		gap: 10px;
		height: 44px;
		padding: 0 4px 0 12px;
		border: 0;
		background: none;
		min-width: 0;
		text-align: left;
		border-radius: var(--st-radius-md);
		position: relative;
		z-index: 1;
	}
	.pgn:focus-visible {
		outline-offset: -2px;
	}
	.pname {
		font-weight: 600;
		font-size: 13.5px;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.mode {
		justify-self: end;
		display: flex;
		min-width: 0;
	}
	.cnt {
		font-size: 12px;
		color: var(--st-subtle);
		text-align: right;
		white-space: nowrap;
	}
	@media (prefers-reduced-motion: reduce) {
		.pgh::after {
			transition: none;
		}
	}
</style>
