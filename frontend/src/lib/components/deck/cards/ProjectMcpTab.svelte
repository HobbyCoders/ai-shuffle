<script lang="ts">
	/**
	 * ProjectMcpTab - MCP server visibility tab for project detail view
	 *
	 * Shows ALL MCP servers available to this project across all scopes:
	 * - USER scope (global servers like "pencil mcp") — with enable/disable toggle + remove
	 * - INSTALLED scope (~/.claude.json project mcpServers) — with edit + remove
	 * - LOCAL scope (.claude/.mcp.json) — with edit capability
	 * - PROJECT scope (.mcp.json) — with edit capability
	 */
	import { mcp, projectMcpServers, mcpLoading } from '$lib/stores/mcp.svelte';
	import type { McpServerInfo } from '$lib/api/mcp';
	import McpServerForm from '$lib/components/mcp/McpServerForm.svelte';
	import { Search, Server, Globe, Terminal, Zap, FolderGit2, FolderLock, Download, Trash2, ToggleLeft, ToggleRight, ChevronRight } from 'lucide-svelte';
	import './card-design-system.css';

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

	// Server type icon helper
	function getTypeLabel(type: string): string {
		switch (type) {
			case 'stdio': return 'stdio';
			case 'sse': return 'SSE';
			case 'http': return 'HTTP';
			default: return type;
		}
	}
</script>

