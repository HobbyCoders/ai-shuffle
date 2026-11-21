<script lang="ts">
	import { detailLine } from './credential-display';
	/**
	 * A provider with nothing connected: the kinds it supports, each with Set up.
	 * Claude also shows its mode and the installed CLI (version, update check,
	 * Remove), so an owner without a Claude sign-in can still change or remove them.
	 * Antigravity shows its installed agent the same way: the update notice
	 * appears for any installed agent, so its switch has to be reachable here.
	 */
	import { untrack } from 'svelte';
	import { providers } from '$lib/stores/providers.svelte';
	import { isNativeProvider } from '$lib/api/model-providers';
	import { loadNativeOptions, nativeOptions } from './native-options.svelte';
	import { getSettingsAccess } from '$lib/settings/access.svelte';
	import DetailPane from '../shell/DetailPane.svelte';
	import RowGroup from '../rows/RowGroup.svelte';
	import SettingRow from '../rows/SettingRow.svelte';
	import ProviderTile from './ProviderTile.svelte';
	import ClaudeCliInstall from './setup/ClaudeCliInstall.svelte';
	import ClaudeModeRow from './ClaudeModeRow.svelte';
	import AntigravityInstall from './setup/AntigravityInstall.svelte';
	import { antigravity } from './antigravity.svelte';
	import { kindsOf, type CredentialFlavor, type ProviderView } from './providers-model';
	import { startSetup } from './provider-actions';

	interface Props {
		provider: ProviderView;
	}

	let { provider }: Props = $props();
	const access = getSettingsAccess();
	const cliInstalled = $derived(!!provider.claude && !!providers.current.claude?.cli?.installed);
	// Stay mounted once shown, so a Remove done here can show its confirmation
	// after the CLI is gone; the section leaves when that is dismissed.
	let showCli = $state(false);
	$effect(() => { if (cliInstalled) showCli = true; });
	const isAntigravity = $derived(provider.id === 'antigravity');
	// Untracked: load() reads and writes the store's own state, which must not re-run this.
	$effect(() => { if (isAntigravity && !access.readOnly) untrack(() => void antigravity.load(true)); });
	const agentInstalled = $derived(isAntigravity && !access.readOnly && !!antigravity.status?.installed);
	const kinds = $derived(kindsOf(provider.id));
	/** Routes that are a pasted key; a sign-in route has no key to list from (#1652). */
	const KEY_FLAVORS = new Set<CredentialFlavor>(['key', 'plan-key', 'agy-key', 'gateway']);
	const models = $derived(
		provider.claude
			? 'Built in'
			: provider.media
				? 'Media models'
				: provider.id === 'openrouter'
					? 'Hundreds'
					: provider.id === 'antigravity'
						? 'From the agent'
						: kinds.every((k) => KEY_FLAVORS.has(k.flavor))
							? 'From the key'
							: 'From the account'
	);
</script>

<DetailPane
	name={provider.name}
	subtitle={detailLine('Not connected', kinds.map((k) => k.label).join(', '), models)}
	item={provider.item}
>
	{#snippet tile()}<ProviderTile {provider} size="lg" dashed />{/snippet}
	{#if provider.claude}<ClaudeModeRow {provider} />{/if}
	{#if isNativeProvider(provider.id) && !kinds.length}
		{#if nativeOptions.error}
			<p class="pv-msg err" role="alert">{nativeOptions.error}</p>
			<button type="button" class="st-btn" onclick={() => loadNativeOptions()}>Retry provider options</button>
		{:else}<p class="pv-msg" role="status">Loading provider options…</p>{/if}
	{/if}
	<RowGroup title="Connect" id="providers-connect">
		{#each kinds as k (k.flavor)}
			<SettingRow label={k.label} hint={k.hint}>
				<button type="button" class="st-btn primary" onclick={() => startSetup(provider, k.flavor)}>Set up</button>
			</SettingRow>
		{/each}
	</RowGroup>
	{#if provider.claude && (cliInstalled || showCli)}
		<ClaudeCliInstall manage isAdmin={!access.readOnly} onDismiss={() => (showCli = false)} />
	{/if}
	{#if agentInstalled}<AntigravityInstall />{/if}
</DetailPane>
