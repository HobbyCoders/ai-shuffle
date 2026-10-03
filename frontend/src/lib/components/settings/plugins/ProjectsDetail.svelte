<script lang="ts">
	/**
	 * ProjectsDetail: plugins found in project folders (`.fluid/plugins`,
	 * which git ignores, and `plugins/`, which is committed), grouped by
	 * project, with Remove.
	 *
	 * Chats load plugins from this device's plugin folder only, so a project
	 * plugin does not run yet. The page says so instead of offering a switch
	 * that would change nothing.
	 */
	import { onMount } from 'svelte';
	import { RefreshCw } from 'lucide-svelte';
	import { getSettingsAccess } from '$lib/settings/access.svelte';
	import DetailPane from '../shell/DetailPane.svelte';
	import type { Fact } from '../shell/FactsStrip.svelte';
	import RowGroup from '../rows/RowGroup.svelte';
	import SettingRow from '../rows/SettingRow.svelte';
	import ValueText from '../rows/ValueText.svelte';
	import InlineBanner from '../rows/InlineBanner.svelte';
	import InlineConfirm from '../rows/InlineConfirm.svelte';
	import { getPluginsModel } from './plugins-model.svelte';
	import { PROJECTS_ICON } from './icons';
	import type { ProjectPlugin } from '$lib/api/plugins';

	const model = getPluginsModel();
	const access = getSettingsAccess();

	onMount(() => {
		void model.loadProjectPlugins();
	});

	const total = $derived(model.projectPlugins.reduce((n, r) => n + r.plugins.length, 0));
	const facts = $derived<Fact[]>([
		{ label: 'Projects', value: model.projectsLoaded ? model.projectPlugins.length : null },
		{ label: 'Plugins', value: model.projectsLoaded ? total : null }
	]);

	function folder(p: ProjectPlugin): string {
		return p.scope === 'local' ? 'In .fluid/plugins' : 'In plugins/, committed';
	}
</script>

{#snippet primary()}
	<button type="button" class="st-btn" disabled={model.projectsLoading} onclick={() => model.loadProjectPlugins()}>
		<RefreshCw />Refresh
	</button>
{/snippet}

{#snippet banner()}
	{#if model.actionError}
		<InlineBanner tone="warn"><span role="alert">{model.actionError}</span></InlineBanner>
	{/if}
	{#if model.projectsError}
		<InlineBanner tone="warn">
			<span role="alert">{model.projectsError}</span>
			{#snippet actions()}
				<button type="button" class="st-btn sm" onclick={() => model.loadProjectPlugins()}>Retry</button>
			{/snippet}
		</InlineBanner>
	{:else if total}
		<InlineBanner tone="info">Chats don't load these yet.</InlineBanner>
	{/if}
{/snippet}

<DetailPane
	name="Project plugins"
	icon={PROJECTS_ICON}
	subtitle="Plugins inside project folders"
	{primary}
	{facts}
	{banner}
	category="plugins"
	item="projects"
>
	{#if model.projectsLoading && !model.projectsLoaded}
		<p class="st-empty" role="status">Loading projects…</p>
	{:else if model.projectsLoaded && !model.projectPlugins.length}
		<p class="st-empty" data-testid="project-plugins-empty">No project has plugins.</p>
	{:else}
		{#each model.projectPlugins as row (row.project.id)}
			<RowGroup title={row.project.name} count={row.plugins.length || undefined}>
				{#if row.error}
					<SettingRow label="Could not read this project" hint={row.error} />
				{/if}
				{#each row.plugins as p (p.id)}
					<SettingRow label={p.name} hint={folder(p)}>
						<ValueText value="Not loaded" />
						{#if !access.readOnly}
							<InlineConfirm
								label="Remove"
								question="Delete its folder?"
								confirmLabel="Delete"
								variant="ghost"
								size="sm"
								disabled={!!model.busy}
								onConfirm={async () => {
									await model.removeProjectPlugin(row.project.id, p.id);
								}}
							/>
						{/if}
					</SettingRow>
				{/each}
			</RowGroup>
		{/each}
	{/if}
</DetailPane>

<style>
	.st-empty {
		padding: 16px 2px;
	}
</style>
