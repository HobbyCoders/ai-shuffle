<script lang="ts">
	/**
	 * Settings > Hooks: app hooks, the picked project's hooks, and plugin
	 * hooks (read-only). The project picker starts at the active chat's
	 * project. Hooks are edited with an explicit Save; Enabled saves at once.
	 */
	import { onMount, untrack } from 'svelte';
	import { get } from 'svelte/store';
	import { Puzzle, Webhook } from 'lucide-svelte';
	import { watchResources } from '$lib/services/watch-resources';
	import { activeSession } from '$lib/stores/active-session';
	import { projects } from '$lib/stores/app-data.svelte';
	import { settingsNav } from '$lib/settings/navigation.svelte';
	import { FORBIDDEN_TEXT } from '$lib/settings/access.svelte';
	import { setHookSearchItems } from '$lib/settings/entries/hooks';
	import type { Hook } from '$lib/api/hooks';
	import CollectionPage from '../shell/CollectionPage.svelte';
	import EmptyDetail from '../shell/EmptyDetail.svelte';
	import ListItem from '../rows/ListItem.svelte';
	import ListGroupLabel from '../rows/ListGroupLabel.svelte';
	import InlineBanner from '../rows/InlineBanner.svelte';
	import SelectField from '../rows/SelectField.svelte';
	import HookDetail from './HookDetail.svelte';
	import PluginHookDetail from './PluginHookDetail.svelte';
	import { DRAFT_ID, HooksModel, hookIcon, hookSummary } from './hooks-model.svelte';
	import '../pages/editors/editors.css';

	const CATEGORY = 'hooks' as const;
	const model = new HooksModel();
	let filter = $state('');

	model.projectId = untrack(() => get(activeSession).project) || null;

	onMount(() => void model.load());
	watchResources(['hooks', 'projects'], () => model.load());

	const projectName = $derived($projects.find((p) => p.id === model.projectId)?.name ?? null);
	const projectOptions = $derived([
		{ value: '', label: 'No project' },
		...$projects.map((p) => ({ value: p.id, label: p.name }))
	]);

	$effect(() =>
		setHookSearchItems([
			...model.app.map((h) => ({ id: h.id, name: h.name, event: h.event, where: 'App' })),
			...model.project.map((h) => ({ id: h.id, name: h.name, event: h.event, where: projectName ?? 'Project' })),
			...model.plugin.map((p) => ({ id: p.id, name: p.plugin_name, event: p.event, where: 'Plugins' }))
		])
	);

	const selected = $derived(settingsNav.selectedItem(CATEGORY));
	const q = $derived(filter.trim().toLowerCase());
	const matches = (...parts: (string | null | undefined)[]) =>
		!q || parts.filter(Boolean).join(' ').toLowerCase().includes(q);
	const hookMatches = (h: Hook) => matches(h.name, h.event, h.tool_matcher, h.command, h.url);

	const app = $derived(model.app.filter(hookMatches));
	const project = $derived(model.project.filter(hookMatches));
	const plugin = $derived(model.plugin.filter((p) => matches(p.plugin_name, p.event, p.description, p.tool_matcher, 'plugin')));
	const showDraft = $derived(model.draft !== null && matches(model.draft.name || 'New hook'));
	const total = $derived(model.app.length + model.project.length + model.plugin.length);
	const nothingShown = $derived(!showDraft && app.length + project.length + plugin.length === 0);

	const known = $derived(
		selected === DRAFT_ID
			? !!model.draft
			: !!(selected && (model.hook(selected) || model.forms[selected] || model.pluginHook(selected)))
	);
	const pluginSelected = $derived(selected ? model.pluginHook(selected) : undefined);

	function add() {
		const id = model.addDraft();
		settingsNav.selectItem(id, CATEGORY);
	}

	function status(h: Hook) {
		if (model.dirty(h.id)) return 'warn' as const;
		return h.enabled ? ('ok' as const) : ('off' as const);
	}

	function summary(h: Hook) {
		if (model.dirty(h.id)) return 'Unsaved changes';
		return h.enabled ? hookSummary(h) : `Off, ${hookSummary(h)}`;
	}

	function meta(h: Hook) {
		return h.failure_count ? `${h.failure_count} fail${h.failure_count === 1 ? '' : 's'}` : h.hook_type;
	}
