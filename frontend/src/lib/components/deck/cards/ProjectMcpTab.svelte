<script lang="ts">
	import Switch from '$lib/components/settings/rows/Switch.svelte';
	import { watchResources } from '$lib/services/watch-resources';
	/** MCP servers across user, installed, local and project scopes. */
	import { untrack } from 'svelte';
	import { mcp, projectMcpServers, mcpLoading, mcpError } from '$lib/stores/mcp.svelte';
	import type { McpServerInfo } from '$lib/api/mcp';
	import McpServerForm from '$lib/components/mcp/McpServerForm.svelte';
	import RowActionMenu from '$lib/components/ui/RowActionMenu.svelte';
	import ConfirmDialog from '$lib/components/ui/ConfirmDialog.svelte';
	import { RowGroup, SettingRow, PanelSearch, EmptyDetail, InlineBanner } from '$lib/components/layout/view';
	import { Search, Server, Globe, FolderGit2, FolderLock, Download, Trash2, Pencil } from 'lucide-svelte';
	import './project-tabs.css';

	interface Props {
		projectId: string;
	}

	let { projectId }: Props = $props();

	// Local state
	let searchQuery = $state('');

	// MCP edit state
	let showMcpServerForm = $state(false);
	let editingMcpServer = $state<McpServerInfo | null>(null);
	let mcpSelectedScope = $state<'installed' | 'local' | 'project'>('local');

	// Touch/narrow only: which toggle-row has its collapsed ⋮ tray expanded (one
	// at a time; desktop keeps the always-inline hover rail). Keyed by scope:name
	// since a name can repeat across scopes.
	let actionsOpenKey = $state<string | null>(null);
	function rowKey(server: McpServerInfo): string {
		return `${server.scope}:${server.name}`;
	}
	function toggleRowActions(e: MouseEvent | KeyboardEvent, key: string) {
		e.stopPropagation();
		actionsOpenKey = actionsOpenKey === key ? null : key;
	}
	function onWindowClick() {
		if (actionsOpenKey !== null) actionsOpenKey = null;
	}
	function onWindowKeydown(e: KeyboardEvent) {
		if (e.key === 'Escape') actionsOpenKey = null;
	}

	// The project whose servers the store holds, as far as this tab knows: set
	// when this tab's newest load for the current project resolves. Until then
	// (right after a switch, or when that load fails) the list is empty and no
	// row can act, so the previous project's names are never shown or sent
	// with the new project's id. A slower load for a project already left is
	// ignored (the store also drops its late response).
	let listedProjectId = $state<string | null>(null);
	const listed = $derived(listedProjectId !== null && listedProjectId === projectId);
	let loadSeq = 0;
	async function reload(id: string): Promise<void> {
		const seq = ++loadSeq;
		await mcp.loadProjectServers(id);
		if (seq === loadSeq && id === projectId) listedProjectId = id;
	}
	/** After an action for ``id`` finished: if the user switched meanwhile, the
	 *  action's own reload (or optimistic update) left ``id``'s servers in the
	 *  store, so fetch the current project's again. */
	function resyncIfSwitched(id: string) {
		if (id !== projectId) reload(projectId).catch(() => {});
	}

	// Load MCP servers when project changes. An open editor, a pending removal,
	// an open row tray and the search belong to the previous project, so drop them.
	let loadedProjectId: string | null = null;
	$effect(() => {
		const id = projectId;
		if (!id) return;
		untrack(() => {
			if (loadedProjectId !== null && loadedProjectId !== id) {
				showMcpServerForm = false;
				editingMcpServer = null;
				pendingRemove = null;
				actionsOpenKey = null;
				searchQuery = '';
			}
			loadedProjectId = id;
			// A failed load is shown through $mcpError.
			reload(id).catch(() => {});
		});
	});

	// Filter servers by search
	function filterServers(servers: McpServerInfo[]): McpServerInfo[] {
		if (!searchQuery.trim()) return servers;
		const q = searchQuery.toLowerCase();
		return servers.filter(s =>
			s.name.toLowerCase().includes(q) ||
			s.description?.toLowerCase().includes(q) ||
			s.command?.toLowerCase().includes(q) ||
			s.url?.toLowerCase().includes(q)
		);
	}

	const EMPTY: McpServerInfo[] = [];
	const shown = $derived(
		listed ? $projectMcpServers : { user: EMPTY, installed: EMPTY, local: EMPTY, project: EMPTY }
	);
	const filteredUserServers = $derived(() => filterServers(shown.user || []));
	const filteredInstalledServers = $derived(() => filterServers(shown.installed || []));
	const filteredLocalServers = $derived(() => filterServers(shown.local || []));
	const filteredProjectServers = $derived(() => filterServers(shown.project || []));

	const totalServers = $derived(
		(shown.user?.length || 0) +
		(shown.installed?.length || 0) +
		(shown.local?.length || 0) +
		(shown.project?.length || 0)
	);

	// Toggle server enabled/disabled for this project
	async function handleToggle(server: McpServerInfo) {
		if (!listed) return;
		const id = projectId;
		try {
			await mcp.toggleProjectServer(id, server.name, !server.enabled);
		} catch (e) {
			console.error('Failed to toggle MCP server:', e);
		}
		resyncIfSwitched(id);
	}

	// Remove server (after confirmation: a global server is deleted everywhere)
	let pendingRemove = $state<McpServerInfo | null>(null);
	async function confirmRemove() {
		const server = pendingRemove;
		pendingRemove = null;
		if (server && listed) await handleRemoveServer(server);
	}
	async function handleRemoveServer(server: McpServerInfo) {
		const id = projectId;
		try {
			await mcp.removeProjectServer(id, server.name, server.scope as 'user' | 'installed' | 'local' | 'project');
		} catch (e) {
			console.error('Failed to remove MCP server:', e);
		}
		resyncIfSwitched(id);
	}

	// Add new local/project server
	export function openAddForm() {
		editingMcpServer = null;
		mcpSelectedScope = 'local';
		showMcpServerForm = true;
	}

	// Edit local/project/installed server
	function handleEditServer(server: McpServerInfo) {
		if (!listed) return;
		editingMcpServer = server;
		mcpSelectedScope = server.scope as 'installed' | 'local' | 'project';
		showMcpServerForm = true;
	}

	async function handleSaveMcpServer(config: {
		name: string;
		type: 'stdio' | 'sse' | 'http';
		command?: string;
		args?: string[];
		url?: string;
		env?: Record<string, string>;
		headers?: Record<string, string>;
		description?: string;
		scope?: 'installed' | 'local' | 'project';
	}) {
		const id = projectId;
		try {
			const scope = config.scope || mcpSelectedScope;
			if (editingMcpServer) {
				await mcp.updateProjectServer(
					id,
					editingMcpServer.name,
					config,
					scope
				);
			} else {
				await mcp.addProjectServer(id, {
					...config,
					scope: scope as 'installed' | 'local' | 'project'
				});
			}
			if (id === projectId) {
				showMcpServerForm = false;
				editingMcpServer = null;
			}
		} catch (e) {
			console.error('Failed to save MCP server:', e);
		}
		resyncIfSwitched(id);
	}

	// Server type label helper
	function getTypeLabel(type: string): string {
		switch (type) {
			case 'stdio': return 'stdio';
			case 'sse': return 'SSE';
			case 'http': return 'HTTP';
			default: return type;
		}
	}
	let remoteForm: McpServerForm | undefined = $state();
	watchResources(['mcp'], async () => {
		const selected = editingMcpServer;
		const id = projectId;
		await reload(id);
		if (id !== projectId || !selected || editingMcpServer !== selected || !showMcpServerForm) return;
		const fresh = Object.values($projectMcpServers).flat().find((server) => server.name === selected.name && server.scope === selected.scope);
		if (remoteForm?.canRefreshRemote(!fresh)) {
			if (fresh) editingMcpServer = fresh;
			else { showMcpServerForm = false; editingMcpServer = null; }
		}
	});
