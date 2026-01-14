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
	import { Search, Package, Download, Trash2, X, Loader2, ToggleLeft, ToggleRight, FolderGit2, FolderLock } from 'lucide-svelte';
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

	// Load plugins when project changes
	$effect(() => {
		if (projectId) {
			projectPlugins.loadAll(projectId);
			globalPlugins.loadMarketplaces();
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

			<!-- Empty state -->
			{#if $projectPluginStats.totalInstalled === 0}
				<div class="card-empty-state">
					<Package class="card-empty-icon" />
					<p class="card-empty-title">No plugins installed</p>
					<p class="card-empty-description">
						Install plugins to add commands, agents, and hooks to this project.
					</p>
				</div>
			{/if}
		</div>

		<!-- Install button -->
		<div class="tab-footer">
			<button onclick={() => (showInstallModal = true)} class="card-btn-primary">
				<Download />
				Install Plugin
			</button>
		</div>
	{/if}
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

	.tab-footer {
		padding: var(--space-3) var(--space-4);
		border-top: 1px solid var(--border);
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
</style>
