<script lang="ts">
	/**
	 * A ChatGPT or SuperGrok subscription (device-code sign-in). Overview has
	 * the sign-in state and Reconnect; Models the curation; Usage the weekly
	 * allowance and the banked resets.
	 */
	import { onMount, untrack } from 'svelte';
	import { RefreshCw } from 'lucide-svelte';
	import { getErrorMessage } from '$lib/api/client';
	import { providers } from '$lib/stores/providers.svelte';
	import { settingsNav } from '$lib/settings/navigation.svelte';
	import { flashSaved } from '$lib/settings/save-status.svelte';
	import { NATIVE_IMAGE_HELP } from '$lib/settings/entries/providers';
	import DetailPane from '../shell/DetailPane.svelte';
	import type { DangerAction } from '../shell/DangerZone.svelte';
	import RowGroup from '../rows/RowGroup.svelte';
	import SettingRow from '../rows/SettingRow.svelte';
	import ValueText from '../rows/ValueText.svelte';
	import TextField from '../rows/TextField.svelte';
	import ProviderTile from './ProviderTile.svelte';
	import ModelCuration from './ModelCuration.svelte';
	import DeviceCodeSetup from './setup/DeviceCodeSetup.svelte';
	import SubscriptionUsage from './tabs/SubscriptionUsage.svelte';
	import { BankedResets, isGrokSubscription } from './banked-resets.svelte';
	import { planLabel, supportsNativeImages, type CredentialView, type ProviderView } from './providers-model';
	import CredentialControl from './CredentialControl.svelte';

	interface Props {
		credential: CredentialView;
		provider: ProviderView;
	}

	let { credential, provider }: Props = $props();

	const row = $derived(credential.row!);
	const grok = $derived(isGrokSubscription(row));
	const signedIn = $derived(!!row.codex_access_token_set && !row.needs_reauth);
	const banked = untrack(() => new BankedResets(() => credential.row!));
	let reconnecting = $state(false);
	let saving = $state(false);
	let error = $state('');

	async function rename(name: string) {
		if (saving || !name.trim() || name.trim() === row.name) return;
		saving = true;
		error = '';
		try {
			await providers.saveProvider({ name: name.trim(), provider_type: row.provider_type, base_url: row.base_url }, row.id);
			flashSaved();
		} catch (e) { error = getErrorMessage(e, 'Could not rename this account'); }
		finally { saving = false; }
	}

	const usageEntry = $derived(providers.current.usage?.providers?.find((p) => p.provider_id === row.id) ?? null);
	const weekly = $derived(usageEntry?.windows?.find((w) => w.type === 'seven_day') ?? null);

	const position = $derived(
		!row.enabled
			? 'Not in the model picker'
			: signedIn
				? `${row.models.length} models in picker`
				: 'Sign in again to use it'
	);

	const facts = $derived([
		{ label: 'Plan', value: planLabel(usageEntry?.plan_type) ?? planLabel(row.account_plan) },
		{ label: 'Weekly use', value: weekly ? `${Math.round(weekly.utilization)}%` : null },
		{ label: 'Models', value: `${row.models.length} in picker` },
		{ label: 'Resets', value: banked.hasLoaded ? `${banked.availableCount} banked` : null }
	]);

	const tabs = $derived([
		{ id: 'Overview' },
		{ id: 'Models' },
		{ id: 'Usage', count: banked.hasLoaded && banked.availableCount ? banked.availableCount : undefined }
	]);

	const danger = $derived<DangerAction[]>([
		{
			label: 'Remove this credential',
			hint: 'Signs out this account',
			button: 'Remove',
			question: `Remove ${credential.name}?`,
			onConfirm: async () => {
				await providers.deleteProvider(row.id);
				settingsNav.selectItem(provider.item, 'providers');
			}
		}
	]);

	function reconnect() {
		settingsNav.setTab('providers', credential.item, 'Overview');
		reconnecting = true;
	}

	onMount(() => {
		if (signedIn) void banked.refresh();
		void providers.loadUsage();
	});
</script>

<DetailPane
	name={credential.name}
	badge="Subscription"
	status={credential.status}
	statusText={credential.statusText}
	subtitle={position}
	{facts}
	{tabs}
	{danger}
	item={credential.item}
>
	{#snippet tile()}<ProviderTile {provider} size="lg" />{/snippet}
	{#snippet primary()}
		{#if !signedIn && !reconnecting}
			<button type="button" class="st-btn warn" onclick={reconnect}><RefreshCw />Reconnect</button>
		{/if}
		<CredentialControl {credential} showState />
	{/snippet}
	{#snippet children(tab: string | null)}
		{#if tab === 'Models'}
			{#key row.id}<ModelCuration {row} />{/key}
		{:else if tab === 'Usage'}
			<SubscriptionUsage {banked} {usageEntry} {signedIn} plan={row.account_plan ?? null} />
		{:else if reconnecting}
			<RowGroup title="Reconnect" bare>
				<DeviceCodeSetup
					kind={grok ? 'xai' : 'codex'}
					existingProviderId={row.id}
					onDone={() => {
						reconnecting = false;
						flashSaved();
						void banked.refresh();
					}}
					onCancel={() => (reconnecting = false)}
				/>
			</RowGroup>
		{:else}
			{#if error}<p class="pv-msg err" role="alert">{error}</p>{/if}
			<RowGroup title="Account">
				<SettingRow label="Name"><TextField value={row.name} width={220} maxlength={120} disabled={saving} onchange={rename} /></SettingRow>
				<SettingRow label="Sign-in">
					<ValueText
						status={signedIn ? 'ok' : 'warn'}
						value={row.needs_reauth ? 'Reconnect needed' : signedIn ? 'Valid, refreshes itself' : 'Not signed in'}
					/>
				</SettingRow>
				<SettingRow label="Endpoint">
					<ValueText mono value={row.base_url.replace(/^https?:\/\//, '')} />
				</SettingRow>
			</RowGroup>
			{#if supportsNativeImages(row)}
				<RowGroup title="Use">
					<SettingRow label="Native image generation" help={NATIVE_IMAGE_HELP}>
						<ValueText status="ok" value="Supported" />
					</SettingRow>
				</RowGroup>
			{/if}
		{/if}
	{/snippet}
</DetailPane>
