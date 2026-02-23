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
	import { mcp, projectMcpServers, mcpLoading } from '$lib/stores/mcp';
	import type { McpServerInfo } from '$lib/api/mcp';
	import { McpServerForm } from '$lib/components/mcp';
	import { Search, Server, Globe, Terminal, Zap, FolderGit2, FolderLock, Download, Trash2, ToggleLeft, ToggleRight } from 'lucide-svelte';
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
				{#each filteredUserServers() as server (server.name)}
					<div class="server-item" class:server-item--disabled={!server.enabled}>
						<div class="server-icon" class:server-icon--muted={!server.enabled}>
							{#if server.type === 'stdio'}
								<Terminal size={16} />
							{:else if server.type === 'sse'}
								<Zap size={16} />
							{:else if server.type === 'http'}
								<Globe size={16} />
							{:else}
								<Server size={16} />
							{/if}
						</div>
						<div class="server-content">
							<div class="server-header">
								<span class="server-name">{server.name}</span>
								<span class="mcp-type-badge">{getTypeLabel(server.type)}</span>
								{#if server.builtin}
									<span class="card-badge card-badge--builtin">Built-in</span>
								{/if}
								<span class="card-badge {server.enabled ? 'card-badge--success' : 'card-badge--muted'}">
									{server.enabled ? 'Enabled' : 'Disabled'}
								</span>
							</div>
							{#if server.description}
								<span class="server-description">{server.description}</span>
							{/if}
							<div class="server-meta">
								{#if server.type === 'stdio' && server.command}
									<span class="meta-chip"><Terminal size={12} /> <code>{server.command}{server.args ? ' ' + server.args.join(' ') : ''}</code></span>
								{:else if (server.type === 'sse' || server.type === 'http') && server.url}
									<span class="meta-chip"><Globe size={12} /> {server.url}</span>
								{/if}
							</div>
						</div>
						<div class="server-actions" onclick={(e) => e.stopPropagation()} role="toolbar" tabindex="-1">
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
				{#each filteredInstalledServers() as server (server.name)}
					<div class="server-item" class:server-item--disabled={!server.enabled}>
						<div class="server-icon" class:server-icon--muted={!server.enabled}>
							{#if server.type === 'stdio'}
								<Terminal size={16} />
							{:else if server.type === 'sse'}
								<Zap size={16} />
							{:else if server.type === 'http'}
								<Globe size={16} />
							{:else}
								<Server size={16} />
							{/if}
						</div>
						<div class="server-content">
							<div class="server-header">
								<span class="server-name">{server.name}</span>
								<span class="mcp-type-badge">{getTypeLabel(server.type)}</span>
								<span class="card-badge {server.enabled ? 'card-badge--success' : 'card-badge--muted'}">
									{server.enabled ? 'Enabled' : 'Disabled'}
								</span>
							</div>
							{#if server.description}
								<span class="server-description">{server.description}</span>
							{/if}
							<div class="server-meta">
								{#if server.type === 'stdio' && server.command}
									<span class="meta-chip"><Terminal size={12} /> <code>{server.command}{server.args ? ' ' + server.args.join(' ') : ''}</code></span>
								{:else if (server.type === 'sse' || server.type === 'http') && server.url}
									<span class="meta-chip"><Globe size={12} /> {server.url}</span>
								{/if}
							</div>
						</div>
						<div class="server-actions" onclick={(e) => e.stopPropagation()} role="toolbar" tabindex="-1">
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
				{#each filteredLocalServers() as server (server.name)}
					<button class="server-item server-item--clickable" onclick={() => handleEditServer(server)}>
						<div class="server-icon">
							{#if server.type === 'stdio'}
								<Terminal size={16} />
							{:else if server.type === 'sse'}
								<Zap size={16} />
							{:else if server.type === 'http'}
								<Globe size={16} />
							{:else}
								<Server size={16} />
							{/if}
						</div>
						<div class="server-content">
							<div class="server-header">
								<span class="server-name">{server.name}</span>
								<span class="mcp-type-badge">{getTypeLabel(server.type)}</span>
							</div>
							{#if server.description}
								<span class="server-description">{server.description}</span>
							{/if}
							<div class="server-meta">
								{#if server.type === 'stdio' && server.command}
									<span class="meta-chip"><Terminal size={12} /> <code>{server.command}{server.args ? ' ' + server.args.join(' ') : ''}</code></span>
								{:else if (server.type === 'sse' || server.type === 'http') && server.url}
									<span class="meta-chip"><Globe size={12} /> {server.url}</span>
								{/if}
							</div>
						</div>
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
				{#each filteredProjectServers() as server (server.name)}
					<button class="server-item server-item--clickable" onclick={() => handleEditServer(server)}>
						<div class="server-icon">
							{#if server.type === 'stdio'}
								<Terminal size={16} />
							{:else if server.type === 'sse'}
								<Zap size={16} />
							{:else if server.type === 'http'}
								<Globe size={16} />
							{:else}
								<Server size={16} />
							{/if}
						</div>
						<div class="server-content">
							<div class="server-header">
								<span class="server-name">{server.name}</span>
								<span class="mcp-type-badge">{getTypeLabel(server.type)}</span>
							</div>
							{#if server.description}
								<span class="server-description">{server.description}</span>
							{/if}
							<div class="server-meta">
								{#if server.type === 'stdio' && server.command}
									<span class="meta-chip"><Terminal size={12} /> <code>{server.command}{server.args ? ' ' + server.args.join(' ') : ''}</code></span>
								{:else if (server.type === 'sse' || server.type === 'http') && server.url}
									<span class="meta-chip"><Globe size={12} /> {server.url}</span>
								{/if}
							</div>
						</div>
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

	.scope-icon {
		width: 16px;
		height: 16px;
		flex-shrink: 0;
	}

	/* ─── Server Items ─── */

	.server-item {
		display: flex;
		align-items: flex-start;
		gap: var(--space-3, 12px);
		padding: var(--space-3, 12px);
		border-radius: var(--radius-md, 8px);
		border: 1px solid var(--border-subtle, color-mix(in oklch, var(--border) 60%, transparent));
		background: var(--surface-1, var(--card));
		transition: border-color 150ms ease;
	}

	.server-item:hover {
		border-color: var(--border-default, var(--border));
	}

	.server-item--clickable {
		cursor: pointer;
		text-align: left;
		font: inherit;
		color: inherit;
	}

	.server-item--disabled {
		opacity: 0.6;
	}

	.server-icon {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 32px;
		height: 32px;
		border-radius: var(--radius, 6px);
		background: color-mix(in oklch, var(--primary) 12%, transparent);
		color: var(--primary);
		flex-shrink: 0;
	}

	.server-icon--muted {
		background: var(--muted);
		color: var(--muted-foreground);
	}

	.server-content {
		flex: 1;
		min-width: 0;
		display: flex;
		flex-direction: column;
		gap: 4px;
	}

	.server-header {
		display: flex;
		align-items: center;
		gap: var(--space-2, 8px);
		flex-wrap: wrap;
	}

	.server-name {
		font-size: var(--text-sm, 0.75rem);
		font-weight: 600;
		color: var(--foreground);
	}

	.server-description {
		font-size: var(--text-xs, 0.6875rem);
		color: var(--muted-foreground);
	}

	.server-meta {
		display: flex;
		flex-wrap: wrap;
		gap: var(--space-2, 8px);
	}

	.meta-chip {
		display: inline-flex;
		align-items: center;
		gap: 4px;
		font-size: var(--text-xs, 0.6875rem);
		color: var(--muted-foreground);
	}

	.meta-chip code {
		font-family: var(--card-font-mono, 'JetBrains Mono', monospace);
		font-size: var(--text-xs, 0.6875rem);
	}

	.mcp-type-badge {
		font-size: var(--text-xs, 0.6875rem);
		padding: 1px 6px;
		border-radius: var(--radius-sm, 4px);
		background: color-mix(in oklch, var(--muted) 80%, transparent);
		color: var(--muted-foreground);
		font-weight: 500;
	}

	.server-actions {
		display: flex;
		gap: var(--space-1, 4px);
		flex-shrink: 0;
	}

	/* Truncate long paths/URLs */
	.meta-chip {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		max-width: 100%;
	}

	.meta-chip code {
		overflow: hidden;
		text-overflow: ellipsis;
	}

	.server-meta {
		overflow: hidden;
		min-width: 0;
	}

	/* Mobile: actions on centered bottom row */
	@media (max-width: 639px) {
		.server-item {
			flex-wrap: wrap;
		}

		.server-actions {
			flex: 1 0 100%;
			justify-content: center;
			padding-top: var(--space-2, 8px);
			border-top: 1px solid var(--border-subtle, color-mix(in oklch, var(--border) 60%, transparent));
			margin-top: var(--space-1, 4px);
		}
	}
</style>
