<script lang="ts">
	/**
	 * ProjectMcpTab - MCP server visibility tab for project detail view
	 *
	 * Studio-v2 grouped-field design (.set-group / .set-row), mirroring the
	 * Profile editor. Field primitives come from project-tabs.css (.ptab).
	 *
	 * Shows ALL MCP servers available to this project across all scopes:
	 * - USER scope (global servers) — enable/disable toggle + remove
	 * - INSTALLED scope (~/.claude.json project mcpServers) — toggle + remove
	 * - LOCAL scope (.claude/.mcp.json) — edit
	 * - PROJECT scope (.mcp.json) — edit
	 */
	import { mcp, projectMcpServers, mcpLoading } from '$lib/stores/mcp.svelte';
	import type { McpServerInfo } from '$lib/api/mcp';
	import McpServerForm from '$lib/components/mcp/McpServerForm.svelte';
	import RowActionMenu from '$lib/components/ui/RowActionMenu.svelte';
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

	// Load MCP servers when project changes
	$effect(() => {
		if (projectId) {
			mcp.loadProjectServers(projectId);
		}
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

	const filteredUserServers = $derived(() => filterServers($projectMcpServers.user || []));
	const filteredInstalledServers = $derived(() => filterServers($projectMcpServers.installed || []));
	const filteredLocalServers = $derived(() => filterServers($projectMcpServers.local || []));
	const filteredProjectServers = $derived(() => filterServers($projectMcpServers.project || []));

	const totalServers = $derived(
		($projectMcpServers.user?.length || 0) +
		($projectMcpServers.installed?.length || 0) +
		($projectMcpServers.local?.length || 0) +
		($projectMcpServers.project?.length || 0)
	);

	// Toggle server enabled/disabled for this project
	async function handleToggle(server: McpServerInfo) {
		try {
			await mcp.toggleProjectServer(projectId, server.name, !server.enabled);
		} catch (e) {
			console.error('Failed to toggle MCP server:', e);
		}
	}

	// Remove server
	async function handleRemoveServer(server: McpServerInfo) {
		try {
			await mcp.removeProjectServer(projectId, server.name, server.scope as 'user' | 'installed' | 'local' | 'project');
		} catch (e) {
			console.error('Failed to remove MCP server:', e);
		}
	}

	// Add new local/project server
	export function openAddForm() {
		editingMcpServer = null;
		mcpSelectedScope = 'local';
		showMcpServerForm = true;
	}

	// Edit local/project/installed server
	function handleEditServer(server: McpServerInfo) {
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
		try {
			const scope = config.scope || mcpSelectedScope;
			if (editingMcpServer) {
				await mcp.updateProjectServer(
					projectId,
					editingMcpServer.name,
					config,
					scope
				);
			} else {
				await mcp.addProjectServer(projectId, {
					...config,
					scope: scope as 'installed' | 'local' | 'project'
				});
			}
			showMcpServerForm = false;
			editingMcpServer = null;
		} catch (e) {
			console.error('Failed to save MCP server:', e);
		}
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
</script>

<svelte:window onclick={onWindowClick} onkeydown={onWindowKeydown} />

{#snippet serverMeta(server: McpServerInfo)}
	{#if server.type === 'stdio' && server.command}
		<p class="mono">{server.command}{server.args ? ' ' + server.args.join(' ') : ''}</p>
	{:else if (server.type === 'sse' || server.type === 'http') && server.url}
		<p class="mono">{server.url}</p>
	{/if}
{/snippet}

{#snippet toggleRow(server: McpServerInfo, removable: boolean)}
	<div class="set-row" class:disabled={!server.enabled}>
		<div class="lab">
			<h4>
				{server.name}
				<span class="hint">· {getTypeLabel(server.type)}</span>
				{#if server.builtin}<span class="badge muted">Built-in</span>{/if}
			</h4>
			{@render serverMeta(server)}
		</div>
		<div class="ctl">
			<button
				type="button"
				class="switch"
				class:on={server.enabled}
				role="switch"
				aria-checked={server.enabled}
				aria-label={server.enabled ? 'Disable server' : 'Enable server'}
				title={server.enabled ? 'Disable server' : 'Enable server'}
				onclick={() => handleToggle(server)}
			><i></i></button>
			{#if removable}
				<RowActionMenu
					open={actionsOpenKey === rowKey(server)}
					onToggle={(e) => toggleRowActions(e, rowKey(server))}
					label="Server actions"
				>
					<button type="button" class="icon-btn danger" title="Remove server" onclick={(e) => { e.stopPropagation(); actionsOpenKey = null; handleRemoveServer(server); }}>
						<Trash2 size={15} />
					</button>
				</RowActionMenu>
			{/if}
		</div>
	</div>
{/snippet}

{#snippet editRow(server: McpServerInfo)}
	<div class="set-row">
		<div class="lab">
			<h4>{server.name} <span class="hint">· {getTypeLabel(server.type)}</span></h4>
			{@render serverMeta(server)}
		</div>
		<div class="ctl">
			<button type="button" class="icon-btn" title="Edit server" onclick={() => handleEditServer(server)}>
				<Pencil size={15} />
			</button>
		</div>
	</div>
{/snippet}

<div class="ptab">
	<!-- Search -->
	{#if totalServers > 3}
		<div class="tsearch">
			<Search />
			<input type="text" bind:value={searchQuery} placeholder="Search MCP servers…" class="inp" />
		</div>
	{/if}

	<!-- Global (User) MCP Servers -->
	{#if filteredUserServers().length > 0}
		<div class="set-group">
			<div class="set-grouphead grouphead-row">
				<div><h3><Globe />Global MCP Servers</h3></div>
				<span class="badge mono">~/.claude.json</span>
			</div>
			{#each filteredUserServers() as server (server.name)}
				{@render toggleRow(server, !server.builtin)}
			{/each}
		</div>
	{/if}

	<!-- Installed MCP Servers (per-project in ~/.claude.json) -->
	{#if filteredInstalledServers().length > 0}
		<div class="set-group">
			<div class="set-grouphead grouphead-row">
				<div><h3><Download />Installed MCP Servers</h3></div>
				<span class="badge mono">~/.claude.json</span>
			</div>
			{#each filteredInstalledServers() as server (server.name)}
				{@render toggleRow(server, true)}
			{/each}
		</div>
	{/if}

	<!-- Local MCP Servers -->
	{#if filteredLocalServers().length > 0}
		<div class="set-group">
			<div class="set-grouphead grouphead-row">
				<div><h3><FolderLock />Local MCP Servers</h3></div>
				<span class="badge mono">.claude/.mcp.json</span>
			</div>
			{#each filteredLocalServers() as server (server.name)}
				{@render editRow(server)}
			{/each}
		</div>
	{/if}

	<!-- Project MCP Servers -->
	{#if filteredProjectServers().length > 0}
		<div class="set-group">
			<div class="set-grouphead grouphead-row">
				<div><h3><FolderGit2 />Project MCP Servers</h3></div>
				<span class="badge mono">.mcp.json</span>
			</div>
			{#each filteredProjectServers() as server (server.name)}
				{@render editRow(server)}
			{/each}
		</div>
	{/if}

	<!-- Empty state -->
	{#if totalServers === 0}
		<div class="empty-state">
			<Server />
			<p class="es-title">No MCP servers available</p>
			<p class="es-desc">Add MCP servers from the MCP Servers card to see them here.</p>
		</div>
	{/if}

	<!-- MCP Server Form Modal -->
	<McpServerForm
		show={showMcpServerForm}
		server={editingMcpServer}
		scope={mcpSelectedScope}
		loading={$mcpLoading}
		showScopeSelector={!editingMcpServer}
		onSave={handleSaveMcpServer}
		onClose={() => {
			showMcpServerForm = false;
			editingMcpServer = null;
		}}
	/>
</div>
