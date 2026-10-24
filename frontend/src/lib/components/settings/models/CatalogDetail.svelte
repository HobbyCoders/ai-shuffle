<script lang="ts">
	/**
	 * CatalogDetail: the generation catalog (#379).
	 *
	 *   All accounts: every connected account on every worker (fal.ai, ChatGPT
	 *   and SuperGrok plans, OpenAI/xAI API keys), verified or not, with its
	 *   status, live price and holding worker, plus one optional favourite per
	 *   kind (a bookmark, apart from the remembered choice).
	 *   fal.ai models: the full fal.ai list; picking remembers a generation
	 *   model or pins a model for a media transform tool.
	 */
	import { onMount } from 'svelte';
	import { RefreshCw } from 'lucide-svelte';
	import { catalogStore } from '$lib/stores/catalog';
	import { settingsNav } from '$lib/settings/navigation.svelte';
	import { generationChoices } from '$lib/stores/generation-choices.svelte';
	import { modelLabel } from '$lib/utils/generation-choice';
	import DetailPane from '../shell/DetailPane.svelte';
	import type { Fact } from '../shell/FactsStrip.svelte';
	import type { DetailTab } from '../shell/DetailTabs.svelte';
	import InlineBanner from '../rows/InlineBanner.svelte';
	import { ALL_TOOLS, GENERATION_TOOL_KIND } from './media-tools.svelte';
	import { getModelsState } from './tasks.svelte';
	import { CATALOG_ICON } from './icons';
	import AccountCatalog from './AccountCatalog.svelte';
	import FalCatalog from './FalCatalog.svelte';
	import { AccountCatalog as AccountCatalogState, accountCount } from './account-catalog.svelte';

	const ITEM = 'catalog';
	const TABS: DetailTab[] = [
		{ id: 'accounts', label: 'All accounts' },
		{ id: 'fal', label: 'fal.ai models' }
	];

	const { tools } = getModelsState();
	const accounts = new AccountCatalogState();
	onMount(() => accounts.start());

	const tab = $derived(settingsNav.tabOf('models', ITEM, TABS.map((t) => t.id)));

	// ── fal.ai tab (catalogStore is a plain store; catalogVersion makes it reactive) ──
	const falSize = $derived.by(() => {
		void tools.catalogVersion;
		return catalogStore.catalogSize;
	});
	const falCategories = $derived.by(() => {
		void tools.catalogVersion;
		return catalogStore.categories.length;
	});
	const falLoading = $derived.by(() => {
		void tools.catalogVersion;
		return catalogStore.loading;
	});
	const falError = $derived.by(() => {
		void tools.catalogVersion;
		return catalogStore.error;
	});
	const remembered = $derived(Object.keys(generationChoices.remembered).length);
	const pinned = $derived(ALL_TOOLS.filter((t) => !GENERATION_TOOL_KIND[t.id] && tools.modelId(t)).length);

	// ── All accounts tab ──
	const entries = $derived(accounts.entries ?? []);
	const ready = $derived(entries.filter((e) => e.status === 'available').length);
	const workers = $derived(new Set(entries.map((e) => e.worker)).size);
	const favourite = $derived(generationChoices.favourites[accounts.kind]);

	const facts = $derived<Fact[]>(
		tab === 'fal'
			? [
					{ label: 'Models', value: falSize ? falSize.toLocaleString() : null },
					{ label: 'Remembered', value: remembered },
					{ label: 'Pinned for tools', value: pinned },
					{ label: 'Categories', value: falCategories || null }
				]
			: [
					{ label: 'Accounts', value: accounts.entries ? accountCount(entries) : null },
					{ label: 'Workers', value: accounts.entries ? workers : null },
					{ label: 'Ready', value: accounts.entries ? `${ready} of ${entries.length}` : null },
					{ label: 'Favourite', value: favourite ? modelLabel(favourite.model) : 'None' }
				]
	);

	const busy = $derived(tab === 'fal' ? falLoading : accounts.loading);
	const failed = $derived(tab === 'fal' ? !!falError : !!accounts.error);

	function refresh() {
		if (tab === 'fal') void catalogStore.refresh();
		else void accounts.load();
	}
</script>

{#snippet primary()}
	<button type="button" class="st-btn" disabled={busy} onclick={refresh}>
		<RefreshCw class={busy ? 'spin' : ''} />Refresh
	</button>
{/snippet}

{#snippet banner()}
	{#if tab === 'fal' && tools.falEnabled === false}
		<InlineBanner tone="warn">
			fal.ai is off, so these models cannot run.
			{#snippet actions()}
				<button type="button" class="st-btn sm" onclick={() => settingsNav.go({ category: 'providers' })}>
					Open providers
				</button>
			{/snippet}
		</InlineBanner>
	{/if}
{/snippet}

<DetailPane
	name="Generation catalog"
	icon={CATALOG_ICON}
	status={busy ? 'busy' : failed ? 'err' : 'ok'}
	statusText={busy ? 'Refreshing' : failed ? 'Could not load' : 'Synced'}
	badge="Catalog"
	subtitle={tab === 'fal' ? 'Pick to remember or pin a model' : 'Every account, on every worker'}
	tabs={TABS}
	{primary}
	{facts}
	{banner}
	category="models"
	item={ITEM}
>
	{#snippet children(current)}
		{#if current === 'fal'}
			<FalCatalog />
		{:else}
			<AccountCatalog catalog={accounts} />
		{/if}
	{/snippet}
</DetailPane>

<style>
	:global(.settings-shell svg.lucide.spin) {
		animation: st-models-spin 0.9s linear infinite;
	}
	@keyframes -global-st-models-spin {
		to {
			transform: rotate(360deg);
		}
	}
	@media (prefers-reduced-motion: reduce) {
		:global(.settings-shell svg.lucide.spin) {
			animation: none;
		}
	}
</style>
