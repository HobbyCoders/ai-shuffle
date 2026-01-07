<script lang="ts">
	/**
	 * PluginCard - Main plugin management interface
	 *
	 * Provides comprehensive plugin management with tabs for:
	 * - Marketplaces: View, add, remove, and sync plugin marketplaces
	 * - Available: Browse and install plugins from all marketplaces
	 * - Installed: Manage installed plugins, enable/disable, uninstall
	 * - Details: View selected plugin's readme, commands, agents, skills
	 */
	import { onMount } from 'svelte';
	import {
		plugins,
		marketplaces,
		availablePlugins,
		installedPlugins,
		selectedPlugin,
		pluginsLoading,
		pluginsError,
		marketplaceSyncing,
		pluginInstalling,
		pluginsByMarketplace,
		pluginStats
	} from '$lib/stores/plugins';
	import type { AvailablePlugin, PluginInfo, MarketplaceInfo } from '$lib/api/plugins';
	import PluginListItem from './PluginListItem.svelte';
	import MarketplaceList from './MarketplaceList.svelte';
	import PluginDetails from './PluginDetails.svelte';

	interface Props {
		onClose?: () => void;
	}

	let { onClose }: Props = $props();

	type Tab = 'marketplaces' | 'available' | 'installed' | 'details';
	let activeTab = $state<Tab>('installed');
	let searchQuery = $state('');
	let selectedMarketplaceFilter = $state<string | ''>('');

	// Batch install selection
	let selectedForInstall = $state<Set<string>>(new Set());
	let batchInstalling = $state(false);

	// Add marketplace form
	let showAddMarketplace = $state(false);
	let newMarketplaceUrl = $state('');
	let newMarketplaceName = $state('');
	let addingMarketplace = $state(false);

	// Load data on mount
	onMount(() => {
		plugins.loadAll();
	});

	// Filter available plugins
	const filteredAvailable = $derived(() => {
		let items = $availablePlugins;

		// Filter by marketplace
		if (selectedMarketplaceFilter) {
			items = items.filter((p) => p.marketplace === selectedMarketplaceFilter);
		}

		// Filter by search query
		if (searchQuery.trim()) {
			const query = searchQuery.toLowerCase();
			items = items.filter(
				(p) =>
					p.name.toLowerCase().includes(query) ||
					p.description.toLowerCase().includes(query) ||
					p.marketplace.toLowerCase().includes(query)
			);
		}

		return items;
	});

	// Filter installed plugins
	const filteredInstalled = $derived(() => {
		let items = $installedPlugins;

		if (searchQuery.trim()) {
			const query = searchQuery.toLowerCase();
			items = items.filter(
				(p) =>
					p.name.toLowerCase().includes(query) ||
					p.description.toLowerCase().includes(query) ||
					p.marketplace.toLowerCase().includes(query)
			);
		}

		return items;
	});

	// Handle plugin selection
	async function handleSelectPlugin(pluginId: string) {
		await plugins.selectPlugin(pluginId);
		activeTab = 'details';
	}

	// Handle batch install
	async function handleBatchInstall() {
		if (selectedForInstall.size === 0) return;

		batchInstalling = true;
		try {
			await plugins.installPluginsBatch(Array.from(selectedForInstall));
			selectedForInstall = new Set();
		} catch (e) {
			console.error('Batch install failed:', e);
		} finally {
			batchInstalling = false;
		}
	}

	// Toggle plugin selection for batch install
	function togglePluginSelection(pluginId: string) {
		const newSet = new Set(selectedForInstall);
		if (newSet.has(pluginId)) {
			newSet.delete(pluginId);
		} else {
			newSet.add(pluginId);
		}
		selectedForInstall = newSet;
	}

	// Add marketplace
	async function handleAddMarketplace() {
		if (!newMarketplaceUrl.trim() || !newMarketplaceName.trim()) return;

		addingMarketplace = true;
		try {
			await plugins.addMarketplace(newMarketplaceUrl.trim(), newMarketplaceName.trim());
			showAddMarketplace = false;
			newMarketplaceUrl = '';
			newMarketplaceName = '';
			// Reload available plugins
			await plugins.loadAvailable();
		} catch (e) {
			console.error('Failed to add marketplace:', e);
		} finally {
			addingMarketplace = false;
		}
	}

	// Tab definitions
	const tabs = [
		{ id: 'installed' as Tab, label: 'Installed', count: $pluginStats.totalInstalled },
		{ id: 'available' as Tab, label: 'Available', count: $pluginStats.totalAvailable },
		{ id: 'marketplaces' as Tab, label: 'Sources', count: $pluginStats.totalMarketplaces }
	];
