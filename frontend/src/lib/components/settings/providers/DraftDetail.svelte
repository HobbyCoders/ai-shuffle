<script lang="ts">
	/**
	 * A credential being set up. The setup runs here, in the detail; finishing
	 * selects the new credential, cancelling drops the draft row.
	 */
	import { settingsNav } from '$lib/settings/navigation.svelte';
	import { flashSaved } from '$lib/settings/save-status.svelte';
	import { providers } from '$lib/stores/providers.svelte';
	import DetailPane from '../shell/DetailPane.svelte';
	import RowGroup from '../rows/RowGroup.svelte';
	import SettingRow from '../rows/SettingRow.svelte';
	import ProviderTile from './ProviderTile.svelte';
	import ModelCuration from './ModelCuration.svelte';
	import DeviceCodeSetup from './setup/DeviceCodeSetup.svelte';
	import KeySetup from './setup/KeySetup.svelte';
	import FalKeySetup from './setup/FalKeySetup.svelte';
	import GatewaySetup from './setup/GatewaySetup.svelte';
	import ClaudeCliSetup from './setup/ClaudeCliSetup.svelte';
	import ClaudeCloudSetup from './setup/ClaudeCloudSetup.svelte';
	import { providerDrafts, type Draft } from './drafts.svelte';
	import UnofficialBadge from './UnofficialBadge.svelte';
	import { rowItem, UNOFFICIAL_SIGN_IN_HELP, type ProviderView } from './providers-model';

	interface Props {
		draft: Draft;
		provider: ProviderView;
	}

	let { draft, provider }: Props = $props();

	/** Claude has no other credential: the new one becomes the active one. */
	const claudeFirst = $derived(
		!!provider?.claude && !provider.credentials.some((c) => !c.draft && c.status !== 'warn')
	);

	const subtitle = $derived(
		provider?.draft
			? provider.name === 'New provider'
				? 'New provider'
				: `${provider.name}, new provider`
			: `${provider?.name}, new ${
					draft.flavor === 'cli' || draft.flavor === 'device'
						? 'subscription'
						: draft.flavor === 'cloud'
							? 'cloud provider or gateway'
							: 'API key'
				}`
	);

	async function finish(item: string) {
		const draftItem = draft.item;
		settingsNav.selectItem(item, 'providers');
		providerDrafts.remove(draftItem);
		flashSaved();
		await providers.refreshProviders().catch(() => {});
	}

	function cancel() {
		const draftItem = draft.item;
		if (provider.draft) settingsNav.clearSelection('providers');
		else settingsNav.selectItem(provider.item, 'providers');
		providerDrafts.remove(draftItem);
	}
</script>

<DetailPane name={draft.name} badge="Draft" status="busy" statusText="Setting up" {subtitle} item={draft.item}>
	{#snippet tile()}<ProviderTile {provider} size="lg" />{/snippet}

	{#if draft.flavor === 'device'}
		{#if draft.step === 'models' && draft.rowId}
			<ModelCuration mode="setup" providerId={draft.rowId} onDone={() => finish(rowItem(draft.rowId!))} />
		{:else if draft.step === 'run'}
			<RowGroup title="Sign in" bare>
				<DeviceCodeSetup
					kind={provider.id === 'openai' ? 'codex' : 'xai'}
					draftRowId={draft.rowId}
					onRow={(id) => id && id !== draft.rowId && providerDrafts.update(draft.item, { rowId: id, ownsRow: true })}
					onConnected={(id) => providerDrafts.update(draft.item, { rowId: id, ownsRow: false, step: 'models' })}
					onCancel={cancel}
				/>
			</RowGroup>
		{:else}
			<RowGroup title="Sign in">
				<SettingRow
					label={provider.id === 'openai' ? 'Sign in with ChatGPT' : 'Sign in with SuperGrok'}
					hint="No key needed"
					help={UNOFFICIAL_SIGN_IN_HELP}
				>
					<UnofficialBadge />
					<button
						type="button"
						class="st-btn primary"
						onclick={() => providerDrafts.update(draft.item, { step: 'run' })}
					>
						Start sign-in
					</button>
				</SettingRow>
			</RowGroup>
			<div class="pv-acts"><button type="button" class="st-btn ghost" onclick={cancel}>Cancel</button></div>
		{/if}
	{:else if draft.flavor === 'cli'}
		<ClaudeCliSetup makeActive={claudeFirst} onFinish={finish} onCancel={cancel} />
	{:else if draft.flavor === 'cloud'}
		<ClaudeCloudSetup onFinish={finish} onCancel={cancel} />
	{:else if draft.flavor === 'gateway'}
		<GatewaySetup {draft} onFinish={finish} onCancel={cancel} />
	{:else if draft.flavor === 'fal'}
		<FalKeySetup onFinish={finish} onCancel={cancel} />
	{:else}
		<KeySetup {draft} makeActive={claudeFirst} onFinish={finish} onCancel={cancel} />
	{/if}
</DetailPane>
