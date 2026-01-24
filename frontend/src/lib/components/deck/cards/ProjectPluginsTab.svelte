<script lang="ts">
	/**
	 * ProjectPluginsTab - Plugin management tab for project detail view
	 *
	 * Features:
	 * - View LOCAL scope plugins (.claude/plugins/)
	 * - View PROJECT scope plugins (plugins/)
	 * - Install plugins from marketplaces
	 * - Enable/disable/uninstall plugins
	 */
	import { onMount } from 'svelte';
	import {
		projectPlugins,
		localPlugins,
		projectScopePlugins,
		availableProjectPlugins,
		projectPluginsLoading,
		projectPluginsError,
		projectPluginsInstalling,
		projectPluginStats
	} from '$lib/stores/projectPlugins';
	import { marketplaces } from '$lib/stores/plugins';
	import { plugins as globalPlugins } from '$lib/stores/plugins';
	import { mcp, projectMcpServers, mcpLoading } from '$lib/stores/mcp';
	import type { McpServerInfo, CreateProjectMcpServerRequest } from '$lib/api/mcp';
	import { McpServerList, McpServerForm } from '$lib/components/mcp';
	import { Search, Package, Download, Trash2, X, Loader2, ToggleLeft, ToggleRight, FolderGit2, FolderLock, Server } from 'lucide-svelte';
	import './card-design-system.css';

	interface Props {
		projectId: string;
	}

	let { projectId }: Props = $props();

	// Local state
	let searchQuery = $state('');
	let showInstallModal = $state(false);
	let selectedScope = $state<'local' | 'project'>('local');
	let selectedMarketplace = $state<string | null>(null);

	// MCP state
	let showMcpServerForm = $state(false);
	let editingMcpServer = $state<McpServerInfo | null>(null);
	let mcpSelectedScope = $state<'local' | 'project'>('local');

	// Load plugins and MCP servers when project changes
	$effect(() => {
		if (projectId) {
			projectPlugins.loadAll(projectId);
			globalPlugins.loadMarketplaces();
			mcp.loadProjectServers(projectId);
		}
	});

	// Filter installed plugins
	const filteredLocalPlugins = $derived(() => {
		if (!searchQuery.trim()) return $localPlugins;
		const q = searchQuery.toLowerCase();
		return $localPlugins.filter(
			(p) =>
				p.name.toLowerCase().includes(q) ||
				p.description.toLowerCase().includes(q)
		);
	});

	const filteredProjectPlugins = $derived(() => {
		if (!searchQuery.trim()) return $projectScopePlugins;
		const q = searchQuery.toLowerCase();
		return $projectScopePlugins.filter(
			(p) =>
				p.name.toLowerCase().includes(q) ||
				p.description.toLowerCase().includes(q)
		);
	});

	// Filter available plugins
	const filteredAvailable = $derived(() => {
		let items = $availableProjectPlugins;
		if (selectedMarketplace) {
			items = items.filter((p) => p.marketplace === selectedMarketplace);
		}
		if (searchQuery.trim()) {
			const q = searchQuery.toLowerCase();
			items = items.filter(
				(p) =>
					p.name.toLowerCase().includes(q) ||
					p.description.toLowerCase().includes(q)
			);
		}
		return items;
	});

	// Handlers
	async function handleToggle(pluginId: string) {
		await projectPlugins.togglePlugin(projectId, pluginId);
	}

	async function handleUninstall(pluginId: string) {
		await projectPlugins.uninstallPlugin(projectId, pluginId);
	}

	async function handleInstall(pluginId: string) {
		await projectPlugins.installPlugin(projectId, pluginId, selectedScope);
	}

	// MCP Handlers
	function handleAddMcpServer() {
		editingMcpServer = null;
		showMcpServerForm = true;
	}

	function handleEditMcpServer(server: McpServerInfo) {
		editingMcpServer = server;
		mcpSelectedScope = server.scope as 'local' | 'project';
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
		scope?: 'local' | 'project';
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
					scope
				});
			}
			showMcpServerForm = false;
			editingMcpServer = null;
		} catch (e) {
			console.error('Failed to save MCP server:', e);
		}
	}

	async function handleRemoveMcpServer(serverName: string, scope: 'local' | 'project' | 'user') {
		if (scope === 'user') return; // Can't remove global from project view
		try {
			await mcp.removeProjectServer(projectId, serverName, scope as 'local' | 'project');
		} catch (e) {
			console.error('Failed to remove MCP server:', e);
		}
	}
