<script lang="ts">
	/**
	 * Plugins: what agents wrote or installed (needing the owner's review),
	 * connected apps (connector runtimes and their accounts) and installed
	 * agent plugins, then the catalog, plugins found in project folders and
	 * the plugin sources. Owner-only parts (apps, sources, installs) follow
	 * Settings access.
	 */
	import { onMount, untrack } from 'svelte';
	import { Puzzle } from 'lucide-svelte';
	import { watchResources } from '$lib/services/watch-resources';
	import { settingsNav } from '$lib/settings/navigation.svelte';
	import { FORBIDDEN_TEXT, getSettingsAccess } from '$lib/settings/access.svelte';
	import CollectionPage from '../shell/CollectionPage.svelte';
	import EmptyDetail from '../shell/EmptyDetail.svelte';
	import ListItem from '../rows/ListItem.svelte';
	import ListGroupLabel from '../rows/ListGroupLabel.svelte';
	import InlineBanner from '../rows/InlineBanner.svelte';
	import { PluginsModel, parseItem, plural, setPluginsModel, type PluginEntry } from './plugins-model.svelte';
	import { EXPLORE_ICON, PACKAGE_ICON, PROJECTS_ICON, REVIEW_ICON, SOURCES_ICON, connectionIcon } from './icons';
	import { NEEDS_REVIEW } from './plugin-parts';
	import PluginDetail from './PluginDetail.svelte';
	import ConnectionDetail from './ConnectionDetail.svelte';
	import ExploreDetail from './ExploreDetail.svelte';
	import SourcesDetail from './SourcesDetail.svelte';
	import ProjectsDetail from './ProjectsDetail.svelte';
	import type { StatusTone } from '$lib/settings/types';

	const CATEGORY = 'plugins' as const;
	const access = getSettingsAccess();
	const model = new PluginsModel({ canManage: () => !access.readOnly });
	setPluginsModel(model);

	onMount(() => {
		void model.load();
		return () => model.dispose();
	});
	watchResources(['connectors', 'plugins'], () => model.load());

	let filter = $state('');
	const q = $derived(filter.trim().toLowerCase());
	const matches = (...parts: (string | null | undefined)[]) =>
		!q || parts.filter(Boolean).join(' ').toLowerCase().includes(q);

	const selected = $derived(settingsNav.selectedItem(CATEGORY));
	const parsed = $derived(parseItem(selected));

	// Deep links may name a bare plugin id, connector type or account id.
	$effect(() => {
		const raw = selected;
		if (!raw || parsed || model.loading) return;
		const next = untrack(() => model.resolveItem(raw));
		if (next) untrack(() => settingsNav.selectItem(next, CATEGORY));
	});

	interface Row {
		entry: PluginEntry;
		summary: string;
		meta?: string;
		status: StatusTone;
	}

	function appRow(e: PluginEntry): Row {
		const summary = e.count
			? plural(e.count, 'account')
			: e.installed
				? e.enabled
					? 'Runtime on'
					: 'Runtime off'
				: 'Not installed';
		// Accounts keep working on the bundled runtime when none is installed.
		const status: StatusTone = e.failing
			? 'err'
			: e.installed && !e.enabled
				? 'off'
				: e.count || e.enabled
					? 'ok'
					: 'standby';
		return { entry: e, summary, status, meta: e.type?.plugin_update_available ? 'Update' : undefined };
	}

	function packageRow(e: PluginEntry): Row {
		if (e.review) {
			const what = e.authored ? 'Agent draft' : 'Added by an agent';
			return {
				entry: e,
				summary: e.version ? `${what}, v${e.version}` : what,
				status: 'warn',
				meta: 'Review'
			};
		}
		return {
			entry: e,
			summary: e.description || (e.enabled ? 'On' : 'Off'),
			status: e.enabled ? 'ok' : 'off',
			meta: e.update ? 'Update' : e.enabled ? (e.version ? `v${e.version}` : undefined) : 'Off'
		};
	}

	const groups = $derived(
		[
			{ label: NEEDS_REVIEW, rows: model.review.map(packageRow) },
			{ label: 'Connected apps', rows: model.apps.map(appRow) },
			{ label: 'Installed', rows: model.installedPackages.map(packageRow) }
		]
			.map((g) => ({
				...g,
				rows: g.rows.filter((r) => matches(r.entry.name, r.summary, r.entry.description, g.label))
			}))
			.filter((g) => g.rows.length)
	);

	const installedCount = $derived(model.review.length + model.apps.length + model.installedPackages.length);
	const exploreSummary = $derived(model.loading ? 'Loading' : `${model.availableCount} to add`);
	const sourcesSummary = $derived(
		model.updates.length ? plural(model.updates.length, 'update') : plural(model.marketplaces.length, 'source')
	);
	const showExplore = $derived(matches('Explore plugins', 'catalog browse install', exploreSummary));
	const showSources = $derived(!access.readOnly && matches('Sources and updates', 'marketplace', sourcesSummary));
	const projectsSummary = $derived(
		model.projectsLoaded ? plural(model.projectPlugins.reduce((n, r) => n + r.plugins.length, 0), 'plugin') : 'In project folders'
	);
	const showProjects = $derived(matches('Project plugins', 'project folder scope local', projectsSummary));
