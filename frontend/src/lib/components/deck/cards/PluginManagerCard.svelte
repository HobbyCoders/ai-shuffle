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
		pluginStats,
		pluginUpdating,
		updatesAvailable,
		updatesCount,
		autoUpdateSettings,
		checkingUpdates
	} from '$lib/stores/plugins.svelte';
	import type { AvailablePlugin, PluginInfo, MarketplaceInfo, PluginUpdateInfo, AutoUpdateSettings } from '$lib/api/plugins';
	import PluginListItem from '$lib/components/plugins/PluginListItem.svelte';
	import MarketplaceList from '$lib/components/plugins/MarketplaceList.svelte';
	import PluginDetails from '$lib/components/plugins/PluginDetails.svelte';
	import { Search, X, Loader2, Package, FolderOpen, AlertCircle, RefreshCw, ArrowUpCircle, Settings } from 'lucide-svelte';
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

	// Update all state
	let updatingAll = $state(false);

	// Auto-update settings panel
	let showAutoUpdateSettings = $state(false);

	// Build a lookup set of plugin IDs that have updates available
	const updateLookup = $derived(() => {
		const map = new Map<string, PluginUpdateInfo>();
		for (const u of $updatesAvailable) {
			map.set(u.plugin_id, u);
		}
		return map;
	});

	// Load data on mount
	onMount(() => {
		plugins.loadAll();
	});

	// Handle check for updates
	async function handleCheckForUpdates() {
		try {
			await plugins.checkForUpdates();
		} catch (e) {
			console.error('Failed to check for updates:', e);
		}
	}

	// Handle update all plugins
	async function handleUpdateAll() {
		updatingAll = true;
		try {
			await plugins.updateAllPlugins();
		} catch (e) {
			console.error('Failed to update all plugins:', e);
		} finally {
			updatingAll = false;
		}
	}

	// Handle auto-update settings toggle
	async function handleAutoUpdateToggle(field: keyof AutoUpdateSettings, value: boolean | number) {
		try {
			await plugins.updateAutoUpdateSettings({ [field]: value });
		} catch (e) {
			console.error('Failed to update auto-update settings:', e);
		}
	}

	// Format date for auto-update display
	function formatLastChecked(dateStr: string | null): string {
		if (!dateStr) return 'Never';
		try {
			const date = new Date(dateStr);
			const now = new Date();
			const diffMs = now.getTime() - date.getTime();
			const diffMins = Math.floor(diffMs / 60000);
			if (diffMins < 1) return 'Just now';
			if (diffMins < 60) return `${diffMins}m ago`;
			const diffHours = Math.floor(diffMins / 60);
			if (diffHours < 24) return `${diffHours}h ago`;
			const diffDays = Math.floor(diffHours / 24);
			return `${diffDays}d ago`;
		} catch {
			return 'Unknown';
		}
	}

	// Interval options for auto-update
	const intervalOptions = [
		{ value: 1, label: '1 hour' },
		{ value: 3, label: '3 hours' },
		{ value: 6, label: '6 hours' },
		{ value: 12, label: '12 hours' },
		{ value: 24, label: '24 hours' }
	];

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
		{ id: 'installed' as Tab, label: 'Installed', count: $pluginStats.totalInstalled, updatesBadge: $updatesCount },
		{ id: 'available' as Tab, label: 'Available', count: $pluginStats.totalAvailable, updatesBadge: 0 },
		{ id: 'marketplaces' as Tab, label: 'Sources', count: $pluginStats.totalMarketplaces, updatesBadge: 0 }
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

					<!-- Auto-Update Settings -->
					<div class="auto-update-section">
						<button
							class="auto-update-header"
							onclick={() => showAutoUpdateSettings = !showAutoUpdateSettings}
						>
							<div class="auto-update-header-left">
								<Settings size={16} />
								<span>Auto-Update Settings</span>
							</div>
							<svg
								class="auto-update-chevron {showAutoUpdateSettings ? 'auto-update-chevron--open' : ''}"
								width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor"
								stroke-width="2" stroke-linecap="round" stroke-linejoin="round"
							>
								<polyline points="6 9 12 15 18 9"></polyline>
							</svg>
						</button>

						{#if showAutoUpdateSettings}
							<div class="auto-update-body">
								{#if $autoUpdateSettings}
									<!-- Enable/Disable auto-update -->
									<div class="auto-update-row">
										<div class="auto-update-row-info">
											<span class="auto-update-row-label">Auto-updates</span>
											<span class="auto-update-row-desc">Automatically check and install updates</span>
										</div>
										<button
											class="auto-update-toggle"
											onclick={() => handleAutoUpdateToggle('enabled', !$autoUpdateSettings?.enabled)}
											title={$autoUpdateSettings.enabled ? 'Disable auto-updates' : 'Enable auto-updates'}
										>
											{#if $autoUpdateSettings.enabled}
												<span class="toggle-track toggle-track--on">
													<span class="toggle-thumb toggle-thumb--on"></span>
												</span>
											{:else}
												<span class="toggle-track">
													<span class="toggle-thumb"></span>
												</span>
											{/if}
										</button>
									</div>

									<!-- Check interval -->
									<div class="auto-update-row">
										<div class="auto-update-row-info">
											<span class="auto-update-row-label">Check interval</span>
											<span class="auto-update-row-desc">How often to check for updates</span>
										</div>
										<select
											class="auto-update-select"
											value={$autoUpdateSettings.interval_hours}
											onchange={(e) => handleAutoUpdateToggle('interval_hours', Number((e.target as HTMLSelectElement).value))}
											disabled={!$autoUpdateSettings.enabled}
										>
											{#each intervalOptions as opt}
												<option value={opt.value}>{opt.label}</option>
											{/each}
										</select>
									</div>

									<!-- Auto-sync marketplaces -->
									<div class="auto-update-row">
										<div class="auto-update-row-info">
											<span class="auto-update-row-label">Auto-sync sources</span>
											<span class="auto-update-row-desc">Pull latest from plugin sources before checking</span>
										</div>
										<button
											class="auto-update-toggle"
											onclick={() => handleAutoUpdateToggle('auto_sync_marketplaces', !$autoUpdateSettings?.auto_sync_marketplaces)}
											disabled={!$autoUpdateSettings?.enabled}
											title={$autoUpdateSettings.auto_sync_marketplaces ? 'Disable auto-sync' : 'Enable auto-sync'}
										>
											{#if $autoUpdateSettings.auto_sync_marketplaces}
												<span class="toggle-track toggle-track--on">
													<span class="toggle-thumb toggle-thumb--on"></span>
												</span>
											{:else}
												<span class="toggle-track">
													<span class="toggle-thumb"></span>
												</span>
											{/if}
										</button>
									</div>

									<!-- Auto-update plugins -->
									<div class="auto-update-row">
										<div class="auto-update-row-info">
											<span class="auto-update-row-label">Auto-install updates</span>
											<span class="auto-update-row-desc">Install updates automatically when found</span>
										</div>
										<button
											class="auto-update-toggle"
											onclick={() => handleAutoUpdateToggle('auto_update_plugins', !$autoUpdateSettings?.auto_update_plugins)}
											disabled={!$autoUpdateSettings?.enabled}
											title={$autoUpdateSettings.auto_update_plugins ? 'Disable auto-install' : 'Enable auto-install'}
										>
											{#if $autoUpdateSettings.auto_update_plugins}
												<span class="toggle-track toggle-track--on">
													<span class="toggle-thumb toggle-thumb--on"></span>
												</span>
											{:else}
												<span class="toggle-track">
													<span class="toggle-thumb"></span>
												</span>
											{/if}
										</button>
									</div>

									<!-- Last checked + Check now -->
									<div class="auto-update-footer">
										<span class="auto-update-last-checked">
											Last checked: {formatLastChecked($autoUpdateSettings.last_check)}
										</span>
										<button
											onclick={() => plugins.runAutoUpdate()}
											disabled={$checkingUpdates}
											class="check-now-btn"
										>
											{#if $checkingUpdates}
												<Loader2 size={14} class="animate-spin" />
											{:else}
												<RefreshCw size={14} />
											{/if}
											Check Now
										</button>
									</div>
								{:else}
									<div class="auto-update-loading">
										<span class="text-sm text-muted-foreground">Loading settings...</span>
									</div>
								{/if}
							</div>
						{/if}
					</div>
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
										{#each filtered as plugin, index (plugin.id)}
												{#if index > 0}<div class="fp-list-divider"></div>{/if}
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
								{/if}
							{/each}
						{/if}
					</div>
				{:else if activeTab === 'installed'}
					<!-- Check for updates toolbar -->
					<div class="updates-toolbar">
						<button
							onclick={handleCheckForUpdates}
							disabled={$checkingUpdates}
							class="check-updates-btn"
							title="Check for updates"
						>
							<RefreshCw size={14} class={$checkingUpdates ? 'animate-spin' : ''} />
							{#if $checkingUpdates}
								Checking...
							{:else}
								Check for updates
							{/if}
						</button>
						{#if $updatesCount > 0}
							<span class="updates-count-label">
								{$updatesCount} update{$updatesCount !== 1 ? 's' : ''} available
							</span>
						{/if}
					</div>

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
							{#each filteredInstalled() as plugin, index (plugin.id)}
								{#if index > 0}<div class="fp-list-divider"></div>{/if}
								<PluginListItem
									{plugin}
									installing={$pluginInstalling[plugin.id] || false}
									updateAvailable={updateLookup().has(plugin.id)}
									updating={$pluginUpdating[plugin.id] || false}
									onToggle={() => plugins.togglePlugin(plugin.id)}
									onUninstall={() => plugins.uninstallPlugin(plugin.id)}
									onUpdate={() => plugins.updatePlugin(plugin.id)}
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
					{#if tab.updatesBadge > 0}
						<span class="tab-updates-badge">{tab.updatesBadge}</span>
					{/if}
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
		{:else if activeTab === 'installed'}
			{#if $updatesCount > 0}
				<button
					onclick={handleUpdateAll}
					disabled={updatingAll}
					class="batch-install-btn batch-update-btn"
				>
					{#if updatingAll}
						<Loader2 size={16} class="animate-spin" />
					{:else}
						<ArrowUpCircle size={16} />
					{/if}
					Update All ({$updatesCount})
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

	/* ── Tab updates badge ─────────────────────────────────────────────── */
	.tab-updates-badge {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		min-width: 16px;
		height: 16px;
		padding: 0 4px;
		border-radius: 8px;
		background: #f59e0b;
		color: #1a1a1a;
		font-size: 0.625rem;
		font-weight: 700;
		line-height: 1;
		margin-left: 4px;
	}

	/* ── Updates toolbar (above installed list) ────────────────────────── */
	.updates-toolbar {
		display: flex;
		align-items: center;
		gap: 12px;
		padding: 8px 16px;
		border-bottom: 1px solid color-mix(in oklch, var(--border) 50%, transparent);
	}

	.check-updates-btn {
		display: flex;
		align-items: center;
		gap: 6px;
		padding: 6px 12px;
		font-size: 0.75rem;
		font-weight: 500;
		border-radius: 6px;
		background: transparent;
		color: var(--muted-foreground);
		border: 1px solid var(--border);
		cursor: pointer;
		transition: all 150ms ease;
		white-space: nowrap;
		min-height: 32px;
	}

	.check-updates-btn:hover:not(:disabled) {
		color: var(--foreground);
		border-color: var(--accent);
		background: color-mix(in oklch, var(--accent) 8%, transparent);
	}

	.check-updates-btn:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
	}

	.check-updates-btn:disabled {
		opacity: 0.6;
		cursor: not-allowed;
	}

	.updates-count-label {
		font-size: 0.75rem;
		font-weight: 500;
		color: #f59e0b;
	}

	/* ── Update All button variant ─────────────────────────────────────── */
	.batch-update-btn {
		background: #f59e0b;
		color: #1a1a1a;
	}

	.batch-update-btn:hover:not(:disabled) {
		filter: brightness(1.1);
	}

	/* ── Auto-Update Settings Section ──────────────────────────────────── */
	.auto-update-section {
		margin: 0 20px 20px;
		border: 1px solid var(--border);
		border-radius: 8px;
		overflow: hidden;
		background: var(--card);
	}

	.auto-update-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		width: 100%;
		padding: 12px 16px;
		background: transparent;
		border: none;
		cursor: pointer;
		color: var(--foreground);
		font-size: 0.8125rem;
		font-weight: 500;
		transition: background 120ms ease;
		min-height: 44px;
	}

	.auto-update-header:hover {
		background: color-mix(in oklch, var(--muted) 30%, transparent);
	}

	.auto-update-header:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: -2px;
	}

	.auto-update-header-left {
		display: flex;
		align-items: center;
		gap: 8px;
	}

	.auto-update-header-left :global(svg) {
		color: var(--muted-foreground);
	}

	.auto-update-chevron {
		transition: transform 200ms ease;
		color: var(--muted-foreground);
	}

	.auto-update-chevron--open {
		transform: rotate(180deg);
	}

	.auto-update-body {
		border-top: 1px solid var(--border);
		padding: 4px 0;
	}

	.auto-update-row {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 12px;
		padding: 10px 16px;
		min-height: 44px;
	}

	.auto-update-row-info {
		display: flex;
		flex-direction: column;
		gap: 1px;
		min-width: 0;
	}

	.auto-update-row-label {
		font-size: 0.8125rem;
		font-weight: 500;
		color: var(--foreground);
	}

	.auto-update-row-desc {
		font-size: 0.6875rem;
		color: var(--muted-foreground);
	}

	/* ── Toggle switch ─────────────────────────────────────────────────── */
	.auto-update-toggle {
		background: none;
		border: none;
		padding: 0;
		cursor: pointer;
		flex-shrink: 0;
		min-width: 44px;
		min-height: 44px;
		display: flex;
		align-items: center;
		justify-content: center;
	}

	.auto-update-toggle:disabled {
		opacity: 0.4;
		cursor: not-allowed;
	}

	.auto-update-toggle:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
		border-radius: 12px;
	}

	.toggle-track {
		display: block;
		width: 36px;
		height: 20px;
		border-radius: 10px;
		background: var(--muted);
		position: relative;
		transition: background 200ms ease;
	}

	.toggle-track--on {
		background: var(--primary);
	}

	.toggle-thumb {
		position: absolute;
		top: 2px;
		left: 2px;
		width: 16px;
		height: 16px;
		border-radius: 50%;
		background: var(--foreground);
		transition: transform 200ms ease;
	}

	.toggle-thumb--on {
		transform: translateX(16px);
		background: var(--primary-foreground);
	}

	/* ── Select dropdown ───────────────────────────────────────────────── */
	.auto-update-select {
		padding: 6px 28px 6px 10px;
		font-size: 0.75rem;
		border-radius: 6px;
		background: var(--input);
		border: 1px solid var(--border);
		color: var(--foreground);
		cursor: pointer;
		transition: all 150ms ease;
		appearance: none;
		background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%23999' stroke-width='2'%3E%3Cpolyline points='6 9 12 15 18 9'/%3E%3C/svg%3E");
		background-repeat: no-repeat;
		background-position: right 8px center;
		min-height: 32px;
		flex-shrink: 0;
	}

	.auto-update-select:hover:not(:disabled) {
		border-color: var(--accent);
	}

	.auto-update-select:focus {
		outline: none;
		border-color: var(--ring);
		box-shadow: 0 0 0 2px color-mix(in oklch, var(--ring) 25%, transparent);
	}

	.auto-update-select:disabled {
		opacity: 0.4;
		cursor: not-allowed;
	}

	/* ── Auto-update footer ────────────────────────────────────────────── */
	.auto-update-footer {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 12px;
		padding: 10px 16px;
		border-top: 1px solid color-mix(in oklch, var(--border) 50%, transparent);
	}

	.auto-update-last-checked {
		font-size: 0.6875rem;
		color: var(--muted-foreground);
	}

	.check-now-btn {
		display: flex;
		align-items: center;
		gap: 6px;
		padding: 6px 12px;
		font-size: 0.75rem;
		font-weight: 500;
		border-radius: 6px;
		background: var(--secondary);
		color: var(--foreground);
		border: 1px solid var(--border);
		cursor: pointer;
		transition: all 150ms ease;
		white-space: nowrap;
		min-height: 32px;
	}

	.check-now-btn:hover:not(:disabled) {
		border-color: var(--accent);
		background: color-mix(in oklch, var(--accent) 8%, transparent);
	}

	.check-now-btn:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
	}

	.check-now-btn:disabled {
		opacity: 0.6;
		cursor: not-allowed;
	}

	.auto-update-loading {
		padding: 16px;
		text-align: center;
	}

	/* ── Mobile adjustments ────────────────────────────────────────────── */
	@media (max-width: 639px) {
		.updates-toolbar {
			flex-wrap: wrap;
			gap: 8px;
			padding: 8px 12px;
		}

		.auto-update-section {
			margin: 0 12px 16px;
		}

		.auto-update-row {
			padding: 10px 12px;
		}

		.auto-update-footer {
			flex-wrap: wrap;
			gap: 8px;
			padding: 10px 12px;
		}

		.check-now-btn {
			flex: 1;
			justify-content: center;
		}
	}
</style>