</script>

{#snippet hookRow(h: Hook)}
	<ListItem id={h.id} name={h.name} icon={hookIcon(h.hook_type)} category={CATEGORY} status={status(h)} summary={summary(h)} meta={meta(h)} />
{/snippet}

<CollectionPage
	category={CATEGORY}
	count={total}
	addLabel="Add hook"
	onAdd={add}
	addDisabled={model.forbidden}
	bind:filter
>
	{#snippet toolbar()}
		<span class="proj">
			<SelectField
				label="Project"
				value={model.projectId ?? ''}
				options={projectOptions}
				width={200}
				onchange={(v) => model.setProject(v || null)}
			/>
		</span>
	{/snippet}
	{#snippet list()}
		<div data-setting-anchor="hooks.list">
			{#if model.loadError}
				<InlineBanner tone="warn">
					<span role="alert">{model.loadError}</span>
					{#snippet actions()}
						<button type="button" class="st-btn sm" onclick={() => model.load()}>Retry</button>
					{/snippet}
				</InlineBanner>
			{/if}
			{#if model.forbidden}
				<p class="st-empty">{FORBIDDEN_TEXT}</p>
			{:else if model.loading && !model.loaded}
				<p class="st-empty" role="status">Loading hooks…</p>
			{:else}
				{#if showDraft && model.draft}
					<ListGroupLabel label="New" />
					<ListItem
						id={DRAFT_ID}
						name={model.draft.name.trim() || 'New hook'}
						icon={hookIcon(model.draft.type)}
						category={CATEGORY}
						status="busy"
						summary="Not added yet"
					/>
				{/if}
				{#if app.length || !q}
					<ListGroupLabel label="App" />
					{#each app as h (h.id)}{@render hookRow(h)}{/each}
					{#if !q && model.app.length === 0}<p class="st-empty">No app hooks. They fire in every session.</p>{/if}
				{/if}
				{#if model.projectId && (project.length || !q)}
					<ListGroupLabel label={projectName ?? 'Project'} />
					{#each project as h (h.id)}{@render hookRow(h)}{/each}
					{#if !q && model.project.length === 0}<p class="st-empty">No hooks for this project.</p>{/if}
				{/if}
				{#if plugin.length}
					<ListGroupLabel label="Plugins" />
					{#each plugin as p (p.id)}
						<ListItem
							id={p.id}
							name={p.plugin_name}
							icon={Puzzle}
							category={CATEGORY}
							status="ok"
							summary={hookSummary(p)}
							meta="plugin"
						/>
					{/each}
				{/if}
				{#if q && nothingShown}
					<p class="st-empty">Nothing matches.</p>
				{:else if !model.projectId && !q}
					<p class="st-empty">Pick a project above to see its hooks.</p>
				{/if}
			{/if}
		</div>
	{/snippet}
	{#snippet detail()}
		{#if pluginSelected}
			<PluginHookDetail hook={pluginSelected} />
		{:else if selected && known}
			{#key selected}
				<HookDetail {model} id={selected} />
			{/key}
		{:else if selected && selected !== DRAFT_ID && model.loaded}
			<EmptyDetail icon={Webhook} title="Not found" message="This hook was deleted, or belongs to another project." />
		{:else}
			<EmptyDetail icon={Webhook} message="Select a hook, or add one." />
		{/if}
	{/snippet}
</CollectionPage>

<style>
	.proj {
		display: inline-flex;
		align-items: center;
		gap: 8px;
		min-width: 0;
	}
</style>
