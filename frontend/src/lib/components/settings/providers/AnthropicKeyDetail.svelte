<script lang="ts">
	/**
	 * The Anthropic API key: Claude Code authenticates with it in API key mode.
	 * Billed per use by Anthropic; the loop guard does not apply.
	 */
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
	import BuiltInModels from './tabs/BuiltInModels.svelte';
	import { CLAUDE_KEY_ITEM, type CredentialView, type ProviderView } from './providers-model';
	import { changeClaudeMode } from './provider-actions';

	interface Props {
		credential: CredentialView;
		provider: ProviderView;
	}

	let { credential, provider }: Props = $props();

	const mode = $derived(providers.current.claude?.mode ?? 'subscription');
	const keySet = $derived(!!providers.current.claude?.api_key.set || providers.current.anthropicKeySet);
	const masked = $derived(
		providers.current.anthropicKeyMasked ||
			(providers.current.claude?.api_key.last4 ? `sk-ant-…${providers.current.claude.api_key.last4}` : '')
	);
	const position = $derived(
		mode === 'off'
			? 'Claude is off'
			: mode === 'api_key'
				? keySet
					? 'Runs every Claude session'
					: 'Selected, not ready'
				: 'Standby for Claude'
	);

	let replacing = $state(false);
	let key = $state('');
	let saving = $state(false);
	let error = $state('');

	async function save() {
		if (!key.trim() || saving) return;
		saving = true;
		error = '';
		try {
			await providers.setAnthropicKey(key);
			await providers.loadClaude();
			key = '';
			replacing = false;
			flashSaved();
		} catch (e) {
			error = e instanceof Error ? e.message : 'Save failed';
		} finally {
			saving = false;
		}
	}

	async function remove() {
		const back = provider.item;
		try {
			await providers.clearAnthropicKey();
			await providers.loadClaude();
			settingsNav.selectItem(back, 'providers');
		} catch (e) {
			error = getErrorMessage(e, 'Remove failed');
		}
	}

	const danger = $derived<DangerAction[]>(
		keySet
			? [
					{
						label: 'Remove this credential',
						hint: 'Deletes the stored key',
						button: 'Remove',
						question: 'Remove the Anthropic key?',
						onConfirm: remove
					}
				]
			: []
	);
</script>

<DetailPane
	name={credential.name}
	badge="API key"
	status={credential.status}
	statusText={credential.statusText}
	subtitle={position}
	facts={[
		{ label: 'Key', value: masked || null, mono: true },
		{ label: 'Billing', value: 'Per use' },
		{ label: 'Models', value: 'Built in' }
	]}
	tabs={['Overview', 'Models']}
	{danger}
	item={CLAUDE_KEY_ITEM}
>
	{#snippet tile()}<ProviderTile {provider} size="lg" />{/snippet}
	{#snippet primary()}
		{#if mode !== 'api_key' && keySet}
			<button type="button" class="st-btn" onclick={() => changeClaudeMode('api_key')}>Use for Claude</button>
		{/if}
	{/snippet}
	{#snippet children(tab: string | null)}
		{#if tab === 'Models'}
			<BuiltInModels />
		{:else}
			<RowGroup title="Key">
				<SettingRow label="Key">
					<ValueText mono tone="fg" value={masked || (keySet ? 'Key stored' : 'No key stored')} />
				</SettingRow>
				<SettingRow id="providers.anthropicKey" label={replacing || !keySet ? 'New key' : undefined}>
					{#if replacing || !keySet}
						<TextField
							bind:value={key}
							type="password"
							mono
							placeholder="sk-ant-…"
							width={200}
							disabled={saving}
							label="New Anthropic key"
						/>
						<button type="button" class="st-btn primary" disabled={saving || !key.trim()} onclick={save}>Save</button>
						{#if keySet}
							<button
								type="button"
								class="st-btn ghost"
								disabled={saving}
								onclick={() => ((replacing = false), (key = ''))}>Cancel</button
							>
						{/if}
					{:else}
						<button type="button" class="st-btn" onclick={() => (replacing = true)}>Replace</button>
					{/if}
				</SettingRow>
			</RowGroup>
			{#if error}<p class="pv-msg err" role="alert">{error}</p>{/if}
			<RowGroup title="Billing">
				<SettingRow label="Billed"><ValueText tone="fg" value="Per use, Anthropic Console" /></SettingRow>
				<SettingRow label="Spend limit" hint="Loop guard is off here"
					><ValueText value="Set in Anthropic Console" /></SettingRow
				>
			</RowGroup>
		{/if}
	{/snippet}
</DetailPane>
