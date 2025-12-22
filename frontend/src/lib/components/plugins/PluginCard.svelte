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

<div class="fixed inset-0 max-sm:bottom-[calc(4.5rem+env(safe-area-inset-bottom,0px))] z-50 flex items-center justify-center bg-black/50 backdrop-blur-sm">
	<div class="bg-background border border-border rounded-2xl shadow-2xl w-full max-w-4xl max-h-[85vh] max-sm:max-h-full flex flex-col">
		<!-- Header -->
		<div class="flex items-center justify-between px-5 py-4 border-b border-border">
			<div class="flex items-center gap-3">
				<div class="w-10 h-10 rounded-xl bg-primary/10 flex items-center justify-center">
					<svg class="w-5 h-5 text-primary" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 7v10c0 2.21 3.582 4 8 4s8-1.79 8-4V7M4 7c0 2.21 3.582 4 8 4s8-1.79 8-4M4 7c0-2.21 3.582-4 8-4s8 1.79 8 4m0 5c0 2.21-3.582 4-8 4s-8-1.79-8-4" />
					</svg>
				</div>
				<div>
					<h2 class="text-lg font-semibold text-foreground">Plugin Manager</h2>
					<p class="text-sm text-muted-foreground">
						{$pluginStats.totalEnabled} enabled of {$pluginStats.totalInstalled} installed
					</p>
				</div>
			</div>
			<button
				onclick={() => onClose?.()}
				class="p-1.5 rounded-lg text-muted-foreground hover:text-foreground hover:bg-hover-overlay transition-colors"
			>
				<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
				</svg>
			</button>
		</div>

		<!-- Tabs -->
		<div class="flex items-center gap-1 px-5 py-2 border-b border-border overflow-x-auto">
			{#each tabs as tab}
				<button
					onclick={() => {
						activeTab = tab.id;
						if (tab.id !== 'details') {
							plugins.clearSelectedPlugin();
						}
					}}
					class="px-4 py-2 text-sm font-medium rounded-lg transition-colors whitespace-nowrap flex items-center gap-2 {activeTab === tab.id ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:text-foreground hover:bg-hover-overlay'}"
				>
					{tab.label}
					<span class="px-1.5 py-0.5 text-xs rounded-full {activeTab === tab.id ? 'bg-primary-foreground/20 text-primary-foreground' : 'bg-muted text-muted-foreground'}">
						{tab.count}
					</span>
				</button>
			{/each}

			{#if $selectedPlugin}
				<button
					onclick={() => activeTab = 'details'}
					class="px-4 py-2 text-sm font-medium rounded-lg transition-colors whitespace-nowrap {activeTab === 'details' ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:text-foreground hover:bg-hover-overlay'}"
				>
					Details
				</button>
			{/if}
		</div>

		<!-- Search bar (for available/installed tabs) -->
		{#if activeTab === 'available' || activeTab === 'installed'}
			<div class="px-5 py-3 border-b border-border flex items-center gap-3">
				<div class="relative flex-1">
					<svg class="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
					</svg>
					<input
						type="text"
						bind:value={searchQuery}
						placeholder="Search plugins..."
						class="w-full pl-9 pr-3 py-2 rounded-lg bg-muted/50 border border-border text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/50"
					/>
				</div>

				{#if activeTab === 'available'}
					<select
						bind:value={selectedMarketplaceFilter}
						class="px-3 py-2 rounded-lg bg-muted/50 border border-border text-foreground focus:outline-none focus:ring-2 focus:ring-primary/50"
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
							class="px-4 py-2 text-sm font-medium text-primary-foreground bg-primary rounded-lg hover:bg-primary/90 transition-colors disabled:opacity-50 flex items-center gap-2"
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
		<div class="flex-1 overflow-y-auto">
			{#if $pluginsError}
				<div class="m-5 p-3 rounded-lg bg-destructive/10 text-destructive text-sm flex items-center justify-between">
					<span>{$pluginsError}</span>
					<button onclick={() => plugins.clearError()} class="text-destructive hover:text-destructive/80">
						<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
						</svg>
					</button>
				</div>
			{/if}

			{#if $pluginsLoading}
				<div class="flex items-center justify-center py-12">
					<div class="flex flex-col items-center gap-3">
						<svg class="w-8 h-8 animate-spin text-primary" fill="none" viewBox="0 0 24 24">
							<circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
							<path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
						</svg>
						<p class="text-sm text-muted-foreground">Loading plugins...</p>
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
							<div class="w-12 h-12 mx-auto mb-3 rounded-full bg-muted flex items-center justify-center">
								<svg class="w-6 h-6 text-muted-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M20 13V6a2 2 0 00-2-2H6a2 2 0 00-2 2v7m16 0v5a2 2 0 01-2 2H6a2 2 0 01-2-2v-5m16 0h-2.586a1 1 0 00-.707.293l-2.414 2.414a1 1 0 01-.707.293h-3.172a1 1 0 01-.707-.293l-2.414-2.414A1 1 0 006.586 13H4" />
								</svg>
							</div>
							<p class="text-muted-foreground">No plugins found</p>
							{#if searchQuery}
								<button onclick={() => searchQuery = ''} class="text-primary hover:underline mt-2">
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
									<h3 class="text-sm font-medium text-muted-foreground mb-2 flex items-center gap-2">
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
							<div class="w-12 h-12 mx-auto mb-3 rounded-full bg-muted flex items-center justify-center">
								<svg class="w-6 h-6 text-muted-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 7v10c0 2.21 3.582 4 8 4s8-1.79 8-4V7M4 7c0 2.21 3.582 4 8 4s8-1.79 8-4M4 7c0-2.21 3.582-4 8-4s8 1.79 8 4" />
								</svg>
							</div>
							<p class="text-muted-foreground mb-2">No plugins installed</p>
							<button
								onclick={() => activeTab = 'available'}
								class="text-primary hover:underline"
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
		<div class="flex justify-between items-center px-5 py-4 border-t border-border">
			<div class="text-sm text-muted-foreground">
				{$pluginStats.totalFileBasedAgents} agents from plugins
			</div>
			<button
				onclick={() => onClose?.()}
				class="px-4 py-2 rounded-lg bg-muted/50 text-foreground font-medium text-sm hover:bg-hover-overlay transition-colors"
			>
				Close
			</button>
		</div>
	</div>
</div>

<!-- Add Marketplace Modal -->
{#if showAddMarketplace}
	<div
		class="fixed inset-0 z-[60] bg-black/50 flex items-center justify-center p-4"
		onclick={(e) => e.target === e.currentTarget && (showAddMarketplace = false)}
		role="dialog"
		aria-modal="true"
	>
		<div class="bg-card border border-border rounded-lg shadow-xl w-full max-w-md p-5">
			<h3 class="text-lg font-semibold text-foreground mb-4">Add Plugin Source</h3>

			<div class="space-y-4">
				<div class="space-y-2">
					<label class="text-sm font-medium text-muted-foreground">Name</label>
					<input
						type="text"
						bind:value={newMarketplaceName}
						placeholder="e.g., my-plugins"
						class="w-full px-3 py-2 rounded-lg bg-muted/50 border border-border text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/50"
					/>
				</div>

				<div class="space-y-2">
					<label class="text-sm font-medium text-muted-foreground">Git URL</label>
					<input
						type="text"
						bind:value={newMarketplaceUrl}
						placeholder="https://github.com/user/plugins.git"
						class="w-full px-3 py-2 rounded-lg bg-muted/50 border border-border text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/50"
					/>
					<p class="text-xs text-muted-foreground">
						Enter the git repository URL containing plugins
					</p>
				</div>
			</div>

			<div class="flex justify-end gap-2 mt-6">
				<button
					onclick={() => showAddMarketplace = false}
					class="px-4 py-2 rounded-lg bg-muted/50 text-foreground font-medium text-sm hover:bg-hover-overlay transition-colors"
				>
					Cancel
				</button>
				<button
					onclick={handleAddMarketplace}
					disabled={!newMarketplaceUrl.trim() || !newMarketplaceName.trim() || addingMarketplace}
					class="px-4 py-2 rounded-lg bg-primary text-primary-foreground font-medium text-sm disabled:opacity-50 hover:bg-primary/90 transition-colors flex items-center gap-2"
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
