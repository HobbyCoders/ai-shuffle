<script lang="ts">
	/**
	 * A provider with nothing connected: the kinds it supports, each with Set up.
	 * Claude also shows the installed CLI (version, keep installed, Remove), so
	 * an owner without a Claude sign-in can still manage or remove it.
	 */
	import { providers } from '$lib/stores/providers.svelte';
	import { getSettingsAccess } from '$lib/settings/access.svelte';
	import DetailPane from '../shell/DetailPane.svelte';
	import RowGroup from '../rows/RowGroup.svelte';
	import SettingRow from '../rows/SettingRow.svelte';
	import ProviderTile from './ProviderTile.svelte';
	import ClaudeCliInstall from './setup/ClaudeCliInstall.svelte';
	import UnofficialBadge from './UnofficialBadge.svelte';
	import { kindsOf, UNOFFICIAL_SIGN_IN_HELP, type ProviderView } from './providers-model';
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
	badge="Provider"
	status="off"
	statusText="Not connected"
	subtitle="Nothing added yet"
	facts={[
		{ label: 'Kinds', value: kinds.map((k) => k.label).join(', ') },
		{ label: 'Models', value: models }
	]}
	item={provider.item}
>
	{#snippet tile()}<ProviderTile {provider} size="lg" dashed />{/snippet}
	<RowGroup title="Connect" id="providers-connect">
		{#each kinds as k (k.flavor)}
			<SettingRow label={k.label} hint={k.hint} help={k.unofficial ? UNOFFICIAL_SIGN_IN_HELP : undefined}>
				{#if k.unofficial}<UnofficialBadge />{/if}
				<button type="button" class="st-btn primary" onclick={() => startSetup(provider, k.flavor)}>Set up</button>
			</SettingRow>
		{/each}
	</RowGroup>
	{#if provider.claude && (cliInstalled || showCli)}
		<ClaudeCliInstall manage isAdmin={!access.readOnly} onDismiss={() => (showCli = false)} />
	{/if}
</DetailPane>
