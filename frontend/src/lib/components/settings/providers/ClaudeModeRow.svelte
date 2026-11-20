<script lang="ts">
	/**
	 * What Claude runs on (Subscription, API key, Cloud, Off), on Claude's detail
	 * page whether or not anything is connected, so it stays reachable in every
	 * state. Separate from choosing among saved keys and sign-ins: "Use" on a saved
	 * account never changes this. "Cloud" opens its page (the warning is there) and
	 * the control stays on the current mode until that finishes.
	 */
	import type { ClaudeAuthMode } from '$lib/api/claude-auth';
	import { getErrorMessage } from '$lib/api/client';
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

	async function confirmApiKey() {
		if (access.readOnly || !claude?.api_key_confirmation_required || confirming) return;
		confirming = true;
		confirmationError = '';
		try {
			// Explicitly save the current mode to pin the legacy key. A same-mode
			// click on the segmented control intentionally does not save anything.
			await providers.setClaudeMode('api_key');
			flashSaved();
		} catch (e) {
			confirmationError = getErrorMessage(e, 'Could not confirm the API key');
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
			<button type="button" class="st-btn" disabled={confirming} onclick={confirmApiKey}>
				{confirming ? 'Confirming…' : 'Confirm API key'}
			</button>
		</SettingRow>
		{#if confirmationError}<p class="pv-msg err" role="alert">{confirmationError}</p>{/if}
	{/if}
</RowGroup>