</script>
<svelte:window onclick={onWindowClick} onkeydown={onWindowKeydown} />
{#snippet serverRow(server: McpServerInfo, editable: boolean)}
	<SettingRow label={server.name} hint={`${getTypeLabel(server.type)}${server.builtin ? ' · Built-in' : ''}`}>
		{#snippet expanded()}
			{#if server.command || server.url}<p class="project-code">{server.command ? `${server.command}${server.args ? ' ' + server.args.join(' ') : ''}` : server.url}</p>{/if}
		{/snippet}
		{#if editable}
			<button class="st-btn icon" title="Edit server" aria-label={`Edit ${server.name}`} onclick={() => handleEditServer(server)}><Pencil /></button>
		{:else}
			<Switch controlled size="sm" checked={server.enabled} label={`${server.enabled ? 'Disable' : 'Enable'} ${server.name}`} title={server.enabled ? 'Disable server' : 'Enable server'} onchange={() => handleToggle(server)} />
			{#if !server.builtin}
				<RowActionMenu open={actionsOpenKey === rowKey(server)} onToggle={(e) => toggleRowActions(e, rowKey(server))} label="Server actions">
					<button type="button" class="st-btn icon danger" title="Remove server" aria-label={`Remove ${server.name}`} onclick={(e) => { e.stopPropagation(); actionsOpenKey = null; pendingRemove = server; }}><Trash2 /></button>
				</RowActionMenu>
			{/if}
		{/if}
	</SettingRow>
{/snippet}
{#if totalServers > 3}<PanelSearch bind:value={searchQuery} label="Search MCP servers" placeholder="Search MCP servers" />{/if}
{#if $mcpError}<InlineBanner>{$mcpError}{#snippet actions()}<button class="st-btn sm" onclick={() => mcp.clearError()}>Dismiss</button>{/snippet}</InlineBanner>{/if}
{#if !listed && !$mcpError}<p class="project-muted" role="status">Loading MCP servers…</p>{/if}
{#if filteredUserServers().length}<RowGroup title="Global MCP servers">{#snippet actions()}<code class="project-muted">~/.claude.json</code>{/snippet}{#each filteredUserServers() as server (server.name)}{@render serverRow(server, false)}{/each}</RowGroup>{/if}
{#if filteredInstalledServers().length}<RowGroup title="Installed MCP servers">{#snippet actions()}<code class="project-muted">.claude/settings.json</code>{/snippet}{#each filteredInstalledServers() as server (server.name)}{@render serverRow(server, false)}{/each}</RowGroup>{/if}
{#if filteredLocalServers().length}<RowGroup title="Local MCP servers">{#snippet actions()}<code class="project-muted">.claude/settings.local.json</code>{/snippet}{#each filteredLocalServers() as server (server.name)}{@render serverRow(server, true)}{/each}</RowGroup>{/if}
{#if filteredProjectServers().length}<RowGroup title="Project MCP servers">{#snippet actions()}<code class="project-muted">.mcp.json</code>{/snippet}{#each filteredProjectServers() as server (server.name)}{@render serverRow(server, true)}{/each}</RowGroup>{/if}
{#if listed && !filteredUserServers().length && !filteredInstalledServers().length && !filteredLocalServers().length && !filteredProjectServers().length && !$mcpError}
	<EmptyDetail title={searchQuery ? 'No matching MCP servers' : 'No MCP servers available'} message="Add a server to make its tools available to this project.">
		{#if searchQuery}<button class="st-btn" onclick={() => (searchQuery = '')}>Clear search</button>{:else}<button class="st-btn primary" onclick={openAddForm}>Add server</button>{/if}
	</EmptyDetail>
{/if}
<ConfirmDialog open={!!pendingRemove} title="Remove MCP server" message={pendingRemove?.scope === 'user' ? `Remove "${pendingRemove?.name}"? It is a global server, so every project loses it.` : `Remove "${pendingRemove?.name}" from this project?`} confirmLabel="Remove" danger onConfirm={confirmRemove} onCancel={() => (pendingRemove = null)} />
<McpServerForm bind:this={remoteForm} show={showMcpServerForm} server={editingMcpServer} scope={mcpSelectedScope} loading={$mcpLoading} showScopeSelector={!editingMcpServer} onSave={handleSaveMcpServer} onClose={() => { showMcpServerForm = false; editingMcpServer = null; }} />
