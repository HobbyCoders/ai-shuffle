<script lang="ts">
	/**
	 * ProvidersSection — the "Providers" settings tab (Chunk 4 build-out).
	 *
	 * Replaces the Chunk-3 passthrough shell with the real, store-driven provider
	 * surface. It owns Group A of the plan (§1.1): the intro callout, an "Add
	 * provider" button (no-op this chunk — Chunk 5 wires the Add-provider modal),
	 * and the unified `ProviderList` (Claude subscription, Claude API key, then the
	 * model_providers rows).
	 *
	 * The remaining Connections groups (Media & tools / fal, Integrations / GitHub)
	 * still live in `ConnectionsSection`, rendered below with only their own props.
	 * Chunks 5–8 lift those out too.
	 *
	 * Data is loaded once per session via `providers.ensureLoaded()` (landmine L1 /
	 * ruling PR6): the settings sections remount on every tab switch, so the store
	 * caches the initial provider + account fetch across those remounts. Mutations
	 * refresh the store independently, so the cache never goes stale.
	 */
	import { onMount } from 'svelte';
	import { Info, Server, Plus } from 'lucide-svelte';
	import { providers } from '$lib/stores/providers.svelte';
	import ConnectionsSection from '../ConnectionsSection.svelte';
	import ProviderList from './ProviderList.svelte';

	interface Props {
		// ----- GitHub (forwarded to the residual ConnectionsSection) -----
		githubUser: string | null;
		githubToken: string;
		onGithubTokenChange: (value: string) => void;
		githubLoginLoading: boolean;
		onConnectGithub: () => void;
		onDisconnectGithub: () => void;
	}
	let {
		githubUser,
		githubToken,
		onGithubTokenChange,
		githubLoginLoading,
		onConnectGithub,
		onDisconnectGithub
	}: Props = $props();

	const s = $derived(providers.current);

	// Add provider is a no-op this chunk — Chunk 5 wires the Add-provider modal.
	function onAddProvider() {
		// Chunk 5 wires this (Add-provider modal / flow).
	}

	onMount(() => {
		// Load once per session; cached across tab-switch remounts (L1 / PR6).
		providers.ensureLoaded();
	});
</script>

<!-- One home for every credential -->
<div class="ed-note">
	<Info />
	<div><b>One home for every credential.</b> Your Claude account, API keys and model gateways live here together — each row shows its status and is masked. Pick which models a provider offers via its <b>Manage models</b>; choose the model per profile in the profile editor.</div>
</div>

<!-- Group A: model providers -->
<div class="set-group">
	<div class="set-grouphead">
		<h3><Server />Providers</h3>
		<p>Your Claude subscription and API key, plus any OpenRouter / OpenAI / custom gateways. Enabled providers and their curated models appear in the profile editor.</p>
	</div>

	<div class="prov-groupbody">
		<div class="prov-toolbar">
			<h4>Configured providers</h4>
			<button type="button" class="ed-btn prov-add-btn" onclick={onAddProvider}>
				<Plus />Add provider
			</button>
		</div>

		{#if s.claudeAuthError}
			<div class="prov-err" role="alert">{s.claudeAuthError}</div>
		{/if}

		<ProviderList />
	</div>
</div>

<!-- Remaining groups (Media & tools, Integrations) still served by ConnectionsSection. -->
<ConnectionsSection
	{githubUser}
	{githubToken}
	{onGithubTokenChange}
	{githubLoginLoading}
	{onConnectGithub}
	{onDisconnectGithub}
/>

<style>
	.prov-groupbody {
		padding: 4px 18px 14px;
	}
	.prov-toolbar {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 12px;
		margin: 6px 0 10px;
	}
	.prov-toolbar h4 {
		margin: 0;
		font-size: 12px;
		font-weight: 600;
		letter-spacing: 0.03em;
		text-transform: uppercase;
		color: var(--muted-foreground);
	}
	.prov-add-btn {
		display: inline-flex;
		align-items: center;
		gap: 6px;
	}
	.prov-add-btn :global(svg) {
		width: 14px;
		height: 14px;
	}
	.prov-err {
		font-size: 12.5px;
		color: var(--destructive);
		padding: 4px 2px 8px;
	}
</style>
