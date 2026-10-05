<script lang="ts">
	/**
	 * Settings > Providers: providers and the credentials connected to them.
	 *
	 * The list shows only connected credentials, each on its provider's
	 * connector line with its own switch (Claude: the mode control instead);
	 * providers with nothing connected sit under "Not connected". "+" on a
	 * provider and "Add provider" (a custom gateway) make one draft row whose
	 * setup runs in the detail. See providers-model.ts for the rules.
	 */
	import './providers.css';
	import { onDestroy, onMount } from 'svelte';
	import { KeyRound } from 'lucide-svelte';
	import { providers } from '$lib/stores/providers.svelte';
	import { watchResources } from '$lib/services/watch-resources';
	import { settingsNav } from '$lib/settings/navigation.svelte';
	import { FORBIDDEN_TEXT } from '$lib/settings/access.svelte';
	import CollectionPage from '../shell/CollectionPage.svelte';
	import EmptyDetail from '../shell/EmptyDetail.svelte';
	import ProviderList from './ProviderList.svelte';
	import ProviderDetail from './ProviderDetail.svelte';
	import NotConnectedDetail from './NotConnectedDetail.svelte';
	import CredentialDetail from './CredentialDetail.svelte';
	import DraftDetail from './DraftDetail.svelte';
	import { providerDrafts } from './drafts.svelte';
	import { buildProviders, claudeUsageRecorded, isConnected, parseProviderItem } from './providers-model';
	import { startGateway } from './provider-actions';

	let filter = $state('');

	const s = $derived(providers.current);
	const views = $derived(
		buildProviders({
			rows: s.providers,
			claude: s.claude,
			anthropicKeyMasked: s.anthropicKeyMasked,
			fal: { keySet: s.falKeySet, masked: s.falKeyMasked, enabled: s.falEnabled },
			usage: s.usage?.providers,
			claudeUsed: claudeUsageRecorded(s.usage),
			drafts: providerDrafts.list,
			draftGateways: providerDrafts.gateways
		})
	);
	const connectedCount = $derived(views.filter((p) => isConnected(p) && !p.draft).length);
	const loading = $derived(s.providersLoading || (!s.claude && !s.claudeError && !s.claudeForbidden));
	const forbidden = $derived(s.claudeForbidden && s.providers.length === 0);

	const selected = $derived(settingsNav.selectedItem('providers'));
	const target = $derived.by(() => {
		const draft = providerDrafts.get(selected);
		if (draft) {
			const provider = views.find((p) => p.id === draft.providerId);
			return provider ? ({ kind: 'draft', draft, provider } as const) : null;
		}
		const providerId = parseProviderItem(selected);
		if (providerId) {
			const provider = views.find((p) => p.id === providerId);
			return provider ? ({ kind: 'provider', provider } as const) : null;
		}
		for (const provider of views) {
			const credential = provider.credentials.find((c) => c.item === selected);
			if (credential) return { kind: 'credential', credential, provider } as const;
		}
		return null;
	});

	onMount(() => {
		void providers.refreshAll();
		// Subscription rows name their plan; the stored snapshot has the freshest one.
		void providers.loadUsage();
	});
	watchResources(['settings'], () => providers.refreshAll());
	onDestroy(() => providerDrafts.discardAll());
</script>

<CollectionPage
	category="providers"
	count="{connectedCount} connected"
	addLabel="Add provider"
	onAdd={startGateway}
	bind:filter
>
	{#snippet list()}
		{#if forbidden}
			<p class="pv-empty">{FORBIDDEN_TEXT}</p>
		{:else}
			{#if s.providersError}<p class="pv-msg err" role="alert">{s.providersError}</p>{/if}
			<ProviderList {views} {filter} />
		{/if}
	{/snippet}
	{#snippet detail()}
		{#if target?.kind === 'draft'}
			{#key target.draft.item}<DraftDetail draft={target.draft} provider={target.provider} />{/key}
		{:else if target?.kind === 'provider'}
			{#if isConnected(target.provider)}
				<ProviderDetail provider={target.provider} />
			{:else}
				<NotConnectedDetail provider={target.provider} />
			{/if}
		{:else if target?.kind === 'credential'}
			{#key target.credential.item}
				<CredentialDetail credential={target.credential} provider={target.provider} />
			{/key}
		{:else if forbidden}
			<EmptyDetail icon={KeyRound} message={FORBIDDEN_TEXT} />
		{:else if selected && loading}
			<EmptyDetail icon={KeyRound} message="Loading providers" />
		{:else}
			<EmptyDetail icon={KeyRound} message="Select a provider or credential." />
		{/if}
	{/snippet}
</CollectionPage>
