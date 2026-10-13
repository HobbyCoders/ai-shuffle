<script lang="ts">
	/**
	 * ProfileToolConfig — the agent editor's one tool picker (ruling T6), the
	 * Agents page's Tools tab.
	 *
	 * Every tool a session can be given comes from `GET /tools/surface`, grouped
	 * by where it lives: the harness builtins, the app's bridged servers, the AI
	 * tools, the browser, computer use, app config, then each configured MCP
	 * server (in its own card). There is no hardcoded list — a builtin added
	 * upstream shows up here on the next refresh.
	 *
	 * The access control says what the checkboxes mean. In "all"/"none" they are
	 * read-only and simply show what the agent gets; in "allow"/"disallow" they
	 * drive that mode's list. Selecting a whole MCP server saves the
	 * `mcp__<server>__*` pattern; unchecking one of its tools expands the
	 * pattern back to names.
	 */
	import { Search, RefreshCw, ChevronDown, Check, Minus, TriangleAlert } from 'lucide-svelte';
	import { computerUse } from '$lib/stores/computerUse.svelte';
	import type { ToolSurface, ToolSurfaceGroup, ToolSurfaceTool } from '$lib/api/toolSurface';
	import RowGroup from '$lib/components/settings/rows/RowGroup.svelte';
	import SettingRow from '$lib/components/settings/rows/SettingRow.svelte';
	import InlineBanner from '$lib/components/settings/rows/InlineBanner.svelte';
	import {
		type SelectabilityContext,
		type ToolSelectionMode,
		countAllowedTools,
		filterGroupTools,
		groupBlockedReason,
		groupSelectionState,
		listMatches,
		selectableSurfaceTools,
		selectableTools,
		sortGroups,
		toolBlockedReason
	} from './toolSurface';

	interface Props {
		toolSelectionMode: ToolSelectionMode;
		allowedTools: string[];
		disallowedTools: string[];
		surface: ToolSurface;
		loading?: boolean;
		refreshing?: boolean;
		/** Signed-in user's admin flag; admin-only groups are read-only otherwise. */
		isAdmin: boolean;
		onToolSelectionModeChange: (mode: ToolSelectionMode) => void;
		onToggleTool: (group: ToolSurfaceGroup, toolName: string) => void;
		onToggleGroup: (group: ToolSurfaceGroup) => void;
		onRefresh: () => void;
	}

	let {
		toolSelectionMode,
		allowedTools,
		disallowedTools,
		surface,
		loading = false,
		refreshing = false,
		isAdmin,
		onToolSelectionModeChange,
		onToggleTool,
		onToggleGroup,
		onRefresh
	}: Props = $props();

	let search = $state('');
	// Explicit user expand/collapse; unset groups fall back to the default below.
	let expanded = $state<Record<string, boolean>>({});

	const ctx = $derived<SelectabilityContext>({ computerUseEnabled: computerUse.enabled, isAdmin });
	const editing = $derived(toolSelectionMode === 'allow' || toolSelectionMode === 'disallow');
	const list = $derived(
		toolSelectionMode === 'allow' ? allowedTools : toolSelectionMode === 'disallow' ? disallowedTools : []
	);

	const groups = $derived(sortGroups(surface.groups));
	const appGroups = $derived(groups.filter((g) => g.kind !== 'mcp'));
	const mcpGroups = $derived(groups.filter((g) => g.kind === 'mcp'));
	const errorsByGroup = $derived(Object.fromEntries(surface.errors.map((e) => [e.group, e.message])));

	const totalSelectable = $derived(selectableSurfaceTools(groups, ctx).length);
	const allowedCount = $derived(countAllowedTools(toolSelectionMode, groups, list, ctx));

	// Searching is the one time every matching group opens on its own.
	const searching = $derived(search.trim().length > 0);
	const anyMatch = $derived(!searching || groups.some((g) => filterGroupTools(g, search).length > 0));

	function isExpanded(group: ToolSurfaceGroup, matches: number): boolean {
		if (searching) return matches > 0;
		return expanded[group.id] ?? group.kind === 'harness';
	}

	function toggleExpanded(group: ToolSurfaceGroup) {
		const current = expanded[group.id] ?? group.kind === 'harness';
		expanded = { ...expanded, [group.id]: !current };
	}

	/** What the row's checkbox shows — in all/none it reports, it does not edit. */
	function isChecked(group: ToolSurfaceGroup, tool: ToolSurfaceTool): boolean {
		if (toolBlockedReason(group, tool, ctx)) return false;
		if (toolSelectionMode === 'all') return true;
		if (toolSelectionMode === 'none') return false;
		return listMatches(list, tool.name);
	}

	function groupState(group: ToolSurfaceGroup): 'all' | 'some' | 'none' {
		if (toolSelectionMode === 'all') return selectableTools(group, ctx).length > 0 ? 'all' : 'none';
		if (toolSelectionMode === 'none') return 'none';
		return groupSelectionState(group, list, ctx);
	}

	function selectedInGroup(group: ToolSurfaceGroup): number {
		return group.tools.filter((t) => isChecked(group, t)).length;
	}

	/** Strip the `mcp__server__` prefix inside a server's own card. */
	function shortName(group: ToolSurfaceGroup, name: string): string {
		const server = group.server?.trim();
		return group.kind === 'mcp' && server && name.startsWith(`mcp__${server}__`)
			? name.slice(`mcp__${server}__`.length)
			: name;
	}

	const MODE_HINTS: Record<ToolSelectionMode, string> = {
		all: 'Every tool. Read-only list.',
		none: 'No tools at all.',
		allow: 'Only the checked tools.',
		disallow: 'All but the checked tools.'
	};
	const MODE_OPTIONS: { id: ToolSelectionMode; label: string }[] = [
		{ id: 'all', label: 'All' },
		{ id: 'allow', label: 'Only selected' },
		{ id: 'disallow', label: 'All except selected' },
		{ id: 'none', label: 'None' }
	];

	function modeKey(event: KeyboardEvent) {
		const keys = ['ArrowRight', 'ArrowDown', 'ArrowLeft', 'ArrowUp'];
		if (!keys.includes(event.key)) return;
		event.preventDefault();
		const i = MODE_OPTIONS.findIndex((o) => o.id === toolSelectionMode);
		const step = event.key === 'ArrowRight' || event.key === 'ArrowDown' ? 1 : -1;
		const next = MODE_OPTIONS[(i + step + MODE_OPTIONS.length) % MODE_OPTIONS.length];
		onToolSelectionModeChange(next.id);
		const group = (event.currentTarget as HTMLElement).closest('[role="radiogroup"]');
		queueMicrotask(() => group?.querySelector<HTMLElement>(`[data-mode="${next.id}"]`)?.focus());
	}

	function hintFor(group: ToolSurfaceGroup, blocked: string | null, error: string | undefined): string {
		if (error) return error;
		if (blocked) return blocked;
		return group.kind === 'mcp' ? 'MCP server' : 'Included with the app';
	}
