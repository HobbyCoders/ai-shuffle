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
	 * The remaining credential groups (Media & tools / fal, Integrations / GitHub)
	 * now live in the store-adjacent `MediaToolsSection`, rendered below with only
	 * their own GitHub props (Chunk 8 lifted them out of ConnectionsSection, which
	 * is now removed, and folded the old AI-models tab into a "Manage media models"
	 * drawer there).
	 *
	 * Data is refetched on every mount via `providers.refreshAll()`: the settings
	 * sections remount on every tab switch, so re-opening the Providers tab always
	 * reconciles with the server (this fixes a Codex sign-in that completes after
	 * the UI stopped watching leaving the row stuck on "Not connected"). The refresh
	 * is SILENT — loaders keep their loading flags down when data already renders,
	 * so nothing flickers. (`setup/+page.svelte` still uses the one-shot
	 * `ensureLoaded()`; that path is untouched.)
	 */
	import { onMount } from 'svelte';
	import { Info, Server, Plus } from 'lucide-svelte';
	import { providers } from '$lib/stores/providers.svelte';
	import MediaToolsSection from './MediaToolsSection.svelte';
	import ProviderList from './ProviderList.svelte';
	import AddProviderModal from './AddProviderModal.svelte';

	interface Props {
		// ----- GitHub (forwarded to MediaToolsSection) -----
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

	// Add-provider modal open flag. The modal owns its own step machine + flows;
	// on completion or dismissal it calls back to close.
	let addProviderOpen = $state(false);
	function onAddProvider() {
		addProviderOpen = true;
	}

	onMount(() => {
		// Refetch on every card-open / tab-switch. A Codex sign-in that completes
		// after the UI stops watching would otherwise leave the row stuck on "Not
		// connected"; refetching on open reconciles it. Silent — already-rendered
		// rows don't flicker while the background refresh runs.
		providers.refreshAll();
	});
</script>

<!-- One home for every credential -->
<div class="ed-note">
	<Info />
	<div><b>One home for every credential.</b> Your Claude account, API keys and model gateways live here together — each row shows its status and is masked. Pick which models a provider offers via its <b>Manage models</b>; choose the model per agent in the agent editor.</div>
</div>

<!-- Group A: model providers -->
<div class="set-group">
	<div class="set-grouphead">
		<h3><Server />Providers</h3>
		<p>Your Claude subscription and API key, plus any OpenRouter / OpenAI / custom gateways. Enabled providers and their curated models appear in the agent editor.</p>
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

<!-- Media & tools (fal.ai, GitHub, AI media models) — served by MediaToolsSection. -->
<MediaToolsSection
	{githubUser}
	{githubToken}
	{onGithubTokenChange}
	{githubLoginLoading}
	{onConnectGithub}
	{onDisconnectGithub}
/>

{#if addProviderOpen}
	<AddProviderModal onClose={() => (addProviderOpen = false)} />
{/if}

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
