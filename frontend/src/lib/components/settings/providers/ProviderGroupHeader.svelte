<script lang="ts">
	/**
	 * A connected provider's header: tile and name (opens the provider), then
	 * Claude's "Subscription | API key | Cloud | Off" and the "+".
	 */
	import { settingsNav } from '$lib/settings/navigation.svelte';
	import { providers } from '$lib/stores/providers.svelte';
	import type { ClaudeAuthMode } from '$lib/api/claude-auth';
	import { moveListFocus } from '../rows/ListItem.svelte';
	import Segmented from '../rows/Segmented.svelte';
	import AddKindMenu from './AddKindMenu.svelte';
	import ProviderTile from './ProviderTile.svelte';
	import { addableKinds, MODE_OPTIONS, type ProviderView } from './providers-model';
	import { pickClaudeMode, startSetup } from './provider-actions';

	interface Props {
		provider: ProviderView;
	}

	let { provider }: Props = $props();

	const selected = $derived(settingsNav.selectedItem('providers') === provider.item);
	const kinds = $derived(addableKinds(provider));
	const mode = $derived(providers.current.claude?.mode ?? 'subscription');
</script>

<div class="pgh" class:claude={!!provider.claude} aria-current={selected ? 'true' : undefined}>
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
	{#if provider.pending}
		<span class="cnt checking" role="status">Checking…</span>
	{:else if provider.claude}
		<span class="mode">
			<Segmented
				size="sm"
				label="Claude runs on"
				options={MODE_OPTIONS}
				value={mode}
				disabled={!providers.current.claude}
				onchange={(v: ClaudeAuthMode) => pickClaudeMode(provider, v)}
			/>
		</span>
		<AddKindMenu providerName={provider.name} {kinds} onpick={(k) => startSetup(provider, k.flavor)} />
	{:else}
		<span class="cnt">{provider.draft ? 'Setting up' : ''}</span>
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
		min-height: 48px;
		padding-right: 8px;
	}
	.pgh[aria-current='true'] {
		background: var(--st-accent);
	}
	.pgh:hover:not([aria-current='true']) {
		background: var(--st-hover);
	}
	.pgn {
		display: flex;
		align-items: center;
		gap: 10px;
		height: 48px;
		padding: 0 4px 0 12px;
		border: 0;
		background: none;
		min-width: 0;
		text-align: left;
		border-radius: var(--st-radius-md);
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
	/* Claude: the name gives way before the mode switch does, so the switch
	   never has to scroll. Three columns (name, mode, "+"), so the "+" stays on
	   the header line instead of wrapping under it onto the first credential. */
	.pgh.claude {
		grid-template-columns: minmax(0, 1fr) auto auto;
	}
	.mode {
		justify-self: end;
		display: flex;
		align-items: center;
		min-width: 0;
	}
	/* A list up to phone width (280-420px) cannot hold the name, the mode
	   switch and the "+" on one line: the switch moves under the name. */
	@container st-list (max-width: 419px) {
		.pgh.claude:has(.mode) {
			grid-template-columns: minmax(0, 1fr) auto;
			height: auto;
			padding-bottom: 8px;
		}
		.pgh.claude .pgn { grid-column: 1; }
		.pgh.claude :global(.plus) { grid-column: 2; grid-row: 1; }
		.pgh.claude .mode {
			grid-column: 1 / -1;
			justify-self: start;
			padding-left: 12px;
		}
	}
	.cnt {
		font-size: 12px;
		color: var(--st-subtle);
		text-align: right;
		white-space: nowrap;
	}
	.checking {
		justify-self: end;
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
