<script lang="ts">
	/**
	 * Settings > Providers: providers and the credentials connected to them.
	 *
	 * The list shows only connected credentials, each on its provider's
	 * connector line with a slot switch (Claude keys: a selection action);
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
	import {
		buildProviders,
		CLAUDE_KEY_ITEM,
		CLAUDE_SUB_ITEM,
		claudeUsageRecorded,
		isConnected,
		parseProviderItem
	} from './providers-model';
	import { startGateway } from './provider-actions';
	import { loadNativeOptions, nativeOptions } from './native-options.svelte';

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
			claudePending: !s.claude && !s.claudeError && !s.claudeForbidden,
			drafts: providerDrafts.list,
			draftGateways: providerDrafts.gateways
		})
	);
	const connectedCount = $derived(views.filter((p) => isConnected(p) && !p.draft).length);
	const loading = $derived(!s.initialSnapshotReady || s.providersLoading || (!s.claude && !s.claudeError && !s.claudeForbidden));
	const readError = $derived(s.providersError || s.integrationsError || s.claudeError);
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
		// Registry/search links use the stable kind id; open the selected saved key.
		if (selected === CLAUDE_KEY_ITEM) {
			const provider = views.find((p) => p.claude);
			const keys = provider?.credentials.filter((c) => c.flavor === 'anthropic') ?? [];
			const credential = keys.find((c) => c.active) ?? keys[0];
			if (provider && credential) return { kind: 'credential', credential, provider } as const;
		}
		// The same for saved Claude sign-ins: the stable id opens the active one.
		if (selected === CLAUDE_SUB_ITEM) {
			const provider = views.find((p) => p.claude);
			const subs = provider?.credentials.filter((c) => c.flavor === 'cli' && !c.draft) ?? [];
			const credential = subs.find((c) => c.item === CLAUDE_SUB_ITEM) ?? subs.find((c) => c.active) ?? subs[0];
			if (provider && credential) return { kind: 'credential', credential, provider } as const;
		}
		for (const provider of views) {
			const credential = provider.credentials.find((c) => c.item === selected);
			if (credential) return { kind: 'credential', credential, provider } as const;
		}
		return null;
	});

	// Back can replay a setup draft that Save removed, or a credential that
	// was deleted. A settled missing target must show the phone list, not an
	// empty detail with no Back control. Keep pending loads selected.
	$effect(() => {
		if (selected && !target && !loading && !readError) settingsNav.clearSelection('providers');
	});

	onMount(() => {
		void providers.refreshAll();
		void loadNativeOptions();
		// Subscription rows name their plan; the stored snapshot has the freshest one.
		void providers.loadUsage();
	});
	watchResources(['settings'], () => providers.refreshAll());
	onDestroy(() => providerDrafts.discardAll());
</script>

<CollectionPage
	category="providers"
	count={s.initialSnapshotReady ? `${connectedCount} connected` : 'Loading…'}
	addLabel="Add provider"
	onAdd={startGateway}
	replaceOnSelect
	bind:filter
>
	{#snippet list()}
		{#if !s.initialSnapshotReady}
			<p class="pv-empty" role="status">Loading providers…</p>
		{:else if forbidden}
			<p class="pv-empty">{FORBIDDEN_TEXT}</p>
		{:else}
			{#if readError}
				<p class="pv-msg err" role="alert">{readError}</p>
				<button type="button" class="st-btn ghost" disabled={s.refreshing} aria-busy={s.refreshing} onclick={() => providers.refreshAll()}>Retry providers</button>
			{/if}
			{#if nativeOptions.error}
				<p class="pv-msg err" role="alert">{nativeOptions.error}</p>
				<button type="button" class="st-btn ghost" onclick={() => loadNativeOptions()}>Retry provider options</button>
			{/if}
			<!-- Failed reads are unknown, not evidence that a provider is disconnected. -->
			<ProviderList views={readError ? views.filter(isConnected) : views} {filter} />
		{/if}
	{/snippet}
	{#snippet detail()}
		{#if selected && !s.initialSnapshotReady}
			<EmptyDetail icon={KeyRound} message="Loading providers" />
		{:else if selected && readError && (!target || (target.kind === 'provider' && !isConnected(target.provider)))}
			<EmptyDetail icon={KeyRound} message={readError}>
				<button type="button" class="st-btn" disabled={s.refreshing} aria-busy={s.refreshing} onclick={() => providers.refreshAll()}>Retry providers</button>
			</EmptyDetail>
		{:else if target?.kind === 'draft'}
			{#key target.draft.item}<DraftDetail draft={target.draft} provider={target.provider} />{/key}
		{:else if target?.kind === 'provider'}
			{#if target.provider.pending}
				<EmptyDetail icon={KeyRound} message="Loading providers" />
			{:else if isConnected(target.provider)}
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
			<EmptyDetail icon={KeyRound} message="Select a provider or credential." idle />
		{/if}
	{/snippet}
</CollectionPage>
