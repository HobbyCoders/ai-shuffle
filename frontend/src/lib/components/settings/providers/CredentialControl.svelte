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
	/** A failed switch says why right here, not only in the bell. */
	let error = $state('');
	async function setEnabled(enabled: boolean) {
		error = '';
		error = (await setCredentialEnabled(credential, enabled)) ?? '';
	}
</script>

<span class="wrap">
<span class="control" aria-busy={saving}>
	{#if saved}
		{#if saved.active}
			<span class="state">{credential.on ? 'Active' : 'Selected'}</span>
		{:else}
			<button type="button" class="st-btn ghost sm" disabled={busy || access.readOnly}
				aria-label="Use {credential.name}"
				onclick={() => setEnabled(true)}>{saving ? 'Switching…' : 'Use'}</button>
		{/if}
	{:else}
		{#if showState || saving}<span class="state">{saving ? 'Saving…' : credential.active ? 'Active' : credential.enabled ? 'On' : 'Off'}</span>{/if}
		<Switch {size} checked={credential.enabled} disabled={busy}
			label={credential.enabled ? `${credential.name} on` : `Use ${credential.name}`}
			onchange={(v) => setEnabled(v)} />
	{/if}
</span>
{#if error}<span class="err" role="alert">{error}</span>{/if}
</span>

<style>
	/* A column: a failed switch's message goes under the controls without widening them. */
	.wrap { display: inline-flex; flex-direction: column; align-items: flex-end; gap: 4px; flex: none; max-width: 100%; }
	.control { display: inline-flex; align-items: center; gap: 10px; flex: none; }
	/* Capped: on phone the control sits in the More menu, which sizes to its content. */
	.err { max-width: min(18rem, 70vw); text-align: right; font-size: 12px; color: var(--st-destructive); overflow-wrap: anywhere; }
	.state { font-size: 12px; color: var(--st-mfg); white-space: nowrap; }
	/* Touch target for the "Use" button only: forcing 44px on the Switch too
	   squashed its 36x21 pill into a 44x44 circle on phones. */
	@media (pointer: coarse) { .control { min-height: 44px; } .control :global(button:not([role='switch'])) { min-height: 44px; min-width: 44px; } }
</style>
