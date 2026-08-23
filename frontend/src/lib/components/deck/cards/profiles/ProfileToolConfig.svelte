<script lang="ts">
	/**
	 * ProfileToolConfig — the agent editor's one tool picker (ruling T6).
	 *
	 * Every tool a session can be given comes from `GET /tools/surface`, grouped
	 * by where it lives: the harness builtins, the app's bridged servers, the AI
	 * tools, the browser, computer use, app config, then each configured MCP
	 * server. There is no second card and no hardcoded list — a builtin added
	 * upstream shows up here on the next refresh.
	 *
	 * The mode select says what the checkboxes mean. In "all"/"none" they are
	 * read-only and simply show what the agent gets; in "allow"/"disallow" they
	 * drive that mode's list. Selecting a whole MCP server saves the
	 * `mcp__<server>__*` pattern; unchecking one of its tools expands the
	 * pattern back to names.
	 */
	import { Wrench, ListChecks, Search, RefreshCw, ChevronDown, Check, Minus, AlertTriangle } from 'lucide-svelte';
	import type { ToolSurface, ToolSurfaceGroup, ToolSurfaceTool } from '$lib/api/toolSurface';
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
		/** The profile's own computer-use switch — its group is dead while off. */
		computerUseEnabled: boolean;
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
		computerUseEnabled,
		isAdmin,
		onToolSelectionModeChange,
		onToggleTool,
		onToggleGroup,
		onRefresh
	}: Props = $props();

	let search = $state('');
	// Explicit user expand/collapse; unset groups fall back to the default below.
	let expanded = $state<Record<string, boolean>>({});

	const ctx = $derived<SelectabilityContext>({ computerUseEnabled, isAdmin });
	const editing = $derived(toolSelectionMode === 'allow' || toolSelectionMode === 'disallow');
	const list = $derived(
		toolSelectionMode === 'allow'
			? allowedTools
			: toolSelectionMode === 'disallow'
				? disallowedTools
				: []
	);

	const groups = $derived(sortGroups(surface.groups));
	const errorsByGroup = $derived(
		Object.fromEntries(surface.errors.map((e) => [e.group, e.message]))
	);

	const totalSelectable = $derived(selectableSurfaceTools(groups, ctx).length);
	const allowedCount = $derived(countAllowedTools(toolSelectionMode, groups, list, ctx));

	// Searching is the one time every matching group opens on its own.
	const searching = $derived(search.trim().length > 0);

	function visibleTools(group: ToolSurfaceGroup): ToolSurfaceTool[] {
		return filterGroupTools(group, search);
	}

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
		if (toolSelectionMode === 'all') {
			return selectableTools(group, ctx).length > 0 ? 'all' : 'none';
		}
		if (toolSelectionMode === 'none') return 'none';
		return groupSelectionState(group, list, ctx);
	}

	function selectedInGroup(group: ToolSurfaceGroup): number {
		return group.tools.filter((t) => isChecked(group, t)).length;
	}

	const MODE_HINTS: Record<ToolSelectionMode, string> = {
		all: 'Every discovered tool is available. The list below is read-only.',
		none: 'No tools at all. The list below is read-only.',
		allow: 'Only the checked tools are available.',
		disallow: 'Every tool except the checked ones is available.'
	};
</script>

<div class="set-group">
	<div class="set-grouphead"><h3><Wrench />Tool access</h3><p>Control which tools this agent can use during conversations.</p></div>
	<div class="set-row">
		<div class="lab"><h4>Mode</h4><p>{MODE_HINTS[toolSelectionMode]}</p></div>
		<div class="ctl">
			<select
				class="sel"
				aria-label="Tool access mode"
				value={toolSelectionMode}
				onchange={(e) => onToolSelectionModeChange((e.target as HTMLSelectElement).value as ToolSelectionMode)}
			>
				<option value="all">All Tools Allowed</option>
				<option value="none">Disable All Tools</option>
				<option value="allow">Allow Only Selected</option>
				<option value="disallow">Disallow Selected</option>
			</select>
		</div>
	</div>
</div>

