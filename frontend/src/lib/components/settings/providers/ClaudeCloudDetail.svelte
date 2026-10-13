<script lang="ts">
	/**
	 * Claude through a cloud provider or gateway (Amazon Bedrock, Google Vertex
	 * AI, Microsoft Foundry or an LLM gateway), chosen by the owner. The warning
	 * stays at the top while the method is saved or on. Owner only: the
	 * worker refuses everyone else.
	 */
	import { onMount } from 'svelte';
	import { getClaudeCloud, type ClaudeCloudConfig } from '$lib/api/claude-auth';
	import { getErrorMessage } from '$lib/api/client';
	import { providers } from '$lib/stores/providers.svelte';
	import { settingsNav } from '$lib/settings/navigation.svelte';
	import { flashSaved } from '$lib/settings/save-status.svelte';
	import DetailPane from '../shell/DetailPane.svelte';
	import type { DangerAction } from '../shell/DangerZone.svelte';
	import InlineBanner from '../rows/InlineBanner.svelte';
	import ProviderTile from './ProviderTile.svelte';
	import BuiltInModels from './tabs/BuiltInModels.svelte';
	import ClaudeCloudForm from './ClaudeCloudForm.svelte';
	import { CLOUD_WARNING, GATEWAY_WARNING } from './claude-cloud';
	import { CLAUDE_CLOUD_ITEM, type CredentialView, type ProviderView } from './providers-model';

	interface Props {
		credential: CredentialView;
		provider: ProviderView;
	}

	let { credential, provider }: Props = $props();

	const mode = $derived(providers.current.claude?.mode ?? 'subscription');
	const status = $derived(providers.current.claude?.cloud);
	let config = $state<ClaudeCloudConfig | null>(null);
	let loadError = $state('');

	const position = $derived(
		mode === 'off'
			? 'Claude is off'
			: mode === 'cloud'
				? status?.ready
					? 'Runs every Claude session'
					: 'Selected, not ready'
				: 'Standby for Claude'
	);
	const gateway = $derived((config?.method ?? status?.method) === 'gateway');

	async function load() {
		try {
			config = await getClaudeCloud();
			loadError = '';
		} catch (e) {
			loadError =
				(e as { status?: unknown } | null)?.status === 403
					? 'Only the owner of this worker can view or change this.'
					: getErrorMessage(e, 'Could not read the cloud settings');
		}
	}
	onMount(load);

	async function saved(next: ClaudeCloudConfig) {
		config = next;
		flashSaved();
	}

	async function remove() {
		// Read before the await: removing the row closes this detail, and its
		// props cannot be read once it is gone.
		const back = provider.item;
		try {
			await providers.clearClaudeCloud();
			settingsNav.selectItem(back, 'providers');
		} catch (e) {
			loadError = getErrorMessage(e, 'Remove failed');
		}
	}

	const danger = $derived<DangerAction[]>(
		status?.configured
			? [
					{
						label: 'Remove this credential',
						hint: 'Deletes the stored values',
						button: 'Remove',
						question: `Remove ${status.method_label ?? 'the cloud method'}?`,
						onConfirm: remove
					}
				]
			: []
	);
</script>

<DetailPane
	name={credential.name}
	badge="Cloud"
	status={credential.status}
	statusText={credential.statusText}
	subtitle={position}
	facts={[
		{ label: 'Method', value: status?.method_label ?? 'None' },
		{ label: 'Values', value: status?.ready ? 'All present' : status?.missing.length ? `Missing ${status.missing.join(', ')}` : 'None' },
		{ label: 'Billing', value: gateway ? 'Through the gateway' : 'Your cloud account' },
		{ label: 'Models', value: 'Built in' }
	]}
	tabs={['Overview', 'Models']}
	{danger}
	item={CLAUDE_CLOUD_ITEM}
>
	{#snippet tile()}<ProviderTile {provider} size="lg" />{/snippet}
	{#snippet banner()}
		<InlineBanner tone="warn">{config?.warning ?? CLOUD_WARNING}</InlineBanner>
		{#if gateway}<InlineBanner tone="warn">{config?.gateway_warning ?? GATEWAY_WARNING}</InlineBanner>{/if}
		{#if status?.storage_locked}
			<InlineBanner tone="warn">The credential store is locked, so the stored values cannot be read.</InlineBanner>
		{/if}
	{/snippet}
	{#snippet children(tab: string | null)}
		{#if tab === 'Models'}
			<BuiltInModels />
		{:else if config}
			<ClaudeCloudForm {config} {mode} showWarning={false} onSaved={saved} />
		{:else if loadError}
			<p class="pv-msg err" role="alert">{loadError}</p>
		{:else}
			<p class="pv-msg">Loading</p>
		{/if}
	{/snippet}
</DetailPane>
