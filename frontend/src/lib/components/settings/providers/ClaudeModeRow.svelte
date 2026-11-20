<script lang="ts">
	/**
	 * What Claude runs on (Subscription, API key, Cloud, Off), on Claude's detail
	 * page whether or not anything is connected, so it stays reachable in every
	 * state. Separate from choosing among saved keys and sign-ins: "Use" on a saved
	 * account never changes this. "Cloud" opens its page (the warning is there) and
	 * the control stays on the current mode until that finishes.
	 */
	import type { ClaudeAuthMode } from '$lib/api/claude-auth';
	import { getErrorMessage, isApiError } from '$lib/api/client';
	import { flashSaved } from '$lib/settings/save-status.svelte';
	import { getSettingsAccess } from '$lib/settings/access.svelte';
	import { providers } from '$lib/stores/providers.svelte';
	import RowGroup from '../rows/RowGroup.svelte';
	import Segmented from '../rows/Segmented.svelte';
	import SettingRow from '../rows/SettingRow.svelte';
	import { pickClaudeMode } from './provider-actions';
	import { MODE_OPTIONS, type ProviderView } from './providers-model';

	let { provider }: { provider: ProviderView } = $props();

	const access = getSettingsAccess();
	const claude = $derived(providers.current.claude);
	const mode = $derived<ClaudeAuthMode>(claude?.mode ?? 'subscription');
	let confirming = $state(false);
	let confirmationError = $state('');
	let rejectedRevision = $state<string | null>(null);

	async function refreshSelection() {
		if (await providers.loadClaude()) rejectedRevision = null;
	}

	async function confirmApiKey(revision: string) {
		if (access.readOnly || confirming || revision === rejectedRevision) return;
		confirming = true;
		confirmationError = '';
		try {
			await providers.confirmClaudeApiKey(revision);
			flashSaved();
		} catch (e) {
			confirmationError = getErrorMessage(e, 'Could not confirm the API key');
			if (isApiError(e) && e.status === 409) {
				rejectedRevision = revision;
				await refreshSelection();
			}
		} finally {
			confirming = false;
		}
	}
</script>

<RowGroup title="Claude Code mode">
	<SettingRow id="providers.claudeMode" hint="Subscription and API key choices sync. Update all workers before relying on a switch: older workers may keep using their previous account. Cloud and Off apply only here.">
		<Segmented options={MODE_OPTIONS} value={mode} disabled={!claude || access.readOnly || confirming} onchange={(v: ClaudeAuthMode) => pickClaudeMode(provider, v)} />
	</SettingRow>
	{#if mode === 'api_key' && claude?.api_key_confirmation_required && !access.readOnly}
		<SettingRow
			label="Confirm your saved API key"
			hint={`Confirm ${claude.api_key.last4 ? `the key ending in ${claude.api_key.last4}` : 'the saved key'} before choosing a subscription sign-in. Claude Code stays in API key mode.`}
			wrapHint
		>
			{@const revision = claude.api_key_confirmation}
			{#if revision && revision !== rejectedRevision}
				{#key revision}
					<button type="button" class="st-btn" disabled={confirming} onclick={() => confirmApiKey(revision)}>
						{confirming ? 'Confirming…' : 'Confirm API key'}
					</button>
				{/key}
			{:else if revision}
				<button type="button" class="st-btn" disabled={confirming} onclick={refreshSelection}>
					Refresh selection
				</button>
			{:else}
				<span>Update this worker to confirm the saved key.</span>
			{/if}
		</SettingRow>
	{/if}
	{#if confirmationError}<p class="pv-msg err" role="alert">{confirmationError}</p>{/if}
</RowGroup>