<div class="set-group">
	<div class="set-grouphead ts-head">
		<div class="ts-headtext">
			<h3><ListChecks />Tools</h3>
			<p>
				{allowedCount} of {totalSelectable} tools allowed{#if surface.discovered_at}<span class="hint"> · discovered {new Date(surface.discovered_at).toLocaleTimeString()}</span>{/if}
			</p>
		</div>
		<div class="ts-headacts">
			<div class="ts-searchbox">
				<Search size={14} />
				<input
					class="ts-search"
					type="search"
					placeholder="Search tools…"
					aria-label="Search tools"
					bind:value={search}
				/>
			</div>
			<button
				type="button"
				class="ts-refresh"
				title="Re-discover the tool surface"
				aria-label="Refresh tool list"
				disabled={refreshing}
				onclick={onRefresh}
			>
				<RefreshCw size={14} class={refreshing ? 'ts-spin' : ''} />
			</button>
		</div>
	</div>

	<div class="ts-body">
		{#if loading}
			<div class="ed-note"><span>Discovering tools…</span></div>
		{:else if groups.length === 0}
			<div class="ed-note"><AlertTriangle /><span>No tools were discovered. Try refreshing.</span></div>
		{:else}
			{#each groups as group (group.id)}
				{@const rows = visibleTools(group)}
				{@const open = isExpanded(group, rows.length)}
				{@const state = groupState(group)}
				{@const blocked = groupBlockedReason(group, ctx)}
				{@const error = errorsByGroup[group.id]}
				{#if rows.length > 0 || !searching}
					<div class="ts-group" class:blocked={!!blocked}>
						<div class="ts-grouphead">
							<button
								type="button"
								class="ts-chk"
								class:on={state === 'all'}
								class:mixed={state === 'some'}
								role="checkbox"
								aria-checked={state === 'all' ? 'true' : state === 'some' ? 'mixed' : 'false'}
								aria-label={`Toggle all ${group.label} tools`}
								disabled={!editing || !!blocked}
								onclick={() => onToggleGroup(group)}
							>
								{#if state === 'all'}<Check size={12} />{:else if state === 'some'}<Minus size={12} />{/if}
							</button>
							<button
								type="button"
								class="ts-gtitle"
								aria-expanded={open}
								aria-label={`Toggle ${group.label} section`}
								onclick={() => toggleExpanded(group)}
							>
								<span class="ts-gname">{group.label}</span>
								<span class="hint">{group.tools.length}</span>
								{#if blocked}<span class="pill-s ts-blocked" title={blocked}>unavailable</span>{/if}
								<ChevronDown size={15} class={open ? 'ts-chev open' : 'ts-chev'} />
							</button>
							<span class="pill-s acc">{selectedInGroup(group)}/{group.tools.length}</span>
						</div>

						{#if error}
							<div class="ts-err"><AlertTriangle size={14} /><span>{error}</span></div>
						{/if}
						{#if blocked}
							<div class="ts-reason">{blocked}</div>
						{/if}

						{#if open}
							{#if rows.length === 0}
								<div class="ts-empty">No tools in this group.</div>
							{:else}
								<div class="ts-tools">
									{#each rows as tool (tool.name)}
										{@const reason = toolBlockedReason(group, tool, ctx)}
										{@const checked = isChecked(group, tool)}
										<button
											type="button"
											class="ts-tool"
											class:on={checked}
											role="checkbox"
											aria-checked={checked}
											aria-label={tool.name}
											title={reason ?? tool.description}
											disabled={!editing || !!reason}
											onclick={() => onToggleTool(group, tool.name)}
										>
											<span class="ts-box" class:on={checked}>{#if checked}<Check size={11} />{/if}</span>
											<span class="ts-tmeta">
												<span class="ts-tname">
													{tool.name}
													{#if tool.risk}<span class="ts-risk risk-{tool.risk}">{tool.risk}</span>{/if}
												</span>
												<span class="ts-tdesc">{reason ?? tool.description}</span>
											</span>
										</button>
									{/each}
								</div>
							{/if}
						{/if}
					</div>
				{/if}
			{/each}
		{/if}
	</div>
</div>

<div class="ed-note">
	<Wrench />
	<span>
		{#if toolSelectionMode === 'all'}
			Every discovered tool is available to this agent.
		{:else if toolSelectionMode === 'none'}
			This agent gets no tools at all.
		{:else if toolSelectionMode === 'allow'}
			{#if list.length === 0}
				Nothing selected — an empty allow list leaves this agent with no tools.
			{:else}
				{allowedCount} tool{allowedCount === 1 ? '' : 's'} selected.
			{/if}
		{:else}
			{list.length} entr{list.length === 1 ? 'y' : 'ies'} blocked; {allowedCount} still available.
		{/if}
	</span>
</div>

<style>
	/* ── Section header: title + search + refresh ── */
	.ts-head {
		display: flex;
		align-items: flex-start;
		justify-content: space-between;
		gap: 14px;
		flex-wrap: wrap;
	}
	.ts-headtext {
		min-width: 0;
	}
	.ts-headacts {
		display: flex;
		align-items: center;
		gap: 8px;
		flex-shrink: 0;
	}
	.ts-searchbox {
		display: flex;
		align-items: center;
		gap: 7px;
		height: 34px;
		padding: 0 10px;
		border: 1px solid var(--border);
		border-radius: 9px;
		background: var(--background);
		color: var(--muted-foreground);
	}
	.ts-searchbox:focus-within {
		border-color: var(--ring, var(--primary));
	}
	.ts-search {
		border: none;
		background: none;
		outline: none;
		font: inherit;
		font-size: 12.5px;
		color: var(--foreground);
		width: 170px;
	}
	.ts-search::placeholder {
		color: var(--muted-foreground);
		opacity: 0.7;
	}
	.ts-refresh {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 34px;
		height: 34px;
		border: 1px solid var(--border);
		border-radius: 9px;
		background: var(--background);
		color: var(--muted-foreground);
		cursor: pointer;
		transition: background 0.12s, color 0.12s;
	}
	.ts-refresh:hover:not(:disabled) {
		background: var(--muted);
		color: var(--foreground);
	}
	.ts-refresh:disabled {
		opacity: 0.5;
		cursor: progress;
	}
	.ts-refresh :global(.ts-spin) {
		animation: ts-spin 0.9s linear infinite;
	}
	@keyframes ts-spin {
		to {
			transform: rotate(360deg);
		}
	}

	/* ── Group list ── */
	.ts-body {
		display: flex;
		flex-direction: column;
		gap: 10px;
		padding: 14px 18px 18px;
	}
	.ts-group {
		border: 1px solid var(--border);
		border-radius: 10px;
		background: var(--background);
		overflow: hidden;
	}
	.ts-group.blocked {
		opacity: 0.72;
	}
	.ts-grouphead {
		display: flex;
		align-items: center;
		gap: 10px;
		padding: 10px 12px;
	}
	.ts-gtitle {
		flex: 1;
		min-width: 0;
		display: flex;
		align-items: center;
		gap: 8px;
		background: none;
		border: none;
		padding: 0;
		font: inherit;
		color: var(--foreground);
		text-align: left;
		cursor: pointer;
	}
	.ts-gname {
		font-size: 13px;
		font-weight: 600;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.ts-gtitle :global(.ts-chev) {
		color: var(--muted-foreground);
		flex-shrink: 0;
		transition: transform 0.18s;
	}
	.ts-gtitle :global(.ts-chev.open) {
		transform: rotate(180deg);
	}
	.ts-blocked {
		background: color-mix(in oklch, var(--destructive) 16%, transparent);
		color: var(--foreground);
	}

	/* Tri-state group checkbox */
	.ts-chk {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 18px;
		height: 18px;
		flex-shrink: 0;
		border: 1px solid var(--border);
		border-radius: 5px;
		background: var(--background);
		color: var(--primary-foreground);
		cursor: pointer;
		transition: background 0.12s, border-color 0.12s;
	}
	.ts-chk.on,
	.ts-chk.mixed {
		background: var(--primary);
		border-color: var(--primary);
	}
	.ts-chk:disabled {
		cursor: not-allowed;
		opacity: 0.55;
	}

	.ts-err {
		display: flex;
		align-items: center;
		gap: 8px;
		margin: 0 12px 10px;
		padding: 8px 10px;
		border-radius: 8px;
		font-size: 12px;
		color: var(--foreground);
		background: color-mix(in oklch, var(--destructive) 14%, transparent);
	}
	.ts-reason,
	.ts-empty {
		margin: 0 12px 10px;
		font-size: 11.5px;
		color: var(--muted-foreground);
	}

	/* ── Tool rows ── */
	.ts-tools {
		display: flex;
		flex-direction: column;
		border-top: 1px solid var(--border);
	}
	.ts-tool {
		display: flex;
		align-items: flex-start;
		gap: 10px;
		width: 100%;
		padding: 9px 12px;
		background: none;
		border: none;
		border-bottom: 1px solid var(--border);
		font: inherit;
		color: var(--foreground);
		text-align: left;
		cursor: pointer;
		transition: background 0.12s;
	}
	.ts-tool:last-child {
		border-bottom: none;
	}
	.ts-tool:hover:not(:disabled) {
		background: var(--muted);
	}
	.ts-tool:disabled {
		cursor: not-allowed;
		opacity: 0.55;
	}
	.ts-box {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 16px;
		height: 16px;
		margin-top: 1px;
		flex-shrink: 0;
		border: 1px solid var(--border);
		border-radius: 4px;
		color: var(--primary-foreground);
	}
	.ts-box.on {
		background: var(--primary);
		border-color: var(--primary);
	}
	.ts-tmeta {
		min-width: 0;
		display: flex;
		flex-direction: column;
		gap: 2px;
	}
	.ts-tname {
		display: flex;
		align-items: center;
		gap: 7px;
		font-size: 12.5px;
		font-weight: 600;
		font-family: var(--font-mono, monospace);
	}
	.ts-tdesc {
		font-size: 11.5px;
		color: var(--muted-foreground);
		line-height: 1.45;
	}
	.ts-risk {
		font-family: var(--font-mono, monospace);
		font-size: 9.5px;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		padding: 2px 5px;
		border-radius: 4px;
		background: var(--muted);
		color: var(--muted-foreground);
	}
	.ts-risk.risk-exec {
		background: color-mix(in oklch, var(--destructive) 20%, transparent);
		color: var(--foreground);
	}
	.ts-risk.risk-write {
		background: color-mix(in oklch, var(--primary) 22%, transparent);
		color: var(--foreground);
	}

	@media (max-width: 620px) {
		.ts-search {
			width: 110px;
		}
	}
</style>
