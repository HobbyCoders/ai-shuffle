<script lang="ts">
	/**
	 * A connected provider's quiet label: its name (opens the provider), then
	 * Claude's "Subscription | API key | Cloud | Off" select and "Add account".
	 */
	import { settingsNav } from '$lib/settings/navigation.svelte';
	import { providers } from '$lib/stores/providers.svelte';
	import type { ClaudeAuthMode } from '$lib/api/claude-auth';
	import { moveListFocus } from '../rows/ListItem.svelte';
	import SelectField from '../rows/SelectField.svelte';
	import AddKindMenu from './AddKindMenu.svelte';
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

<div class="pgh" aria-current={selected ? 'true' : undefined}>
	<button
		type="button"
		class="pgn"
		data-settings-item={provider.item}
		data-settings-nav-item
		onclick={() => settingsNav.selectItem(provider.item, 'providers')}
		onkeydown={moveListFocus}
	>
		<span class="pname">{provider.name}</span>
	</button>
	<span class="pgr">
		{#if provider.pending}
			<span class="cnt checking" role="status">Checking…</span>
		{:else if provider.claude}
			<SelectField
				label="Claude runs on"
				width={150}
				options={MODE_OPTIONS}
				value={mode}
				disabled={!providers.current.claude}
				onchange={(v) => pickClaudeMode(provider, v as ClaudeAuthMode)}
			/>
			<AddKindMenu providerName={provider.name} {kinds} onpick={(k) => startSetup(provider, k.flavor)} />
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
		min-height: 40px;
		padding: 4px 8px 4px 16px;
	}
	.pgh[aria-current='true'] {
		background: var(--st-accent);
	}
	.pgn {
		min-width: 0;
		height: 32px;
		padding: 0;
		border: 0;
		background: none;
		text-align: left;
		color: var(--st-mfg);
		border-radius: var(--st-radius-sm);
	}
	.pgn:hover {
		color: var(--st-fg);
	}
	.pname {
		display: block;
		font-weight: 500;
		font-size: 12px;
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