</script>

<CollectionPage category={CATEGORY} count={model.loading ? undefined : `${installedCount} added`} bind:filter>
	{#snippet list()}
		{#if model.forbidden}
			<p class="st-empty">{FORBIDDEN_TEXT}</p>
		{:else if model.error}
			<InlineBanner tone="warn">
				<span role="alert">{model.error}</span>
				{#snippet actions()}
					<button type="button" class="st-btn sm" onclick={() => model.load()}>Retry</button>
				{/snippet}
			</InlineBanner>
		{/if}
		{#each groups as group (group.label)}
			<ListGroupLabel label={group.label} />
			{#each group.rows as row (row.entry.item)}
				<ListItem
					id={row.entry.item}
					name={row.entry.name}
					summary={row.summary}
					meta={row.meta}
					icon={row.entry.kind === 'connection'
						? connectionIcon(row.entry.id)
						: row.entry.review
							? REVIEW_ICON
							: PACKAGE_ICON}
					status={row.status}
					category={CATEGORY}
				/>
			{/each}
		{/each}
		{#if model.loading}
			<p class="st-empty" role="status">Loading plugins…</p>
		{:else if !model.forbidden && !model.error && installedCount === 0 && !q}
			<p class="st-empty">Nothing added yet.</p>
		{:else if q && !groups.length && !showExplore && !showSources && !showProjects}
			<p class="st-empty">No plugin matches “{filter.trim()}”.</p>
		{/if}
		{#if showExplore || showSources || showProjects}
			<ListGroupLabel label="More" />
			{#if showExplore}
				<ListItem
					id="explore"
					name="Explore plugins"
					summary={exploreSummary}
					icon={EXPLORE_ICON}
					category={CATEGORY}
				/>
			{/if}
			{#if showProjects}
				<ListItem
					id="projects"
					name="Project plugins"
					summary={projectsSummary}
					icon={PROJECTS_ICON}
					category={CATEGORY}
				/>
			{/if}
			{#if showSources}
				<ListItem
					id="sources"
					name="Sources and updates"
					summary={sourcesSummary}
					icon={SOURCES_ICON}
					status={model.updates.length ? 'warn' : undefined}
					category={CATEGORY}
				/>
			{/if}
		{/if}
	{/snippet}
	{#snippet detail()}
		{#if parsed?.kind === 'explore'}
			<ExploreDetail />
		{:else if parsed?.kind === 'sources' && !access.readOnly}
			<SourcesDetail />
		{:else if parsed?.kind === 'projects'}
			<ProjectsDetail />
		{:else if parsed?.kind === 'plugin' && model.entryByItem(selected)}
			{#key parsed.id}
				<PluginDetail id={parsed.id} />
			{/key}
		{:else if parsed?.kind === 'connection' && model.entryByItem(selected)}
			{#key parsed.type}
				<ConnectionDetail type={parsed.type} />
			{/key}
		{:else if parsed?.kind === 'connection' && access.readOnly}
			<EmptyDetail icon={Puzzle} title="Apps" message={FORBIDDEN_TEXT} />
		{:else if selected && model.loading}
			<EmptyDetail icon={Puzzle} message="Loading plugins…" />
		{:else if selected && parsed}
			<EmptyDetail icon={Puzzle} title="Not found" message="This plugin is not available here." />
		{:else}
			<EmptyDetail icon={Puzzle} message="Select a plugin or app." />
		{/if}
	{/snippet}
</CollectionPage>
