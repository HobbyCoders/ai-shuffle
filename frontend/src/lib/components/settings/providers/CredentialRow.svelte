<script lang="ts">
	/**
	 * One connected credential in its provider's card. The switch (or "Use" on
	 * saved Claude accounts) is the row's one on/off control, so the status dot
	 * only appears when there is no control or something needs attention.
	 */
	import { settingsNav } from '$lib/settings/navigation.svelte';
	import { moveListFocus } from '../rows/ListItem.svelte';
	import StatusDot from '../rows/StatusDot.svelte';
	import CredentialControl from './CredentialControl.svelte';
	import type { CredentialView } from './providers-model';

	interface Props {
		credential: CredentialView;
	}

	let { credential }: Props = $props();

	const selected = $derived(settingsNav.selectedItem('providers') === credential.item);
	const showDot = $derived(
		!credential.switchable || credential.status === 'warn' || credential.status === 'err' || credential.status === 'busy'
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
	{#if credential.switchable}
		<!-- svelte-ignore a11y_no_static_element_interactions -->
		<span class="sw" onclick={(e) => e.stopPropagation()}>
			{#if showDot}<StatusDot tone={credential.status} />{/if}
			<CredentialControl {credential} />
		</span>
	{:else}
		<span class="sw">{#if showDot}<StatusDot tone={credential.status} />{/if}</span>
	{/if}
</div>

<style>
	.cred {
		display: grid;
		grid-template-columns: minmax(0, 1fr) auto;
		align-items: center;
		gap: 12px;
		min-height: 56px;
		padding: 0 16px 0 48px;
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
		gap: 10px;
	}
</style>