</script>

{#snippet groupRows(items: ToolSurfaceGroup[])}
	{#each items as group (group.id)}
		{@const rows = filterGroupTools(group, search)}
		{@const open = isExpanded(group, rows.length)}
		{@const state = groupState(group)}
		{@const blocked = groupBlockedReason(group, ctx)}
		{@const error = errorsByGroup[group.id]}
		{#if rows.length > 0 || !searching}
			<div class="tg" class:blocked={!!blocked || (!!error && group.tools.length === 0)}>
				<button
					type="button"
					class="chk"
					class:on={state === 'all'}
					class:mixed={state === 'some'}
					role="checkbox"
					aria-checked={state === 'all' ? 'true' : state === 'some' ? 'mixed' : 'false'}
					aria-label={`Toggle all ${group.label}${/tools$/i.test(group.label) ? '' : ' tools'}`}
					disabled={!editing || !!blocked}
					onclick={() => onToggleGroup(group)}
				>
					{#if state === 'all'}<Check />{:else if state === 'some'}<Minus />{/if}
				</button>
				<button
					type="button"
					class="tt"
					aria-expanded={open}
					aria-label={`Toggle ${group.label} section`}
					onclick={() => toggleExpanded(group)}
				>
					<span class="tn">{group.label}</span>
					<span class="th" class:err={!!error}>
						{#if error}<TriangleAlert />{/if}{hintFor(group, blocked, error)}
					</span>
				</button>
				<span class="cnt">{selectedInGroup(group)}/{group.tools.length}</span>
				<span class="chev" class:open aria-hidden="true"><ChevronDown /></span>
			</div>
			{#if open}
				{#if rows.length === 0}
					<p class="tempty">No tools here.</p>
				{:else}
					<div class="tools">
						{#each rows as tool (tool.name)}
							{@const reason = toolBlockedReason(group, tool, ctx)}
							{@const checked = isChecked(group, tool)}
							<button
								type="button"
								class="tool"
								role="checkbox"
								aria-checked={checked}
								aria-label={tool.name}
								title={reason ?? tool.description}
								disabled={!editing || !!reason}
								onclick={() => onToggleTool(group, tool.name)}
							>
								<span class="chk box" class:on={checked} aria-hidden="true">{#if checked}<Check />{/if}</span>
								<span class="tmeta">
									<span class="tname">
										{shortName(group, tool.name)}
										{#if tool.risk}<span class="risk {tool.risk}">{tool.risk}</span>{/if}
									</span>
									<span class="tdesc">{reason ?? tool.description}</span>
								</span>
							</button>
						{/each}
					</div>
				{/if}
			{/if}
		{/if}
	{/each}
{/snippet}

<div class="ptc">
	<RowGroup title="Access">
		<SettingRow label="Which tools" hint={MODE_HINTS[toolSelectionMode]}>
			<div class="st-seg" role="radiogroup" aria-label="Tool access mode">
				{#each MODE_OPTIONS as opt (opt.id)}
					<button
						type="button"
						role="radio"
						data-mode={opt.id}
						aria-checked={toolSelectionMode === opt.id}
						tabindex={toolSelectionMode === opt.id ? 0 : -1}
						onclick={() => onToolSelectionModeChange(opt.id)}
						onkeydown={modeKey}>{opt.label}</button
					>
				{/each}
			</div>
		</SettingRow>
	</RowGroup>

	{#if toolSelectionMode === 'allow' && list.length === 0}
		<InlineBanner>Nothing checked, so no tools.</InlineBanner>
	{/if}

	<RowGroup title="Tools" count={loading ? undefined : `${allowedCount} of ${totalSelectable}`}>
		{#snippet actions()}
			<label class="tsearch">
				<Search />
				<span class="st-vh">Search tools</span>
				<input type="search" placeholder="Search tools" aria-label="Search tools" bind:value={search} />
			</label>
			<button
				type="button"
				class="st-btn ghost icon sm"
				title="Rediscover tools"
				aria-label="Refresh tool list"
				disabled={refreshing}
				onclick={onRefresh}
			>
				<RefreshCw class={refreshing ? 'spin' : ''} />
			</button>
		{/snippet}
		{#if loading}
			<p class="tnote" role="status">Discovering tools…</p>
		{:else if groups.length === 0}
			<p class="tnote">No tools found. Try refresh.</p>
		{:else if appGroups.some((g) => !searching || filterGroupTools(g, search).length > 0)}
			{@render groupRows(appGroups)}
		{:else}
			<p class="tnote">No matches here.</p>
		{/if}
	</RowGroup>

	{#if !loading && mcpGroups.length > 0 && (!searching || mcpGroups.some((g) => filterGroupTools(g, search).length > 0))}
		<RowGroup title="MCP servers" count={mcpGroups.length}>
			{@render groupRows(mcpGroups)}
		</RowGroup>
	{/if}

	{#if searching && !anyMatch}
		<p class="st-note">No tools match “{search.trim()}”.</p>
	{:else if !editing}
		<p class="st-note">Pick “Only selected” or “All except” to edit.</p>
	{/if}
	{#if surface.discovered_at && !loading}
		<p class="st-note">Discovered {new Date(surface.discovered_at).toLocaleTimeString()}.</p>
	{/if}
</div>

<style>
	/* Segmented radios: the Settings Segmented look, with radio semantics. */
	.st-seg {
		display: inline-flex;
		background: var(--st-muted);
		border: 1px solid var(--st-border);
		border-radius: var(--st-radius-md);
		padding: 2px;
		gap: 2px;
		max-width: 100%;
		overflow-x: auto;
		scrollbar-width: none;
	}
	.st-seg::-webkit-scrollbar {
		display: none;
	}
	.st-seg button {
		border: 0;
		background: none;
		border-radius: var(--st-radius-sm);
		height: 26px;
		padding: 0 11px;
		font-size: 13px;
		color: var(--st-mfg);
		white-space: nowrap;
	}
	.st-seg button:hover:not([aria-checked='true']) {
		color: var(--st-fg);
	}
	.st-seg button[aria-checked='true'] {
		background: var(--st-card);
		color: var(--st-fg);
		box-shadow: 0 0 0 1px var(--st-border-strong);
	}

	/* Group rows */
	.tg {
		display: grid;
		grid-template-columns: auto minmax(0, 1fr) auto auto;
		align-items: center;
		gap: 12px;
		min-height: 52px;
		padding: 8px 14px 8px 16px;
	}
	.tg:not(:first-child) {
		border-top: 1px solid var(--st-border);
	}
	.tt {
		display: grid;
		gap: 1px;
		min-width: 0;
		text-align: left;
		border: 0;
		background: none;
		padding: 0;
		color: inherit;
	}
	.tn {
		font-size: 14px;
		font-weight: 450;
	}
	.th {
		font-size: 12.5px;
		color: var(--st-mfg);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
		display: flex;
		align-items: center;
		gap: 5px;
	}
	.th.err {
		color: var(--st-warning);
	}
	.th :global(svg.lucide) {
		width: 13px;
		height: 13px;
	}
	.cnt {
		font-size: 12px;
		color: var(--st-mfg);
		font-variant-numeric: tabular-nums;
		font-family: var(--st-mono);
	}
	.chev {
		display: grid;
		place-items: center;
		width: 22px;
		color: var(--st-mfg);
		transition: transform 0.15s;
	}
	.chev.open {
		transform: rotate(180deg);
	}
	.tg.blocked .tn,
	.tg.blocked .cnt {
		opacity: 0.55;
	}

	/* Checkboxes */
	.chk {
		width: 18px;
		height: 18px;
		display: grid;
		place-items: center;
		flex: none;
		border-radius: 5px;
		border: 1.5px solid var(--st-border-strong);
		background: transparent;
		padding: 0;
		color: var(--st-pfg);
	}
	.chk.on,
	.chk.mixed {
		background: var(--st-primary);
		border-color: var(--st-primary);
	}
	.chk :global(svg.lucide) {
		width: 12px;
		height: 12px;
		stroke-width: 3;
	}
	.chk:disabled {
		opacity: 0.55;
	}
	.chk.box {
		width: 16px;
		height: 16px;
		border-radius: 4px;
		margin-top: 1px;
	}

	/* Tool cells */
	.tools {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
		gap: 2px 14px;
		padding: 0 12px 12px 46px;
	}
	.tool {
		display: grid;
		grid-template-columns: auto minmax(0, 1fr);
		gap: 10px;
		align-items: start;
		padding: 7px 6px;
		border: 0;
		background: none;
		text-align: left;
		border-radius: var(--st-radius-sm);
		color: inherit;
		min-width: 0;
	}
	.tool:hover:not(:disabled) {
		background: var(--st-hover);
	}
	.tool:disabled {
		opacity: 0.55;
	}
	.tool:focus-visible {
		outline-offset: -2px;
	}
	.tmeta {
		display: grid;
		gap: 2px;
		min-width: 0;
	}
	.tname {
		font-family: var(--st-mono);
		font-size: 12px;
		display: flex;
		align-items: center;
		gap: 6px;
		flex-wrap: wrap;
		overflow-wrap: anywhere;
	}
	.tdesc {
		font-size: 12px;
		color: var(--st-mfg);
		line-height: 1.4;
		display: -webkit-box;
		-webkit-line-clamp: 2;
		line-clamp: 2;
		-webkit-box-orient: vertical;
		overflow: hidden;
	}
	.risk {
		font-family: inherit;
		font-size: 10px;
		font-weight: 600;
		padding: 0 6px;
		border-radius: 999px;
		line-height: 16px;
		background: var(--st-muted);
		color: var(--st-mfg);
	}
	.risk.read {
		color: var(--blue);
		background: color-mix(in oklch, var(--blue) 14%, transparent);
	}
	.risk.write {
		color: var(--warning);
		background: color-mix(in oklch, var(--warning) 14%, transparent);
	}
	.risk.exec {
		color: var(--rose);
		background: color-mix(in oklch, var(--rose) 14%, transparent);
	}
	.risk.network {
		color: var(--tool-glob);
		background: color-mix(in oklch, var(--tool-glob) 14%, transparent);
	}

	/* Header search */
	.tsearch {
		display: flex;
		align-items: center;
		gap: 6px;
		height: 28px;
		width: 170px;
		padding: 0 9px;
		border: 1px solid var(--st-input);
		background: var(--st-muted);
		border-radius: var(--st-radius-md);
		color: var(--st-mfg);
	}
	.tsearch:focus-within {
		border-color: var(--st-primary);
		box-shadow: 0 0 0 3px var(--st-primary-soft);
	}
	.tsearch :global(svg.lucide) {
		width: 14px;
		height: 14px;
	}
	.tsearch input {
		flex: 1;
		min-width: 0;
		border: 0;
		background: none;
		outline: none;
		font-size: 12.5px;
		color: var(--st-fg);
	}
	.tsearch input::placeholder {
		color: var(--st-subtle);
	}
	.tsearch input::-webkit-search-cancel-button {
		-webkit-appearance: none;
	}
	.ptc :global(.spin) {
		animation: ptc-spin 0.9s linear infinite;
	}
	@keyframes ptc-spin {
		to {
			transform: rotate(360deg);
		}
	}
	.tnote,
	.tempty {
		margin: 0;
		padding: 14px 16px;
		font-size: 13px;
		color: var(--st-mfg);
	}
	.tempty {
		padding: 0 16px 12px 46px;
		font-size: 12px;
	}
	.st-note {
		margin: 8px 2px 0;
		font-size: 12.5px;
		color: var(--st-mfg);
	}

	@container (max-width: 560px) {
		.tg {
			padding-left: 14px;
		}
		.tools {
			grid-template-columns: minmax(0, 1fr);
			padding-left: 44px;
		}
		.tsearch {
			width: 130px;
		}
	}
	@media (prefers-reduced-motion: reduce) {
		.chev {
			transition: none;
		}
		.ptc :global(.spin) {
			animation: none;
		}
	}
</style>
