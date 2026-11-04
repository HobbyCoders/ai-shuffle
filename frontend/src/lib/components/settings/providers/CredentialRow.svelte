<script lang="ts">
	/**
	 * One connected credential: provider mark, name and summary, then one right
	 * column. The switch (or "Use" on saved Claude accounts) is the on/off control;
	 * a credential that cannot be toggled says its state in words. A status dot
	 * only appears when something needs attention.
	 */
	import { settingsNav } from '$lib/settings/navigation.svelte';
	import { moveListFocus } from '../rows/ListItem.svelte';
	import StatusDot from '../rows/StatusDot.svelte';
	import CredentialControl from './CredentialControl.svelte';
	import ProviderTile from './ProviderTile.svelte';
	import type { CredentialView, ProviderView } from './providers-model';

	interface Props {
		credential: CredentialView;
		provider: ProviderView;
	}

	let { credential, provider }: Props = $props();

	const selected = $derived(settingsNav.selectedItem('providers') === credential.item);
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
	<span class="mark"><ProviderTile {provider} /></span>
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
	{#if credential.switchable}
		<!-- svelte-ignore a11y_no_static_element_interactions -->
		<span class="sw" onclick={(e) => e.stopPropagation()}>
			{#if showDot}<StatusDot tone={credential.status} />{/if}
			<CredentialControl {credential} />
		</span>
	{:else}
		<span class="sw state">{#if showDot}<StatusDot tone={credential.status} />{/if}{stateText}</span>
	{/if}
</div>

<style>
	.cred {
		display: grid;
		grid-template-columns: auto minmax(0, 1fr) auto;
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
	.mark {
		display: flex;
	}
	.main {
		display: grid;
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
