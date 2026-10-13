<script lang="ts">
	/**
	 * Settings > MCP servers: the worker's global MCP servers. Custom servers
	 * are edited here with an explicit Save; built-in ones are read-only.
	 * Turning a server on for one project stays in the Projects card.
	 */
	import { onMount } from 'svelte';
	import { Cable } from 'lucide-svelte';
	import { watchResources } from '$lib/services/watch-resources';
	import { settingsNav } from '$lib/settings/navigation.svelte';
	import { FORBIDDEN_TEXT } from '$lib/settings/access.svelte';
	import { setMcpSearchItems } from '$lib/settings/entries/mcp';
	import CollectionPage from '../shell/CollectionPage.svelte';
	import EmptyDetail from '../shell/EmptyDetail.svelte';
	import ListItem from '../rows/ListItem.svelte';
	import ListGroupLabel from '../rows/ListGroupLabel.svelte';
	import InlineBanner from '../rows/InlineBanner.svelte';
	import McpServerDetail from './McpServerDetail.svelte';
	import { DRAFT_ID, McpModel, detailLine, typeIcon } from './mcp-model.svelte';
	import '../pages/editors/editors.css';

	const CATEGORY = 'mcp' as const;
	const model = new McpModel();
	let filter = $state('');

	onMount(() => void model.load());
	watchResources(['mcp'], () => model.load());

	$effect(() => setMcpSearchItems(model.servers));

	const selected = $derived(settingsNav.selectedItem(CATEGORY));
	const q = $derived(filter.trim().toLowerCase());
	const matches = (...parts: (string | null | undefined)[]) =>
		!q || parts.filter(Boolean).join(' ').toLowerCase().includes(q);

	const custom = $derived(model.custom.filter((s) => matches(s.name, s.description, detailLine(s))));
	const builtins = $derived(model.builtins.filter((s) => matches(s.name, s.description, detailLine(s), 'built-in')));
	const showDraft = $derived(model.draft !== null && matches(model.draft.name || 'New server'));
	// A server deleted elsewhere stays open while its form holds unsaved edits.
	const known = $derived(
		selected === DRAFT_ID ? !!model.draft : !!(selected && (model.server(selected) || model.forms[selected]))
	);

	function add() {
		settingsNav.selectItem(model.addDraft(), CATEGORY);
	}
</script>

<CollectionPage
	category={CATEGORY}
	count={model.servers.length}
	addLabel="Add server"
	filterPlaceholder="Filter servers"
	onAdd={add}
	addDisabled={model.forbidden}
	bind:filter
>
	{#snippet list()}
		<div data-setting-anchor="mcp.servers">
			{#if model.loadError}
				<InlineBanner tone="warn">
					<span role="alert">{model.loadError}</span>
					{#snippet actions()}
						<button type="button" class="st-btn sm" onclick={() => model.load()}>Retry</button>
					{/snippet}
				</InlineBanner>
			{/if}
			{#if showDraft || custom.length}
				<ListGroupLabel label="Custom" />
			{/if}
			{#if showDraft && model.draft}
				<ListItem
					id={DRAFT_ID}
					name={model.draft.name.trim() || 'New server'}
					icon={typeIcon(model.draft.type)}
					category={CATEGORY}
					status="busy"
					summary="Not added yet"
				/>
			{/if}
			{#each custom as s (s.name)}
				<ListItem
					id={s.name}
					name={s.name}
					icon={typeIcon(s.type)}
					category={CATEGORY}
					status={model.dirty(s.name) ? 'warn' : s.enabled ? 'ok' : 'off'}
					summary={model.dirty(s.name) ? 'Unsaved changes' : detailLine(s)}
					meta={s.type}
					mono
				/>
			{/each}
			{#if builtins.length}
				<ListGroupLabel label="Built-in" />
				{#each builtins as s (s.name)}
					<ListItem
						id={s.name}
						name={s.name}
						icon={typeIcon(s.type)}
						category={CATEGORY}
						status={s.enabled ? 'ok' : 'off'}
						summary={detailLine(s)}
						meta="built-in"
						mono
					/>
				{/each}
			{/if}
			{#if model.forbidden}
				<p class="st-empty">{FORBIDDEN_TEXT}</p>
			{:else if model.loading && !model.loaded}
				<p class="st-empty" role="status">Loading servers…</p>
			{:else if model.loaded && model.servers.length === 0 && !model.draft}
				<p class="st-empty">No servers yet.</p>
			{:else if q && custom.length === 0 && builtins.length === 0 && !showDraft}
				<p class="st-empty">Nothing matches.</p>
			{/if}
		</div>
	{/snippet}
	{#snippet detail()}
		{#if selected && known}
			{#key selected}
				<McpServerDetail {model} id={selected} />
			{/key}
		{:else if selected && selected !== DRAFT_ID && model.loaded}
			<EmptyDetail icon={Cable} title="Not found" message="This server was deleted." />
		{:else}
			<EmptyDetail icon={Cable} message="Select a server, or add one." />
		{/if}
	{/snippet}
</CollectionPage>
