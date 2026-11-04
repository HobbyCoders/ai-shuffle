<script lang="ts">
	/**
	 * One connected credential under its provider's label: name and summary, then
	 * one right column. The title area opens it; the switch (or "Use" on saved
	 * Claude accounts) is its own on/off control, and a credential that cannot be
	 * toggled says its state in words. A status dot only appears when something
	 * needs attention. Claude's first row also carries what Claude runs on.
	 */
	import { settingsNav } from '$lib/settings/navigation.svelte';
	import { ChevronRight } from 'lucide-svelte';
	import { moveListFocus } from '../rows/ListItem.svelte';
	import StatusDot from '../rows/StatusDot.svelte';
	import ClaudeModeMenu from './ClaudeModeMenu.svelte';
	import CredentialControl from './CredentialControl.svelte';
	import type { CredentialView, ProviderView } from './providers-model';

	interface Props {
		credential: CredentialView;
		provider: ProviderView;
	}

	let { credential, provider }: Props = $props();

	const selected = $derived(settingsNav.selectedItem('providers') === credential.item);
	const showMode = $derived(provider.claude && provider.credentials[0] === credential);
	const showDot = $derived(credential.status === 'warn' || credential.status === 'err' || credential.status === 'busy');
	const stateText = $derived(
		credential.status === 'ok' && (credential.flavor === 'cli' || credential.flavor === 'device') ? 'Signed in' : credential.statusText
	);

	function select() {
		settingsNav.selectItem(credential.item, 'providers');
	}
</script>

<!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_static_element_interactions -->
<div
	class="cred"
	aria-current={selected ? 'true' : undefined}
	data-settings-item={credential.item}
	onclick={select}
>
	<button
		type="button"
		class="main"
		aria-label="{credential.name}, {credential.statusText}"
		data-settings-nav-item
		onclick={(e) => {
			e.stopPropagation();
			select();
		}}
		onkeydown={moveListFocus}
	>
		<span class="nm">{credential.name}</span>
		<span class="sm">{credential.summary}</span>
	</button>
	{#if showMode}
		<!-- svelte-ignore a11y_no_static_element_interactions -->
		<span class="mode" onclick={(e) => e.stopPropagation()}><ClaudeModeMenu {provider} /></span>
	{/if}
	{#if credential.switchable}
		<!-- svelte-ignore a11y_no_static_element_interactions -->
		<span class="sw" onclick={(e) => e.stopPropagation()}>
			{#if showDot}<StatusDot tone={credential.status} />{/if}
			<CredentialControl {credential} />
		</span>
	{:else}
		<span class="sw state">{#if showDot}<StatusDot tone={credential.status} />{/if}{stateText}</span>
	{/if}
	<ChevronRight class="chev" size={16} aria-hidden="true" />
</div>

<style>
	.cred {
		display: flex;
		align-items: center;
		gap: 12px;
		min-height: 56px;
		padding: 6px 16px;
		cursor: pointer;
	}
	.cred:hover {
		background: var(--st-hover);
	}
	.cred[aria-current='true'] {
		background: var(--st-accent);
	}
	.main {
		display: grid;
		flex: 1;
		gap: 1px;
		min-width: 0;
		border: 0;
		background: none;
		padding: 4px 0;
		text-align: left;
		color: inherit;
		border-radius: var(--st-radius-sm);
	}
	.nm {
		font-size: 13.5px;
		font-weight: 450;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.sm {
		font-size: 13px;
		color: var(--st-mfg);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.mode {
		display: flex;
		flex: none;
	}
	.cred :global(.chev) {
		flex: none;
		color: var(--st-subtle);
	}
	/* A stacked pane is too narrow for all of it: what Claude runs on takes its
	   own line under the name. */
	:global(.settings-shell[data-stacked]) .cred:has(.mode) {
		flex-wrap: wrap;
		row-gap: 2px;
	}
	:global(.settings-shell[data-stacked]) .main {
		flex-basis: 0;
	}
	:global(.settings-shell[data-stacked]) .mode {
		order: 3;
		flex-basis: 100%;
		margin-left: -8px;
	}
	.sw {
		display: flex;
		align-items: center;
		justify-content: flex-end;
		gap: 10px;
	}
	.state {
		gap: 7px;
		font-size: 13px;
		color: var(--st-mfg);
		white-space: nowrap;
	}
</style>