</script>

<div class="plugins-tab">
	<!-- Search -->
	{#if $projectPluginStats.totalInstalled > 0 || showInstallModal}
		<div class="tab-search">
			<div class="card-search-container">
				<Search class="card-search-icon" />
				<input
					type="text"
					bind:value={searchQuery}
					placeholder={showInstallModal ? "Search available plugins..." : "Search installed plugins..."}
					class="card-search-input"
				/>
			</div>
		</div>
	{/if}

	{#if $projectPluginsLoading}
		<div class="card-empty-state">
			<Loader2 class="card-empty-icon animate-spin" />
			<p class="card-empty-title">Loading plugins...</p>
		</div>
	{:else if $projectPluginsError}
		<div class="card-error-banner">
			<span class="flex-1">{$projectPluginsError}</span>
			<button onclick={() => projectPlugins.clearError()}>
				<X class="w-4 h-4" />
			</button>
		</div>
	{:else if showInstallModal}
		<!-- Install View -->
		<div class="install-view">
			<div class="install-header">
				<button onclick={() => (showInstallModal = false)} class="card-btn-secondary">
					← Back
				</button>
				<div class="scope-selector">
					<button
						class="scope-btn"
						class:active={selectedScope === 'local'}
						onclick={() => (selectedScope = 'local')}
						title="Install to .claude/plugins/ (gitignored)"
					>
						<FolderLock class="w-4 h-4" />
						Local
					</button>
					<button
						class="scope-btn"
						class:active={selectedScope === 'project'}
						onclick={() => (selectedScope = 'project')}
						title="Install to plugins/ (committed)"
					>
						<FolderGit2 class="w-4 h-4" />
						Project
					</button>
				</div>
			</div>

			<!-- Marketplace filter -->
			{#if $marketplaces.length > 1}
				<div class="marketplace-filter">
					<select
						bind:value={selectedMarketplace}
						class="card-form-input"
					>
						<option value={null}>All marketplaces</option>
						{#each $marketplaces as mp}
							<option value={mp.id}>{mp.id}</option>
						{/each}
					</select>
				</div>
			{/if}

			<!-- Available plugins list -->
			{#if filteredAvailable().length === 0}
				<div class="card-empty-state">
					<Package class="card-empty-icon" />
					<p class="card-empty-title">No plugins available</p>
					<p class="card-empty-description">
						Add a marketplace from the Plugin Manager card to discover plugins.
					</p>
				</div>
			{:else}
				<div class="plugin-list">
					{#each filteredAvailable() as plugin (plugin.id)}
						<div class="card-list-item">
							<div class="card-item-icon">
								<Package />
							</div>
							<div class="card-item-content">
								<div class="card-item-header">
									<span class="card-item-name">{plugin.name}</span>
									<span class="card-badge card-badge--muted">{plugin.marketplace}</span>
								</div>
								<span class="card-item-description">{plugin.description || 'No description'}</span>
							</div>
							<div class="card-item-actions">
								<button
									onclick={() => handleInstall(plugin.id)}
									disabled={$projectPluginsInstalling[plugin.id]}
									class="card-btn-primary card-btn-small"
								>
									{#if $projectPluginsInstalling[plugin.id]}
										<Loader2 class="w-4 h-4 animate-spin" />
									{:else}
										<Download class="w-4 h-4" />
									{/if}
									Install
								</button>
							</div>
						</div>
					{/each}
				</div>
			{/if}
		</div>
	{:else}
		<!-- Installed Plugins View -->
		<div class="installed-view">
			<!-- Local scope section -->
			{#if filteredLocalPlugins().length > 0}
				<div class="scope-section">
					<div class="scope-header">
						<FolderLock class="w-4 h-4" />
						<span>Local Plugins</span>
						<span class="card-badge card-badge--muted">.claude/plugins/</span>
					</div>
					{#each filteredLocalPlugins() as plugin (plugin.id)}
						<div class="card-list-item">
							<div class="card-item-icon">
								<Package />
							</div>
							<div class="card-item-content">
								<div class="card-item-header">
									<span class="card-item-name">{plugin.name}</span>
									<span class="card-badge" class:card-badge--success={plugin.enabled} class:card-badge--muted={!plugin.enabled}>
										{plugin.enabled ? 'Enabled' : 'Disabled'}
									</span>
								</div>
								<span class="card-item-description">{plugin.description || 'No description'}</span>
							</div>
							<div class="card-item-actions">
								<button
									onclick={() => handleToggle(plugin.id)}
									class="card-action-btn"
									title={plugin.enabled ? 'Disable' : 'Enable'}
								>
									{#if plugin.enabled}
										<ToggleRight class="text-success" />
									{:else}
										<ToggleLeft />
									{/if}
								</button>
								<button
									onclick={() => handleUninstall(plugin.id)}
									disabled={$projectPluginsInstalling[plugin.id]}
									class="card-action-btn card-action-btn--destructive"
									title="Uninstall"
								>
									{#if $projectPluginsInstalling[plugin.id]}
										<Loader2 class="w-4 h-4 animate-spin" />
									{:else}
										<Trash2 />
									{/if}
								</button>
							</div>
						</div>
					{/each}
				</div>
			{/if}

			<!-- Project scope section -->
			{#if filteredProjectPlugins().length > 0}
				<div class="scope-section">
					<div class="scope-header">
						<FolderGit2 class="w-4 h-4" />
						<span>Project Plugins</span>
						<span class="card-badge card-badge--muted">plugins/</span>
					</div>
					{#each filteredProjectPlugins() as plugin (plugin.id)}
						<div class="card-list-item">
							<div class="card-item-icon">
								<Package />
							</div>
							<div class="card-item-content">
								<div class="card-item-header">
									<span class="card-item-name">{plugin.name}</span>
									<span class="card-badge" class:card-badge--success={plugin.enabled} class:card-badge--muted={!plugin.enabled}>
										{plugin.enabled ? 'Enabled' : 'Disabled'}
									</span>
								</div>
								<span class="card-item-description">{plugin.description || 'No description'}</span>
							</div>
							<div class="card-item-actions">
								<button
									onclick={() => handleToggle(plugin.id)}
									class="card-action-btn"
									title={plugin.enabled ? 'Disable' : 'Enable'}
								>
									{#if plugin.enabled}
										<ToggleRight class="text-success" />
									{:else}
										<ToggleLeft />
									{/if}
								</button>
								<button
									onclick={() => handleUninstall(plugin.id)}
									disabled={$projectPluginsInstalling[plugin.id]}
									class="card-action-btn card-action-btn--destructive"
									title="Uninstall"
								>
									{#if $projectPluginsInstalling[plugin.id]}
										<Loader2 class="w-4 h-4 animate-spin" />
									{:else}
										<Trash2 />
									{/if}
								</button>
							</div>
						</div>
					{/each}
				</div>
			{/if}

			<!-- Empty state for plugins -->
			{#if $projectPluginStats.totalInstalled === 0}
				<div class="card-empty-state-inline">
					<Package class="card-empty-icon-small" />
					<p class="card-empty-title-small">No plugins installed</p>
				</div>
			{/if}

			<!-- MCP Servers Section -->
			<div class="mcp-section">
				<div class="section-divider">
					<Server class="w-4 h-4" />
					<span>MCP Servers</span>
				</div>

				<!-- Local MCP Servers -->
				{#if ($projectMcpServers.local?.length || 0) > 0}
					<div class="scope-section">
						<div class="scope-header">
							<FolderLock class="w-4 h-4" />
							<span>Local MCP Servers</span>
							<span class="card-badge card-badge--muted">.claude/.mcp.json</span>
						</div>
						<McpServerList
							servers={$projectMcpServers.local || []}
							scope="local"
							loading={$mcpLoading}
							onEdit={handleEditMcpServer}
							onRemove={(name) => handleRemoveMcpServer(name, 'local')}
						/>
					</div>
				{/if}

				<!-- Project MCP Servers -->
				{#if ($projectMcpServers.project?.length || 0) > 0}
					<div class="scope-section">
						<div class="scope-header">
							<FolderGit2 class="w-4 h-4" />
							<span>Project MCP Servers</span>
							<span class="card-badge card-badge--muted">.mcp.json</span>
						</div>
						<McpServerList
							servers={$projectMcpServers.project || []}
							scope="project"
							loading={$mcpLoading}
							onEdit={handleEditMcpServer}
							onRemove={(name) => handleRemoveMcpServer(name, 'project')}
						/>
					</div>
				{/if}

				<!-- Empty state for MCP -->
				{#if ($projectMcpServers.local?.length || 0) === 0 && ($projectMcpServers.project?.length || 0) === 0}
					<div class="card-empty-state-inline">
						<Server class="card-empty-icon-small" />
						<p class="card-empty-title-small">No MCP servers configured</p>
					</div>
				{/if}
			</div>
		</div>

		<!-- Action buttons -->
		<div class="card-footer">
			<div class="card-footer-content footer-buttons">
				<button onclick={() => (showInstallModal = true)} class="card-btn-secondary">
					<Download />
					Install Plugin
				</button>
				<button onclick={handleAddMcpServer} class="card-btn-primary">
					<Server />
					Add MCP Server
				</button>
			</div>
		</div>
	{/if}

	<!-- MCP Server Form Modal -->
	<McpServerForm
		show={showMcpServerForm}
		server={editingMcpServer}
		scope={mcpSelectedScope}
		loading={$mcpLoading}
		showScopeSelector={true}
		onSave={handleSaveMcpServer}
		onClose={() => {
			showMcpServerForm = false;
			editingMcpServer = null;
		}}
	/>
</div>

<style>
	.plugins-tab {
		display: flex;
		flex-direction: column;
		height: 100%;
		gap: var(--space-3);
	}

	.tab-search {
		padding: 0 var(--space-4);
	}

	.installed-view,
	.install-view {
		flex: 1;
		overflow-y: auto;
		padding: 0 var(--space-4);
		display: flex;
		flex-direction: column;
		gap: var(--space-4);
	}

	.install-header {
		display: flex;
		justify-content: space-between;
		align-items: center;
		gap: var(--space-3);
	}

	.scope-selector {
		display: flex;
		gap: var(--space-1);
		background: var(--muted);
		border-radius: var(--radius);
		padding: 2px;
	}

	.scope-btn {
		display: flex;
		align-items: center;
		gap: var(--space-1);
		padding: var(--space-1) var(--space-2);
		border-radius: calc(var(--radius) - 2px);
		font-size: var(--text-sm);
		color: var(--muted-foreground);
		background: transparent;
		border: none;
		cursor: pointer;
		transition: all 0.15s ease;
	}

	.scope-btn:hover {
		color: var(--foreground);
	}

	.scope-btn.active {
		background: var(--background);
		color: var(--foreground);
		box-shadow: 0 1px 2px rgba(0, 0, 0, 0.1);
	}

	.marketplace-filter {
		padding: var(--space-2) 0;
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

	.plugin-list {
		display: flex;
		flex-direction: column;
		gap: var(--space-2);
	}

	.text-success {
		color: var(--success);
	}

	.flex-1 {
		flex: 1;
	}

	.animate-spin {
		animation: spin 1s linear infinite;
	}

	@keyframes spin {
		from {
			transform: rotate(0deg);
		}
		to {
			transform: rotate(360deg);
		}
	}

	/* MCP Section */
	.mcp-section {
		display: flex;
		flex-direction: column;
		gap: var(--space-3);
		padding-top: var(--space-4);
	}

	.section-divider {
		display: flex;
		align-items: center;
		gap: var(--space-2);
		font-size: var(--text-sm);
		font-weight: 600;
		color: var(--foreground);
		padding-bottom: var(--space-2);
		border-bottom: 1px solid var(--border);
	}

	/* Inline empty states */
	.card-empty-state-inline {
		display: flex;
		align-items: center;
		gap: var(--space-2);
		padding: var(--space-3);
		background: var(--muted);
		border-radius: var(--radius);
	}

	.card-empty-icon-small {
		width: 16px;
		height: 16px;
		color: var(--muted-foreground);
	}

	.card-empty-title-small {
		font-size: var(--text-sm);
		color: var(--muted-foreground);
		margin: 0;
	}

	/* Footer buttons */
	.footer-buttons {
		display: flex;
		gap: var(--space-2);
	}
</style>
