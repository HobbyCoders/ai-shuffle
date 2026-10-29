<script lang="ts">
	/** One saved Anthropic key. Account selection never changes the Claude Code mode. */
	import { getErrorMessage } from '$lib/api/client';
	import { providers } from '$lib/stores/providers.svelte';
	import { settingsNav } from '$lib/settings/navigation.svelte';
	import { flashSaved } from '$lib/settings/save-status.svelte';
	import DetailPane from '../shell/DetailPane.svelte';
	import type { DangerAction } from '../shell/DangerZone.svelte';
	import RowGroup from '../rows/RowGroup.svelte';
	import SettingRow from '../rows/SettingRow.svelte';
	import ValueText from '../rows/ValueText.svelte';
	import TextField from '../rows/TextField.svelte';
	import ProviderTile from './ProviderTile.svelte';
	import CredentialControl from './CredentialControl.svelte';
	import BuiltInModels from './tabs/BuiltInModels.svelte';
	import type { CredentialView, ProviderView } from './providers-model';
	import { changeClaudeMode, startSetup } from './provider-actions';

	let { credential, provider }: { credential: CredentialView; provider: ProviderView } = $props();
	const account = $derived(credential.account);
	const mode = $derived(providers.current.claude?.mode ?? 'subscription');
	const masked = $derived(account?.masked_key || providers.current.anthropicKeyMasked || 'No key selected');
	let saving = $state(false);
	let error = $state('');

	async function rename(label: string) {
		if (!account || label.trim() === account.label || saving) return;
		saving = true;
		error = '';
		try {
			await providers.renameAnthropicAccount(account.id, label);
			flashSaved();
		} catch (e) {
			error = getErrorMessage(e, 'Could not rename this account');
		} finally { saving = false; }
	}

	async function remove() {
		const back = provider.item;
		error = '';
		try {
			if (account) await providers.deleteAnthropicAccount(account.id);
			else { await providers.clearAnthropicKey(); await providers.loadClaude(); }
			settingsNav.selectItem(back, 'providers');
		} catch (e) { error = getErrorMessage(e, 'Could not remove this account'); }
	}

	const danger = $derived<DangerAction[]>(account || providers.current.anthropicKeySet ? [{
		label: 'Remove this credential',
		hint: account?.active ? 'Leaves API key mode with no selected key; other saved keys are kept' : 'Deletes only this saved key',
		button: 'Remove',
		question: `Remove ${credential.name}?`,
		onConfirm: remove
	}] : []);
</script>

<DetailPane name={credential.name} badge="API key" status={credential.status} statusText={credential.statusText}
	subtitle={credential.on ? 'Runs every Claude session in API key mode' : account?.active ? 'Selected for API key mode' : 'Saved Anthropic account'}
	facts={[{ label: 'Key', value: masked, mono: true }, { label: 'Billing', value: 'Per use' }, { label: 'Models', value: 'Built in' }]}
	tabs={['Overview', 'Models']} {danger} item={credential.item}>
	{#snippet tile()}<ProviderTile {provider} size="lg" />{/snippet}
	{#snippet primary()}
		{#if account}<CredentialControl {credential} />{/if}
		{#if (account?.active || !account && providers.current.anthropicKeySet) && mode !== 'api_key'}
			<button type="button" class="st-btn" onclick={() => changeClaudeMode('api_key')}>Use API key mode</button>
		{/if}
	{/snippet}
	{#snippet children(tab: string | null)}
		{#if tab === 'Models'}
			<BuiltInModels />
		{:else}
			<RowGroup title="Account">
				{#if account}
					<SettingRow label="Name"><TextField value={account.label} width={220} maxlength={120} disabled={saving} onchange={rename} /></SettingRow>
				{/if}
				<SettingRow id="providers.anthropicKey" label="Key"><ValueText mono tone="fg" value={masked} /></SettingRow>
				<SettingRow label="Account selection" hint="Changing the selected key keeps the Claude Code mode unchanged">
					<ValueText value={credential.statusText} status={credential.status} />
				</SettingRow>
				<SettingRow label="Another API key" hint="The new key becomes selected">
					<button type="button" class="st-btn" onclick={() => startSetup(provider, 'anthropic')}>Add key</button>
				</SettingRow>
			</RowGroup>
			{#if error}<p class="pv-msg err" role="alert">{error}</p>{/if}
			<RowGroup title="Billing">
				<SettingRow label="Billed"><ValueText tone="fg" value="Per use, Anthropic Console" /></SettingRow>
				<SettingRow label="Spend limit" hint="Loop guard is off here"><ValueText value="Set in Anthropic Console" /></SettingRow>
			</RowGroup>
		{/if}
	{/snippet}
</DetailPane>