</script>

<div class="fixed inset-0 max-sm:bottom-[calc(4.5rem+env(safe-area-inset-bottom,0px))] z-50 flex items-center justify-center plugin-overlay">
	<div class="plugin-modal w-full max-w-4xl max-h-[85vh] max-sm:max-h-full flex flex-col">
		<!-- Header -->
		<div class="flex items-center justify-between px-5 py-4 plugin-header">
			<div class="flex items-center gap-3">
				<div class="plugin-icon-container">
					<svg class="w-5 h-5" style="color: var(--primary);" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 7v10c0 2.21 3.582 4 8 4s8-1.79 8-4V7M4 7c0 2.21 3.582 4 8 4s8-1.79 8-4M4 7c0-2.21 3.582-4 8-4s8 1.79 8 4m0 5c0 2.21-3.582 4-8 4s-8-1.79-8-4" />
					</svg>
				</div>
				<div>
					<h2 class="text-lg font-semibold" style="color: var(--foreground);">Plugin Manager</h2>
					<p class="text-sm" style="color: var(--muted-foreground);">
						{$pluginStats.totalEnabled} enabled of {$pluginStats.totalInstalled} installed
					</p>
				</div>
			</div>
			<button
				onclick={() => onClose?.()}
				class="plugin-close-btn"
			>
				<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
				</svg>
			</button>
		</div>

		<!-- Tabs -->
		<div class="flex items-center gap-1 px-5 py-2 plugin-tabs overflow-x-auto">
			{#each tabs as tab}
				<button
					onclick={() => {
						activeTab = tab.id;
						if (tab.id !== 'details') {
							plugins.clearSelectedPlugin();
						}
					}}
					class="plugin-tab {activeTab === tab.id ? 'plugin-tab-active' : ''}"
				>
					{tab.label}
					<span class="plugin-tab-count {activeTab === tab.id ? 'plugin-tab-count-active' : ''}">
						{tab.count}
					</span>
				</button>
			{/each}

			{#if $selectedPlugin}
				<button
					onclick={() => activeTab = 'details'}
					class="plugin-tab {activeTab === 'details' ? 'plugin-tab-active' : ''}"
				>
					Details
				</button>
			{/if}
		</div>

		<!-- Search bar (for available/installed tabs) -->
		{#if activeTab === 'available' || activeTab === 'installed'}
			<div class="px-5 py-3 plugin-search-bar flex items-center gap-3">
				<div class="relative flex-1">
					<svg class="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4" style="color: var(--muted-foreground);" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
					</svg>
					<input
						type="text"
						bind:value={searchQuery}
						placeholder="Search plugins..."
						class="plugin-search-input w-full pl-9 pr-3 py-2"
					/>
				</div>

				{#if activeTab === 'available'}
					<select
						bind:value={selectedMarketplaceFilter}
						class="plugin-select px-3 py-2"
					>
						<option value="">All Sources</option>
						{#each $marketplaces as marketplace}
							<option value={marketplace.id}>{marketplace.id}</option>
						{/each}
					</select>

					{#if selectedForInstall.size > 0}
						<button
							onclick={handleBatchInstall}
							disabled={batchInstalling}
							class="plugin-install-btn flex items-center gap-2"
						>
							{#if batchInstalling}
								<svg class="w-4 h-4 animate-spin" fill="none" viewBox="0 0 24 24">
									<circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
									<path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
								</svg>
							{/if}
							Install ({selectedForInstall.size})
						</button>
					{/if}
				{/if}
			</div>
		{/if}

		<!-- Content -->
		<div class="flex-1 overflow-y-auto plugin-content">
			{#if $pluginsError}
				<div class="m-5 p-3 rounded-lg plugin-error flex items-center justify-between">
					<span>{$pluginsError}</span>
					<button onclick={() => plugins.clearError()} class="plugin-error-close">
						<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
						</svg>
					</button>
				</div>
			{/if}

			{#if $pluginsLoading}
				<div class="flex items-center justify-center py-12">
					<div class="flex flex-col items-center gap-3">
						<svg class="w-8 h-8 animate-spin" style="color: var(--primary);" fill="none" viewBox="0 0 24 24">
							<circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
							<path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
						</svg>
						<p class="text-sm" style="color: var(--muted-foreground);">Loading plugins...</p>
					</div>
				</div>
			{:else if activeTab === 'marketplaces'}
				<MarketplaceList
					marketplaces={$marketplaces}
					syncing={$marketplaceSyncing}
					onSync={(id) => plugins.syncMarketplace(id)}
					onRemove={(id) => plugins.removeMarketplace(id)}
					onAdd={() => showAddMarketplace = true}
				/>
			{:else if activeTab === 'available'}
				<div class="p-5 space-y-4">
					{#if filteredAvailable().length === 0}
						<div class="text-center py-8">
							<div class="plugin-empty-icon">
								<svg class="w-6 h-6" style="color: var(--muted-foreground);" fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M20 13V6a2 2 0 00-2-2H6a2 2 0 00-2 2v7m16 0v5a2 2 0 01-2 2H6a2 2 0 01-2-2v-5m16 0h-2.586a1 1 0 00-.707.293l-2.414 2.414a1 1 0 01-.707.293h-3.172a1 1 0 01-.707-.293l-2.414-2.414A1 1 0 006.586 13H4" />
								</svg>
							</div>
							<p style="color: var(--muted-foreground);">No plugins found</p>
							{#if searchQuery}
								<button onclick={() => searchQuery = ''} class="plugin-clear-search mt-2">
									Clear search
								</button>
							{/if}
						</div>
					{:else}
						{#each Object.entries($pluginsByMarketplace) as [marketplace, marketplacePlugins]}
							{@const filtered = marketplacePlugins.filter(p =>
								(!selectedMarketplaceFilter || p.marketplace === selectedMarketplaceFilter) &&
								(!searchQuery.trim() ||
									p.name.toLowerCase().includes(searchQuery.toLowerCase()) ||
									p.description.toLowerCase().includes(searchQuery.toLowerCase())
								)
							)}
							{#if filtered.length > 0}
								<div>
									<h3 class="text-sm font-medium mb-2 flex items-center gap-2 plugin-section-title">
										<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z" />
										</svg>
										{marketplace}
									</h3>
									<div class="space-y-2">
										{#each filtered as plugin (plugin.id)}
											<PluginListItem
												{plugin}
												installing={$pluginInstalling[plugin.id] || false}
												selected={selectedForInstall.has(plugin.id)}
												onInstall={() => plugins.installPlugin(plugin.id)}
												onSelect={() => togglePluginSelection(plugin.id)}
												onDetails={() => handleSelectPlugin(plugin.id)}
												mode="available"
											/>
										{/each}
									</div>
								</div>
							{/if}
						{/each}
					{/if}
				</div>
			{:else if activeTab === 'installed'}
				<div class="p-5 space-y-2">
					{#if filteredInstalled().length === 0}
						<div class="text-center py-8">
							<div class="plugin-empty-icon">
								<svg class="w-6 h-6" style="color: var(--muted-foreground);" fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 7v10c0 2.21 3.582 4 8 4s8-1.79 8-4V7M4 7c0 2.21 3.582 4 8 4s8-1.79 8-4M4 7c0-2.21 3.582-4 8-4s8 1.79 8 4" />
								</svg>
							</div>
							<p style="color: var(--muted-foreground);" class="mb-2">No plugins installed</p>
							<button
								onclick={() => activeTab = 'available'}
								class="plugin-clear-search"
							>
								Browse available plugins
							</button>
						</div>
					{:else}
						{#each filteredInstalled() as plugin (plugin.id)}
							<PluginListItem
								{plugin}
								installing={$pluginInstalling[plugin.id] || false}
								onToggle={() => plugins.togglePlugin(plugin.id)}
								onUninstall={() => plugins.uninstallPlugin(plugin.id)}
								onDetails={() => handleSelectPlugin(plugin.id)}
								mode="installed"
							/>
						{/each}
					{/if}
				</div>
			{:else if activeTab === 'details' && $selectedPlugin}
				<PluginDetails
					plugin={$selectedPlugin}
					onClose={() => {
						plugins.clearSelectedPlugin();
						activeTab = 'installed';
					}}
				/>
			{/if}
		</div>

		<!-- Footer -->
		<div class="flex justify-between items-center px-5 py-4 plugin-footer">
			<div class="text-sm" style="color: var(--muted-foreground);">
				{$pluginStats.totalFileBasedAgents} agents from plugins
			</div>
			<button
				onclick={() => onClose?.()}
				class="plugin-footer-btn"
			>
				Close
			</button>
		</div>
	</div>
</div>

<!-- Add Marketplace Modal -->
{#if showAddMarketplace}
	<div
		class="fixed inset-0 z-[60] plugin-overlay flex items-center justify-center p-4"
		onclick={(e) => e.target === e.currentTarget && (showAddMarketplace = false)}
		role="dialog"
		aria-modal="true"
	>
		<div class="plugin-add-modal w-full max-w-md p-5">
			<h3 class="text-lg font-semibold mb-4" style="color: var(--foreground);">Add Plugin Source</h3>

			<div class="space-y-4">
				<div class="space-y-2">
					<label class="text-sm font-medium" style="color: var(--muted-foreground);">Name</label>
					<input
						type="text"
						bind:value={newMarketplaceName}
						placeholder="e.g., my-plugins"
						class="plugin-input w-full px-3 py-2"
					/>
				</div>

				<div class="space-y-2">
					<label class="text-sm font-medium" style="color: var(--muted-foreground);">Git URL</label>
					<input
						type="text"
						bind:value={newMarketplaceUrl}
						placeholder="https://github.com/user/plugins.git"
						class="plugin-input w-full px-3 py-2"
					/>
					<p class="text-xs" style="color: var(--muted-foreground);">
						Enter the git repository URL containing plugins
					</p>
				</div>
			</div>

			<div class="flex justify-end gap-2 mt-6">
				<button
					onclick={() => showAddMarketplace = false}
					class="plugin-footer-btn"
				>
					Cancel
				</button>
				<button
					onclick={handleAddMarketplace}
					disabled={!newMarketplaceUrl.trim() || !newMarketplaceName.trim() || addingMarketplace}
					class="plugin-primary-btn flex items-center gap-2"
				>
					{#if addingMarketplace}
						<svg class="w-4 h-4 animate-spin" fill="none" viewBox="0 0 24 24">
							<circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
							<path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
						</svg>
					{/if}
					Add Source
				</button>
			</div>
		</div>
	</div>
{/if}

<style>
	/* Overlay styles */
	.plugin-overlay {
		background-color: color-mix(in oklch, var(--background) 50%, transparent);
		backdrop-filter: blur(8px);
		-webkit-backdrop-filter: blur(8px);
	}

	/* Main modal styles */
	.plugin-modal {
		background-color: var(--card);
		border: 1px solid var(--border);
		border-radius: 1rem;
		box-shadow: var(--shadow-l);
	}

	/* Header section */
	.plugin-header {
		border-bottom: 1px solid var(--border);
	}

	.plugin-icon-container {
		width: 2.5rem;
		height: 2.5rem;
		border-radius: 0.75rem;
		background-color: color-mix(in oklch, var(--primary) 15%, transparent);
		display: flex;
		align-items: center;
		justify-content: center;
	}

	.plugin-close-btn {
		padding: 0.375rem;
		border-radius: 0.5rem;
		color: var(--muted-foreground);
		transition: all 150ms ease;
	}

	.plugin-close-btn:hover {
		color: var(--foreground);
		background-color: var(--hover-overlay);
	}

	/* Tabs section */
	.plugin-tabs {
		border-bottom: 1px solid var(--border);
	}

	.plugin-tab {
		padding: 0.5rem 1rem;
		font-size: 0.875rem;
		font-weight: 500;
		border-radius: 0.5rem;
		white-space: nowrap;
		display: flex;
		align-items: center;
		gap: 0.5rem;
		color: var(--muted-foreground);
		transition: all 150ms ease;
	}

	.plugin-tab:hover {
		color: var(--foreground);
		background-color: var(--hover-overlay);
	}

	.plugin-tab-active {
		background-color: var(--primary);
		color: var(--primary-foreground);
	}

	.plugin-tab-active:hover {
		background-color: var(--primary);
		color: var(--primary-foreground);
	}

	.plugin-tab-count {
		padding: 0.125rem 0.375rem;
		font-size: 0.75rem;
		border-radius: 9999px;
		background-color: var(--muted);
		color: var(--muted-foreground);
	}

	.plugin-tab-count-active {
		background-color: color-mix(in oklch, var(--primary-foreground) 20%, transparent);
		color: var(--primary-foreground);
	}

	/* Search bar */
	.plugin-search-bar {
		border-bottom: 1px solid var(--border);
	}

	.plugin-search-input {
		border-radius: 0.5rem;
		background-color: var(--input);
		border: 1px solid var(--border);
		color: var(--foreground);
		transition: all 150ms ease;
	}

	.plugin-search-input::placeholder {
		color: var(--muted-foreground);
	}

	.plugin-search-input:focus {
		outline: none;
		border-color: var(--ring);
		box-shadow: 0 0 0 2px color-mix(in oklch, var(--ring) 25%, transparent);
	}

	.plugin-select {
		border-radius: 0.5rem;
		background-color: var(--input);
		border: 1px solid var(--border);
		color: var(--foreground);
		transition: all 150ms ease;
	}

	.plugin-select:focus {
		outline: none;
		border-color: var(--ring);
		box-shadow: 0 0 0 2px color-mix(in oklch, var(--ring) 25%, transparent);
	}

	.plugin-install-btn {
		padding: 0.5rem 1rem;
		font-size: 0.875rem;
		font-weight: 500;
		border-radius: 0.5rem;
		background-color: var(--primary);
		color: var(--primary-foreground);
		transition: all 150ms ease;
	}

	.plugin-install-btn:hover:not(:disabled) {
		filter: brightness(1.1);
		box-shadow: var(--shadow-s);
	}

	.plugin-install-btn:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	/* Content area */
	.plugin-content {
		background-color: var(--background);
	}

	.plugin-error {
		background-color: color-mix(in oklch, var(--destructive) 15%, transparent);
		color: var(--destructive);
		font-size: 0.875rem;
	}

	.plugin-error-close {
		color: var(--destructive);
		transition: opacity 150ms ease;
	}

	.plugin-error-close:hover {
		opacity: 0.8;
	}

	.plugin-empty-icon {
		width: 3rem;
		height: 3rem;
		margin: 0 auto 0.75rem;
		border-radius: 9999px;
		background-color: var(--muted);
		display: flex;
		align-items: center;
		justify-content: center;
	}

	.plugin-clear-search {
		color: var(--primary);
		transition: all 150ms ease;
	}

	.plugin-clear-search:hover {
		text-decoration: underline;
	}

	.plugin-section-title {
		color: var(--muted-foreground);
	}

	/* Footer */
	.plugin-footer {
		border-top: 1px solid var(--border);
	}

	.plugin-footer-btn {
		padding: 0.5rem 1rem;
		border-radius: 0.5rem;
		background-color: var(--secondary);
		color: var(--foreground);
		font-weight: 500;
		font-size: 0.875rem;
		transition: all 150ms ease;
	}

	.plugin-footer-btn:hover {
		background-color: var(--accent);
	}

	/* Add marketplace modal */
	.plugin-add-modal {
		background-color: var(--card);
		border: 1px solid var(--border);
		border-radius: 0.75rem;
		box-shadow: var(--shadow-l);
	}

	.plugin-input {
		border-radius: 0.5rem;
		background-color: var(--input);
		border: 1px solid var(--border);
		color: var(--foreground);
		transition: all 150ms ease;
	}

	.plugin-input::placeholder {
		color: var(--muted-foreground);
	}

	.plugin-input:focus {
		outline: none;
		border-color: var(--ring);
		box-shadow: 0 0 0 2px color-mix(in oklch, var(--ring) 25%, transparent);
	}

	.plugin-primary-btn {
		padding: 0.5rem 1rem;
		border-radius: 0.5rem;
		background-color: var(--primary);
		color: var(--primary-foreground);
		font-weight: 500;
		font-size: 0.875rem;
		transition: all 150ms ease;
	}

	.plugin-primary-btn:hover:not(:disabled) {
		filter: brightness(1.1);
	}

	.plugin-primary-btn:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}
</style>