<div class="mcp-tab">
	<!-- Search -->
	{#if totalServers > 3}
		<div class="tab-search">
			<div class="card-search-container">
				<Search class="card-search-icon" />
				<input
					type="text"
					bind:value={searchQuery}
					placeholder="Search MCP servers..."
					class="card-search-input"
				/>
			</div>
		</div>
	{/if}

	<div class="server-list">
		<!-- Global (User) MCP Servers -->
		{#if filteredUserServers().length > 0}
			<div class="scope-section">
				<div class="scope-header">
					<Globe class="scope-icon" />
					<span>Global MCP Servers</span>
					<span class="card-badge card-badge--muted">~/.claude.json</span>
				</div>
				{#each filteredUserServers() as server, index (server.name)}
					{#if index > 0}<div class="fp-list-divider"></div>{/if}
					<div class="fp-list-item" class:fp-list-item--disabled={!server.enabled}>
						<div class="fp-item-indicator"></div>
						<div class="fp-item-content">
							<div class="fp-item-header">
								<span class="fp-item-name">{server.name}</span>
								<span class="fp-item-badge">{getTypeLabel(server.type)}</span>
								{#if server.builtin}
									<span class="fp-item-badge">Built-in</span>
								{/if}
								{#if server.enabled}
									<span class="fp-item-badge" style="color: var(--success)">Enabled</span>
								{:else}
									<span class="fp-item-badge">Disabled</span>
								{/if}
							</div>
							<div class="fp-item-meta">
								{#if server.type === 'stdio' && server.command}
									<span class="fp-meta-chip"><code>{server.command}{server.args ? ' ' + server.args.join(' ') : ''}</code></span>
								{:else if (server.type === 'sse' || server.type === 'http') && server.url}
									<span class="fp-meta-chip">{server.url}</span>
								{/if}
							</div>
						</div>
						<!-- svelte-ignore a11y_no_static_element_interactions -->
						<!-- svelte-ignore a11y_click_events_have_key_events -->
						<div class="fp-item-actions" onclick={(e) => e.stopPropagation()} role="toolbar" tabindex="-1">
							<button
								class="card-action-btn"
								title={server.enabled ? 'Disable server' : 'Enable server'}
								onclick={() => handleToggle(server)}
							>
								{#if server.enabled}
									<ToggleRight size={16} />
								{:else}
									<ToggleLeft size={16} />
								{/if}
							</button>
							{#if !server.builtin}
								<button
									class="card-action-btn card-action-btn--danger"
									title="Remove server"
									onclick={() => handleRemoveServer(server)}
								>
									<Trash2 size={14} />
								</button>
							{/if}
						</div>
						<div class="fp-item-chevron"><ChevronRight size={16} /></div>
					</div>
				{/each}
			</div>
		{/if}

		<!-- Installed MCP Servers (per-project in ~/.claude.json) -->
		{#if filteredInstalledServers().length > 0}
			<div class="scope-section">
				<div class="scope-header">
					<Download class="scope-icon" />
					<span>Installed MCP Servers</span>
					<span class="card-badge card-badge--muted">~/.claude.json</span>
				</div>
				{#each filteredInstalledServers() as server, index (server.name)}
					{#if index > 0}<div class="fp-list-divider"></div>{/if}
					<div class="fp-list-item" class:fp-list-item--disabled={!server.enabled}>
						<div class="fp-item-indicator"></div>
						<div class="fp-item-content">
							<div class="fp-item-header">
								<span class="fp-item-name">{server.name}</span>
								<span class="fp-item-badge">{getTypeLabel(server.type)}</span>
								{#if server.enabled}
									<span class="fp-item-badge" style="color: var(--success)">Enabled</span>
								{:else}
									<span class="fp-item-badge">Disabled</span>
								{/if}
							</div>
							<div class="fp-item-meta">
								{#if server.type === 'stdio' && server.command}
									<span class="fp-meta-chip"><code>{server.command}{server.args ? ' ' + server.args.join(' ') : ''}</code></span>
								{:else if (server.type === 'sse' || server.type === 'http') && server.url}
									<span class="fp-meta-chip">{server.url}</span>
								{/if}
							</div>
						</div>
						<!-- svelte-ignore a11y_no_static_element_interactions -->
						<!-- svelte-ignore a11y_click_events_have_key_events -->
						<div class="fp-item-actions" onclick={(e) => e.stopPropagation()} role="toolbar" tabindex="-1">
							<button
								class="card-action-btn"
								title={server.enabled ? 'Disable server' : 'Enable server'}
								onclick={() => handleToggle(server)}
							>
								{#if server.enabled}
									<ToggleRight size={16} />
								{:else}
									<ToggleLeft size={16} />
								{/if}
							</button>
							<button
								class="card-action-btn card-action-btn--danger"
								title="Remove server"
								onclick={() => handleRemoveServer(server)}
							>
								<Trash2 size={14} />
							</button>
						</div>
						<div class="fp-item-chevron"><ChevronRight size={16} /></div>
					</div>
				{/each}
			</div>
		{/if}

		<!-- Local MCP Servers -->
		{#if filteredLocalServers().length > 0}
			<div class="scope-section">
				<div class="scope-header">
					<FolderLock class="scope-icon" />
					<span>Local MCP Servers</span>
					<span class="card-badge card-badge--muted">.claude/.mcp.json</span>
				</div>
				{#each filteredLocalServers() as server, index (server.name)}
					{#if index > 0}<div class="fp-list-divider"></div>{/if}
					<button class="fp-list-item" onclick={() => handleEditServer(server)}>
						<div class="fp-item-indicator"></div>
						<div class="fp-item-content">
							<div class="fp-item-header">
								<span class="fp-item-name">{server.name}</span>
								<span class="fp-item-badge">{getTypeLabel(server.type)}</span>
							</div>
							<div class="fp-item-meta">
								{#if server.type === 'stdio' && server.command}
									<span class="fp-meta-chip"><code>{server.command}{server.args ? ' ' + server.args.join(' ') : ''}</code></span>
								{:else if (server.type === 'sse' || server.type === 'http') && server.url}
									<span class="fp-meta-chip">{server.url}</span>
								{/if}
							</div>
						</div>
						<div class="fp-item-chevron"><ChevronRight size={16} /></div>
					</button>
				{/each}
			</div>
		{/if}

		<!-- Project MCP Servers -->
		{#if filteredProjectServers().length > 0}
			<div class="scope-section">
				<div class="scope-header">
					<FolderGit2 class="scope-icon" />
					<span>Project MCP Servers</span>
					<span class="card-badge card-badge--muted">.mcp.json</span>
				</div>
				{#each filteredProjectServers() as server, index (server.name)}
					{#if index > 0}<div class="fp-list-divider"></div>{/if}
					<button class="fp-list-item" onclick={() => handleEditServer(server)}>
						<div class="fp-item-indicator"></div>
						<div class="fp-item-content">
							<div class="fp-item-header">
								<span class="fp-item-name">{server.name}</span>
								<span class="fp-item-badge">{getTypeLabel(server.type)}</span>
							</div>
							<div class="fp-item-meta">
								{#if server.type === 'stdio' && server.command}
									<span class="fp-meta-chip"><code>{server.command}{server.args ? ' ' + server.args.join(' ') : ''}</code></span>
								{:else if (server.type === 'sse' || server.type === 'http') && server.url}
									<span class="fp-meta-chip">{server.url}</span>
								{/if}
							</div>
						</div>
						<div class="fp-item-chevron"><ChevronRight size={16} /></div>
					</button>
				{/each}
			</div>
		{/if}

		<!-- Empty state -->
		{#if totalServers === 0}
			<div class="card-empty-state">
				<Server class="card-empty-icon" />
				<p class="card-empty-title">No MCP servers available</p>
				<p class="card-empty-description">
					Add MCP servers from the MCP Servers card to see them here.
				</p>
			</div>
		{/if}
	</div>

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

<style>
	.mcp-tab {
		display: flex;
		flex-direction: column;
		height: 100%;
		gap: var(--space-3);
	}

	.tab-search {
		padding: 0 var(--space-4);
	}

	.server-list {
		flex: 1;
		overflow-y: auto;
		padding: 0 var(--space-4);
		display: flex;
		flex-direction: column;
		gap: var(--space-4);
	}

	.scope-section {
		display: flex;
		flex-direction: column;
		gap: var(--space-2);
	}

	.scope-header {
		display: flex;
		align-items: center;
		gap: var(--space-2);
		font-size: var(--text-sm);
		font-weight: 500;
		color: var(--muted-foreground);
		padding: var(--space-1) 0;
	}

	/* ─── Server Items ─── */

	:global(.fp-list-item).fp-list-item--disabled {
		opacity: 0.5;
	}
</style>
