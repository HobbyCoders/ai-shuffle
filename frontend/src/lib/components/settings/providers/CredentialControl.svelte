<script lang="ts">
	/** Shared account action: built-in switches select their slot; Claude keys keep the mode unchanged. */
	import { providers } from '$lib/stores/providers.svelte';
	import { getSettingsAccess } from '$lib/settings/access.svelte';
	import Switch from '../rows/Switch.svelte';
	import type { CredentialView } from './providers-model';
	import { setCredentialEnabled } from './provider-actions';

	let { credential, size = 'md', showState = false }: {
		credential: CredentialView;
		size?: 'sm' | 'md';
		showState?: boolean;
	} = $props();
	const access = getSettingsAccess();
	const busy = $derived(!!providers.current.togglingProviderId || !!providers.current.claudeAccountBusy);
	const saving = $derived(providers.current.togglingProviderId === credential.row?.id ||
		(!!credential.account && providers.current.claudeAccountBusy === credential.account.id));
</script>

<span class="control" aria-busy={saving}>
	{#if credential.account}
		{#if credential.account.active}
			<span class="state">{credential.on ? 'Active' : 'Selected'}</span>
		{:else}
			<button type="button" class="st-btn ghost sm" disabled={busy || access.readOnly}
				aria-label="Use {credential.name}"
				onclick={() => setCredentialEnabled(credential, true)}>{saving ? 'Switching…' : 'Use'}</button>
		{/if}
	{:else}
		{#if showState || saving}<span class="state">{saving ? 'Saving…' : credential.active ? 'Active' : credential.enabled ? 'On' : 'Off'}</span>{/if}
		<Switch {size} checked={credential.enabled} disabled={busy}
			label={credential.enabled ? `${credential.name} on` : `Use ${credential.name}`}
			onchange={(v) => setCredentialEnabled(credential, v)} />
	{/if}
</span>

<style>
	.control { display: inline-flex; align-items: center; gap: 10px; flex: none; }
	.state { font-size: 12px; color: var(--st-mfg); white-space: nowrap; }
	@media (pointer: coarse) { .control { min-height: 44px; } .control :global(button) { min-height: 44px; min-width: 44px; } }
</style>
