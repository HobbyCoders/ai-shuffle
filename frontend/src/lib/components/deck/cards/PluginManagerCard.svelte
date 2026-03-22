<script lang="ts">
	/**
	 * PluginManagerCard - Plugin management card for The Deck
	 *
	 * Features:
	 * - Marketplace management (view, add, remove, sync)
	 * - Browse and install plugins from all marketplaces
	 * - Manage installed plugins (enable/disable, uninstall)
	 * - Plugin details view with readme, commands, agents, skills
	 * - Batch install selection
	 */
	import FeaturePageLayout from '$lib/components/layout/FeaturePageLayout.svelte';
	import { onMount } from 'svelte';
	import {
		plugins,
		marketplaces,
		installedPlugins,
		selectedPlugin,
		pluginsLoading,
		pluginsError,
		marketplaceSyncing,
		pluginInstalling,
		uninstalledAvailablePlugins,
		uninstalledPluginsByMarketplace,
		pluginStats
	} from '$lib/stores/plugins';
	import type { AvailablePlugin, PluginInfo, MarketplaceInfo } from '$lib/api/plugins';
	import PluginListItem from '$lib/components/plugins/PluginListItem.svelte';
	import MarketplaceList from '$lib/components/plugins/MarketplaceList.svelte';
	import PluginDetails from '$lib/components/plugins/PluginDetails.svelte';
	import { Search, X, Loader2, Package, FolderOpen, AlertCircle } from 'lucide-svelte';
	import './card-design-system.css';

	interface Props {
		mobile?: boolean;
	}

	let {
		mobile = false
	}: Props = $props();

	type Tab = 'marketplaces' | 'available' | 'installed' | 'details';
	let activeTab = $state<Tab>('installed');
	let previousTab = $state<Tab>('installed');
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

	// Filter available plugins (only uninstalled)
	const filteredAvailable = $derived(() => {
		let items = $uninstalledAvailablePlugins;

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
		previousTab = activeTab;
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
	const tabs = $derived([
		{ id: 'installed' as Tab, label: 'Installed', count: $pluginStats.totalInstalled },
		{ id: 'available' as Tab, label: 'Available', count: $pluginStats.totalAvailable },
		{ id: 'marketplaces' as Tab, label: 'Sources', count: $pluginStats.totalMarketplaces }
	]);
</script>

{#snippet pluginMainContent()}
	<!-- Content -->
	<div class="plugin-content">
				{#if $pluginsError}
					<div class="plugin-error">
						<AlertCircle size={16} />
						<span>{$pluginsError}</span>
						<button onclick={() => plugins.clearError()} class="error-close">
							<X size={14} />
						</button>
					</div>
				{/if}

				{#if $pluginsLoading}
					<div class="plugin-loading">
						<Loader2 size={32} class="animate-spin" />
						<p>Loading plugins...</p>
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
					<div class="plugin-list">
						{#if filteredAvailable().length === 0}
							<div class="empty-state">
								<div class="empty-icon">
									<Package size={24} />
								</div>
								<p>No plugins found</p>
								{#if searchQuery}
									<button onclick={() => searchQuery = ''} class="clear-search-btn">
										Clear search
									</button>
								{/if}
							</div>
						{:else}
							{#each Object.entries($uninstalledPluginsByMarketplace) as [marketplace, marketplacePlugins]}
								{@const filtered = marketplacePlugins.filter(p =>
									(!selectedMarketplaceFilter || p.marketplace === selectedMarketplaceFilter) &&
									(!searchQuery.trim() ||
										p.name.toLowerCase().includes(searchQuery.toLowerCase()) ||
										p.description.toLowerCase().includes(searchQuery.toLowerCase())
									)
								)}
								{#if filtered.length > 0}
									<div class="marketplace-section">
										<h3 class="section-title">
											<FolderOpen size={16} />
											{marketplace}
										</h3>
										<div class="plugin-grid">
											{#each filtered as plugin (plugin.id)}
												<PluginListItem
													{plugin}
													installing={$pluginInstalling[plugin.id] || false}
													selected={selectedForInstall.has(plugin.id)}
													onInstall={() => {
														plugins.installPlugin(plugin.id);
														// Remove from selection if it was selected (fixes count getting stuck)
														if (selectedForInstall.has(plugin.id)) {
															const newSet = new Set(selectedForInstall);
															newSet.delete(plugin.id);
															selectedForInstall = newSet;
														}
													}}
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
					<div class="plugin-list">
						{#if filteredInstalled().length === 0}
							<div class="empty-state">
								<div class="empty-icon">
									<Package size={24} />
								</div>
								<p>No plugins installed</p>
								<button
									onclick={() => activeTab = 'available'}
									class="clear-search-btn"
								>
									Browse available plugins
								</button>
							</div>
						{:else}
							<div class="plugin-grid">
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
							</div>
						{/if}
					</div>
				{:else if activeTab === 'details' && $selectedPlugin}
					<PluginDetails
						plugin={$selectedPlugin}
						onClose={() => {
							plugins.clearSelectedPlugin();
							activeTab = previousTab;
						}}
						onInstall={() => {
							plugins.installPlugin($selectedPlugin.id);
						}}
						installing={$pluginInstalling[$selectedPlugin.id] || false}
					/>
				{/if}
			</div>

	<!-- Add Marketplace Modal -->
	{#if showAddMarketplace}
		<div
			class="add-marketplace-overlay"
			role="dialog"
			aria-modal="true"
		>
			<button class="add-marketplace-overlay-close" onclick={() => (showAddMarketplace = false)} tabindex="-1" aria-label="Close dialog"></button>
			<div class="add-marketplace-modal">
				<h3>Add Plugin Source</h3>

				<div class="form-group">
					<label for="plugin-source-name">Name</label>
					<input
						id="plugin-source-name"
						type="text"
						bind:value={newMarketplaceName}
						placeholder="e.g., my-plugins"
						class="form-input"
					/>
				</div>

				<div class="form-group">
					<label for="plugin-source-url">Git URL</label>
					<input
						id="plugin-source-url"
						type="text"
						bind:value={newMarketplaceUrl}
						placeholder="https://github.com/user/plugins.git"
						class="form-input"
					/>
					<p class="form-hint">
						Enter the git repository URL containing plugins
					</p>
				</div>

				<div class="modal-actions">
					<button
						onclick={() => showAddMarketplace = false}
						class="btn-secondary"
					>
						Cancel
					</button>
					<button
						onclick={handleAddMarketplace}
						disabled={!newMarketplaceUrl.trim() || !newMarketplaceName.trim() || addingMarketplace}
						class="btn-primary"
					>
						{#if addingMarketplace}
							<Loader2 size={16} class="animate-spin" />
						{/if}
						Add Source
					</button>
				</div>
			</div>
		</div>
	{/if}
{/snippet}

<FeaturePageLayout
	icon={Package}
	title="Plugins"
	itemCount={$pluginStats.totalInstalled}
	itemLabel="installed"
>
	{#snippet headerTabs()}
		<div class="fp-tab-bar">
			{#each tabs as tab}
				<button
					class="fp-tab"
					class:active={activeTab === tab.id}
					onclick={() => {
						activeTab = tab.id;
						if (tab.id !== 'details') {
							plugins.clearSelectedPlugin();
						}
					}}
				>
					{tab.label}
					<span class="fp-tab-count">{tab.count}</span>
				</button>
			{/each}
			{#if $selectedPlugin}
				<button
					class="fp-tab"
					class:active={activeTab === 'details'}
					onclick={() => activeTab = 'details'}
				>
					Details
				</button>
			{/if}
		</div>
	{/snippet}
	{#snippet footer()}
		{#if activeTab === 'available'}
			{#if selectedForInstall.size > 0}
				<button
					onclick={handleBatchInstall}
					disabled={batchInstalling}
					class="batch-install-btn"
				>
					{#if batchInstalling}
						<Loader2 size={16} class="animate-spin" />
					{/if}
					Install ({selectedForInstall.size})
				</button>
			{/if}
		{/if}
	{/snippet}
	{#snippet children()}
		<div class="plugin-card card-system">
			{#if activeTab === 'available' || activeTab === 'installed'}
				<div class="fp-search-bar">
					<div class="fph-search-wrapper">
						<Search size={14} />
						<input
							type="text"
							bind:value={searchQuery}
							placeholder="Search plugins..."
							class="fph-search-input"
						/>
					</div>
				</div>
			{/if}
			{@render pluginMainContent()}
		</div>
	{/snippet}
</FeaturePageLayout>

<style>
	.plugin-card {
		display: flex;
		flex-direction: column;
		height: 100%;
		overflow: hidden;
	}

	.batch-install-btn {
		display: flex;
		align-items: center;
		gap: 6px;
		padding: 8px 16px;
		font-size: 0.875rem;
		font-weight: 500;
		border-radius: 6px;
		background: var(--primary);
		color: var(--primary-foreground);
		border: none;
		cursor: pointer;
		transition: all 150ms ease;
		white-space: nowrap;
	}

	.batch-install-btn:hover:not(:disabled) {
		filter: brightness(1.1);
	}

	.batch-install-btn:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
	}

	.batch-install-btn:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	/* Content */
	.plugin-content {
		flex: 1;
		overflow-y: auto;
	}

	.plugin-error {
		display: flex;
		align-items: center;
		gap: 8px;
		margin: 16px;
		padding: 12px;
		border-radius: 8px;
		background: color-mix(in oklch, var(--destructive) 15%, transparent);
		color: var(--destructive);
		font-size: 0.875rem;
	}

	.error-close {
		margin-left: auto;
		padding: 4px;
		background: transparent;
		border: none;
		color: var(--destructive);
		cursor: pointer;
		border-radius: 4px;
		transition: opacity 150ms ease;
	}

	.error-close:hover {
		opacity: 0.7;
	}

	.plugin-loading {
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		padding: 48px;
		color: var(--muted-foreground);
		gap: 12px;
	}

	.plugin-loading :global(svg) {
		color: var(--primary);
	}

	.plugin-list {
		padding: 16px;
	}

	.marketplace-section {
		margin-bottom: 20px;
	}

	.section-title {
		display: flex;
		align-items: center;
		gap: 8px;
		font-size: 0.8125rem;
		font-weight: 500;
		color: var(--muted-foreground);
		margin-bottom: 12px;
	}

	/* Plugin grid is defined in card-design-system.css */

	.empty-state {
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		padding: 48px 24px;
		text-align: center;
		color: var(--muted-foreground);
	}

	.empty-icon {
		width: 48px;
		height: 48px;
		display: flex;
		align-items: center;
		justify-content: center;
		background: var(--muted);
		border-radius: 50%;
		margin-bottom: 12px;
	}

	.clear-search-btn {
		margin-top: 8px;
		color: var(--primary);
		background: transparent;
		border: none;
		cursor: pointer;
		font-size: 0.875rem;
		transition: all 150ms ease;
	}

	.clear-search-btn:hover {
		text-decoration: underline;
	}

	/* Add Marketplace Modal */
	.add-marketplace-overlay {
		position: fixed;
		inset: 0;
		z-index: 100;
		display: flex;
		align-items: center;
		justify-content: center;
		padding: 16px;
	}

	.add-marketplace-overlay-close {
		position: absolute;
		inset: 0;
		width: 100%;
		height: 100%;
		background: color-mix(in oklch, var(--background) 80%, transparent);
		backdrop-filter: blur(8px);
		-webkit-backdrop-filter: blur(8px);
		border: none;
		cursor: default;
		padding: 0;
	}

	.add-marketplace-modal {
		position: relative;
		z-index: 1;
		width: 100%;
		max-width: 400px;
		padding: 24px;
		background: var(--card);
		border: 1px solid var(--border);
		border-radius: 12px;
		box-shadow: var(--shadow-l);
	}

	.add-marketplace-modal h3 {
		font-size: 1.125rem;
		font-weight: 600;
		color: var(--foreground);
		margin-bottom: 20px;
	}

	.form-group {
		margin-bottom: 16px;
	}

	.form-group label {
		display: block;
		font-size: 0.875rem;
		font-weight: 500;
		color: var(--muted-foreground);
		margin-bottom: 6px;
	}

	.form-input {
		width: 100%;
		padding: 10px 12px;
		border-radius: 6px;
		background: var(--input);
		border: 1px solid var(--border);
		color: var(--foreground);
		font-size: 0.875rem;
		transition: all 150ms ease;
	}

	.form-input::placeholder {
		color: var(--muted-foreground);
	}

	.form-input:focus {
		outline: none;
		border-color: var(--ring);
		box-shadow: 0 0 0 2px color-mix(in oklch, var(--ring) 25%, transparent);
	}

	.form-hint {
		font-size: 0.75rem;
		color: var(--muted-foreground);
		margin-top: 6px;
	}

	.modal-actions {
		display: flex;
		justify-content: flex-end;
		gap: 8px;
		margin-top: 24px;
	}

	.btn-secondary,
	.btn-primary {
		display: flex;
		align-items: center;
		gap: 6px;
		padding: 10px 16px;
		font-size: 0.875rem;
		font-weight: 500;
		border-radius: 6px;
		cursor: pointer;
		transition: all 150ms ease;
		border: none;
	}

	.btn-secondary {
		background: var(--secondary);
		color: var(--foreground);
		border: 1px solid var(--border);
	}

	.btn-secondary:hover {
		border-color: var(--accent);
		box-shadow: 0 0 8px color-mix(in oklch, var(--accent) 20%, transparent);
	}

	.btn-secondary:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
	}

	.btn-primary {
		background: var(--primary);
		color: var(--primary-foreground);
	}

	.btn-primary:hover:not(:disabled) {
		filter: brightness(1.1);
	}

	.btn-primary:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
	}

	.btn-primary:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	/* Spinner animation */
	:global(.animate-spin) {
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

	.fp-search-bar {
		max-width: 640px;
		width: 100%;
		margin: 0 auto;
	}

	.fp-search-bar :global(.fph-search-wrapper) {
		width: 100%;
	}

	.fp-search-bar :global(.fph-search-input) {
		width: 100% !important;
	}
</style>
