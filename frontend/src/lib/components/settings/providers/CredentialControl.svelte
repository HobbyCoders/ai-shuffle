<script lang="ts">
	/**
	 * Shared account action: built-in switches select their slot; saved Claude
	 * keys and sign-ins show "Use" (one is always the selected one) and keep
	 * the Claude Code mode unchanged.
	 */
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
	const saved = $derived(credential.account ?? credential.subscription);
	const saving = $derived(providers.current.togglingProviderId === credential.row?.id ||
		(!!saved && providers.current.claudeAccountBusy === saved.id));
</script>

<span class="control" aria-busy={saving}>
	{#if saved}
		{#if saved.active}
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
	/* Touch target for the "Use" button only: forcing 44px on the Switch too
	   squashed its 36x21 pill into a 44x44 circle on phones. */
	@media (pointer: coarse) { .control { min-height: 44px; } .control :global(button:not([role='switch'])) { min-height: 44px; min-width: 44px; } }
</style>
