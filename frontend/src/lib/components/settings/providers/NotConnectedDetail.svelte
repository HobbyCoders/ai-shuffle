<script lang="ts">
	import { detailLine } from './credential-display';
	/**
	 * A provider with nothing connected: the kinds it supports, each with Set up.
	 * Claude also shows its mode and the installed CLI (version, keep installed,
	 * Remove), so an owner without a Claude sign-in can still change or remove them.
	 */
	import { providers } from '$lib/stores/providers.svelte';
	import { getSettingsAccess } from '$lib/settings/access.svelte';
	import DetailPane from '../shell/DetailPane.svelte';
	import RowGroup from '../rows/RowGroup.svelte';
	import SettingRow from '../rows/SettingRow.svelte';
	import ProviderTile from './ProviderTile.svelte';
	import ClaudeCliInstall from './setup/ClaudeCliInstall.svelte';
	import ClaudeModeRow from './ClaudeModeRow.svelte';
	import { kindsOf, type ProviderView } from './providers-model';
	import { startSetup } from './provider-actions';
	import { isNativeProvider, nativeOptions } from './native-options.svelte';

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
	const kinds = $derived(kindsOf(provider.id));
	const models = $derived(
		provider.claude
			? 'Built in'
			: provider.media
				? 'Media models'
				: provider.id === 'openrouter'
					? 'Hundreds'
					: 'From the key'
	);
</script>

<DetailPane
	name={provider.name}
	subtitle={detailLine('Not connected', kinds.map((k) => k.label).join(', '), models)}
	item={provider.item}
>
	{#snippet tile()}<ProviderTile {provider} size="lg" dashed />{/snippet}
	{#if provider.claude}<ClaudeModeRow {provider} />{/if}
	{#if isNativeProvider(provider.id) && nativeOptions.loading}<p class="pv-msg" role="status">Loading connection options</p>{/if}
	{#if isNativeProvider(provider.id) && nativeOptions.error}
		<p class="pv-msg err" role="alert">{nativeOptions.error}</p>
		<button type="button" class="st-btn" onclick={() => nativeOptions.load(true)}>Retry</button>
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
</DetailPane>
