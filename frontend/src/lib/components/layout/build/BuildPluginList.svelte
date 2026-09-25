<script lang="ts">
	import { watchResources } from '$lib/services/watch-resources';
	/**
	 * BuildPluginList — header-less Plugins surface for the Build page.
	 *
	 * Matches the Subagents/MCP row style + toolbar layout (search left, actions
	 * right). Self-contained: reads the global plugins store and calls its methods
	 * directly. Installed / Available / Sources tabs + plugin Details. Theme tokens.
	 */
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
		pluginStats,
		pluginUpdating,
		updatesAvailable,
		updatesCount,
		autoUpdateSettings,
		checkingUpdates
	} from '$lib/stores/plugins.svelte';
	import type { AvailablePlugin, PluginInfo, PluginUpdateInfo, AutoUpdateSettings } from '$lib/api/plugins';
	import PluginDetails from '$lib/components/plugins/PluginDetails.svelte';
	import RowActionMenu from '$lib/components/ui/RowActionMenu.svelte';
	import ConfirmDialog from '$lib/components/ui/ConfirmDialog.svelte';
	import {
		Package, Puzzle, Search, Plus, RefreshCw, ArrowUpCircle, ToggleLeft, ToggleRight,
		Trash2, Download, Loader2, AlertCircle, X, Database, Settings, Clock
	} from 'lucide-svelte';

	type Tab = 'installed' | 'available' | 'marketplaces' | 'details';
	let activeTab = $state<Tab>('installed');
	let previousTab = $state<Tab>('installed');
	let searchQuery = $state('');

	// Batch install selection
	let selectedForInstall = $state<Set<string>>(new Set());
	let batchInstalling = $state(false);

	// Add-source form
	let showAddSource = $state(false);
	let newSourceUrl = $state('');
	let newSourceName = $state('');
	let addingSource = $state(false);

	// Sources delete confirm
	let confirmDeleteSource = $state<string | null>(null);

	// Uninstall confirm (plugin awaiting confirmation, or null)
	let pendingUninstall = $state<PluginInfo | null>(null);
	function confirmUninstall() {
		const p = pendingUninstall;
		pendingUninstall = null;
		if (p) quiet(plugins.uninstallPlugin(p.id));
	}

	// Touch/narrow only: which row (installed or source) has its collapsed ⋮ action
	// tray expanded. One open at a time; desktop keeps the always-inline rail.
	let actionsOpenId = $state<string | null>(null);

	// RowActionMenu already stops propagation before calling this, so the row's own
	// click (open details) never also fires.
	function toggleRowActions(_e: MouseEvent | KeyboardEvent, id: string) {
		actionsOpenId = actionsOpenId === id ? null : id;
	}
	// Any click reaching the window is outside an open tray (tray buttons all stop
	// propagation) — collapse it and disarm any pending source-removal confirm.
	function onWindowClick() {
		if (actionsOpenId !== null) actionsOpenId = null;
		confirmDeleteSource = null;
	}
	function onWindowKeydown(e: KeyboardEvent) {
		if (e.key === 'Escape') {
			actionsOpenId = null;
			confirmDeleteSource = null;
		}
	}

	// Updates
	let updatingAll = $state(false);

	// Auto-update settings panel
	let showAutoUpdate = $state(false);

	const intervalOptions = [
		{ value: 1, label: '1 hour' },
		{ value: 3, label: '3 hours' },
		{ value: 6, label: '6 hours' },
		{ value: 12, label: '12 hours' },
		{ value: 24, label: '24 hours' }
	];

	// Store actions surface failures through $pluginsError and then rethrow; the
	// rethrow has no other consumer here, so don't leave it unhandled.
	function quiet(p: Promise<unknown>) { p.catch(() => {}); }

	onMount(() => {
		quiet(plugins.loadAll());
	});

	const updateLookup = $derived.by(() => {
		const map = new Map<string, PluginUpdateInfo>();
		for (const u of $updatesAvailable) map.set(u.plugin_id, u);
		return map;
	});

	const filteredInstalled = $derived.by(() => {
		const q = searchQuery.trim().toLowerCase();
		if (!q) return $installedPlugins;
		return $installedPlugins.filter(
			(p) => p.name.toLowerCase().includes(q) || p.description.toLowerCase().includes(q) || p.marketplace.toLowerCase().includes(q)
		);
	});

	const filteredAvailable = $derived.by(() => {
		const q = searchQuery.trim().toLowerCase();
		if (!q) return $uninstalledAvailablePlugins;
		return $uninstalledAvailablePlugins.filter(
			(p) => p.name.toLowerCase().includes(q) || p.description.toLowerCase().includes(q) || p.marketplace.toLowerCase().includes(q)
		);
	});

	const tabs = $derived([
		{ id: 'installed' as Tab, label: 'Installed', count: $pluginStats.totalInstalled },
		{ id: 'available' as Tab, label: 'Available', count: $pluginStats.totalAvailable },
		{ id: 'marketplaces' as Tab, label: 'Sources', count: $pluginStats.totalMarketplaces }
	]);

	function caps(p: AvailablePlugin | PluginInfo): string[] {
		const out: string[] = [];
		if (p.has_commands) out.push('commands');
		if (p.has_agents) out.push('agents');
		if (p.has_skills) out.push('skills');
		if (p.has_hooks) out.push('hooks');
		if (p.has_mcp) out.push('mcp');
		return out;
	}

	function selectTab(id: Tab) {
		if (id === activeTab) return;
		activeTab = id;
		actionsOpenId = null;
		confirmDeleteSource = null;
		if (id !== 'details') plugins.clearSelectedPlugin();
	}

	async function openDetails(pluginId: string) {
		previousTab = activeTab;
		try {
			await plugins.selectPlugin(pluginId);
		} catch {
			return; // shown via $pluginsError
		}
		activeTab = 'details';
	}

	function toggleSelect(pluginId: string) {
		const next = new Set(selectedForInstall);
		if (next.has(pluginId)) next.delete(pluginId);
		else next.add(pluginId);
		selectedForInstall = next;
	}

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

	async function handleCheckUpdates() {
		try { await plugins.checkForUpdates(); } catch (e) { console.error(e); }
	}

	async function handleUpdateAll() {
		updatingAll = true;
		try { await plugins.updateAllPlugins(); } catch (e) { console.error(e); } finally { updatingAll = false; }
	}

	async function handleAddSource() {
		if (!newSourceUrl.trim() || !newSourceName.trim()) return;
		addingSource = true;
		try {
			await plugins.addMarketplace(newSourceUrl.trim(), newSourceName.trim());
			showAddSource = false;
			newSourceUrl = '';
			newSourceName = '';
			await plugins.loadAvailable();
		} catch (e) {
			console.error('Failed to add source:', e);
		} finally {
			addingSource = false;
		}
	}

	function handleRemoveSource(id: string) {
		if (confirmDeleteSource === id) {
			quiet(plugins.removeMarketplace(id));
			confirmDeleteSource = null;
		} else {
			confirmDeleteSource = id;
		}
	}

	async function handleAutoUpdateToggle(field: keyof AutoUpdateSettings, value: boolean | number) {
		try { await plugins.updateAutoUpdateSettings({ [field]: value }); } catch (e) { console.error(e); }
	}

	function formatDate(dateStr: string | null): string {
		if (!dateStr) return 'Never synced';
		try {
			return new Date(dateStr).toLocaleDateString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
		} catch { return 'Unknown'; }
	}
	function formatLastChecked(dateStr: string | null): string {
		if (!dateStr) return 'Never';
		try {
			const diffMins = Math.floor((Date.now() - new Date(dateStr).getTime()) / 60000);
			if (diffMins < 1) return 'Just now';
			if (diffMins < 60) return `${diffMins}m ago`;
			const h = Math.floor(diffMins / 60);
			if (h < 24) return `${h}h ago`;
			return `${Math.floor(h / 24)}d ago`;
		} catch { return 'Unknown'; }
	}
	watchResources(['plugins'], async () => {
		const selected = $selectedPlugin;
		await plugins.loadAll();
		if (selected && activeTab === 'details' && $selectedPlugin === selected) {
			const exists = [...plugins.current.installed, ...plugins.current.available].some((plugin) => plugin.id === selected.id);
			if (exists) await plugins.selectPlugin(selected.id);
			else { plugins.clearSelectedPlugin(); activeTab = previousTab; }
		}
	});
</script>

<svelte:window onclick={onWindowClick} onkeydown={onWindowKeydown} />

<div class="bpl">
	<!-- Tabs (underline sub-nav, left-aligned) -->
	<div class="bpl-tabnav">
		<div class="bpl-tabs">
			{#each tabs as tab}
				<button class="bpl-tab" class:on={activeTab === tab.id} onclick={() => selectTab(tab.id)}>
					{tab.label}<span class="bpl-tab-count">{tab.count}</span>
					{#if tab.id === 'installed' && $updatesCount > 0}<span class="bpl-tab-badge">{$updatesCount}</span>{/if}
				</button>
			{/each}
			{#if $selectedPlugin}
				<button class="bpl-tab" class:on={activeTab === 'details'} onclick={() => (activeTab = 'details')}>Details</button>
			{/if}
		</div>
	</div>

	{#if activeTab === 'details' && $selectedPlugin}
		<PluginDetails
			plugin={$selectedPlugin}
			onClose={() => { plugins.clearSelectedPlugin(); activeTab = previousTab; }}
			onInstall={() => quiet(plugins.installPlugin($selectedPlugin.id))}
			installing={$pluginInstalling[$selectedPlugin.id] || false}
		/>
	{:else}
		<!-- Toolbar: search left, actions right -->
		<div class="bpl-toolbar">
			{#if activeTab === 'installed' || activeTab === 'available'}
				<div class="bpl-search">
					<Search size={14} strokeWidth={1.7} />
					<input type="text" placeholder="Search plugins…" autocomplete="off" bind:value={searchQuery} />
				</div>
			{:else}
				<span class="bpl-spacer"></span>
			{/if}
			<div class="bpl-tools">
				{#if activeTab === 'installed'}
					<button class="bpl-btn" onclick={handleCheckUpdates} disabled={$checkingUpdates}>
						<RefreshCw size={14} strokeWidth={1.7} class={$checkingUpdates ? 'bpl-spin' : ''} />
						<span>{$checkingUpdates ? 'Checking…' : 'Check for updates'}</span>
					</button>
					{#if $updatesCount > 0}
						<button class="bpl-btn bpl-btn--accent" onclick={handleUpdateAll} disabled={updatingAll}>
							{#if updatingAll}<Loader2 size={14} class="bpl-spin" />{:else}<ArrowUpCircle size={14} strokeWidth={1.7} />{/if}
							<span>Update all ({$updatesCount})</span>
						</button>
					{/if}
				{:else if activeTab === 'available'}
					{#if selectedForInstall.size > 0}
						<button class="bpl-btn bpl-btn--primary" onclick={handleBatchInstall} disabled={batchInstalling}>
							{#if batchInstalling}<Loader2 size={14} class="bpl-spin" />{/if}
							<span>Install ({selectedForInstall.size})</span>
						</button>
					{/if}
				{/if}
			</div>
		</div>

		{#if $pluginsError}
			<div class="bpl-error">
				<AlertCircle size={15} strokeWidth={1.7} />
				<span>{$pluginsError}</span>
				<button class="bpl-error-close" onclick={() => plugins.clearError()} aria-label="Dismiss"><X size={14} strokeWidth={2} /></button>
			</div>
		{/if}

		{#if $pluginsLoading}
			<div class="bpl-loading"><Loader2 size={28} class="bpl-spin" /><p>Loading plugins…</p></div>

		<!-- ─── INSTALLED ─── -->
		{:else if activeTab === 'installed'}
			{#if filteredInstalled.length === 0}
				<div class="bpl-empty">
					<Puzzle size={34} strokeWidth={1.25} />
					<p class="bpl-empty-title">{searchQuery.trim() ? 'No plugins match your search' : 'No plugins installed'}</p>
					{#if !searchQuery.trim()}<button class="bpl-link" onclick={() => selectTab('available')}>Browse available plugins</button>{/if}
				</div>
			{:else}
				<div class="bpl-list">
					{#each filteredInstalled as p (p.id)}
						<div class="bpl-row" role="button" tabindex="0" onclick={() => openDetails(p.id)} onkeydown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openDetails(p.id); } }}>
							<span class="bpl-ic"><Package size={17} strokeWidth={1.7} /></span>
							<div class="bpl-body">
								<h4 class="bpl-name">
									{p.name}
									<span class="bpl-badge" class:on={p.enabled}>{p.enabled ? 'Enabled' : 'Disabled'}</span>
									{#if updateLookup.has(p.id)}<span class="bpl-badge bpl-badge--update">Update</span>{/if}
								</h4>
								<p class="bpl-desc">{p.description || 'No description available'}</p>
							</div>
							<div class="bpl-meta">
								<span class="bpl-tag">{p.marketplace}</span>
								{#each caps(p) as c}<span class="bpl-tag">{c}</span>{/each}
							</div>
							<div class="bpl-actions" role="toolbar" tabindex="-1" onclick={(e) => e.stopPropagation()} onkeydown={(e) => e.stopPropagation()}>
								<RowActionMenu
									open={actionsOpenId === p.id}
									onToggle={(e) => toggleRowActions(e, p.id)}
									label="Plugin actions"
								>
									{#if updateLookup.has(p.id)}
										<button class="bpl-act bpl-act--update" title="Update" aria-label="Update {p.name}" onclick={() => { actionsOpenId = null; quiet(plugins.updatePlugin(p.id)); }} disabled={$pluginUpdating[p.id]}>
											{#if $pluginUpdating[p.id]}<Loader2 size={15} class="bpl-spin" />{:else}<ArrowUpCircle size={15} strokeWidth={1.7} />{/if}
										</button>
									{/if}
									<button class="bpl-act bpl-act--danger" title="Uninstall" aria-label="Uninstall {p.name}" onclick={() => { actionsOpenId = null; pendingUninstall = p; }}>
										<Trash2 size={15} strokeWidth={1.7} />
									</button>
								</RowActionMenu>
								<!-- Enable/disable stays outside the menu — an always-visible control. -->
								<button class="bpl-act bpl-toggle-act" title={p.enabled ? 'Disable' : 'Enable'} aria-label="{p.enabled ? 'Disable' : 'Enable'} {p.name}" onclick={() => quiet(plugins.togglePlugin(p.id))}>
									{#if p.enabled}<ToggleRight size={16} strokeWidth={1.7} />{:else}<ToggleLeft size={16} strokeWidth={1.7} />{/if}
								</button>
							</div>
						</div>
					{/each}
				</div>
			{/if}

		<!-- ─── AVAILABLE ─── -->
		{:else if activeTab === 'available'}
			{#if filteredAvailable.length === 0}
				<div class="bpl-empty">
					<Puzzle size={34} strokeWidth={1.25} />
					<p class="bpl-empty-title">No plugins found</p>
					{#if searchQuery.trim()}<button class="bpl-link" onclick={() => (searchQuery = '')}>Clear search</button>{/if}
				</div>
			{:else}
				<div class="bpl-list">
					{#each filteredAvailable as p (p.id)}
						<div class="bpl-row" class:bpl-row--sel={selectedForInstall.has(p.id)} role="button" tabindex="0" onclick={() => openDetails(p.id)} onkeydown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openDetails(p.id); } }}>
							<button class="bpl-check" class:on={selectedForInstall.has(p.id)} title="Select for batch install" aria-label="Select {p.name} for batch install" aria-pressed={selectedForInstall.has(p.id)} onclick={(e) => { e.stopPropagation(); toggleSelect(p.id); }}>
								{#if selectedForInstall.has(p.id)}<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><path d="M5 13l4 4L19 7" /></svg>{/if}
							</button>
							<span class="bpl-ic"><Package size={17} strokeWidth={1.7} /></span>
							<div class="bpl-body">
								<h4 class="bpl-name">{p.name}</h4>
								<p class="bpl-desc">{p.description || 'No description available'}</p>
							</div>
							<div class="bpl-meta">
								<span class="bpl-tag">{p.marketplace}</span>
								{#each caps(p) as c}<span class="bpl-tag">{c}</span>{/each}
							</div>
							<div class="bpl-actions bpl-actions--hover" role="toolbar" tabindex="-1" onclick={(e) => e.stopPropagation()} onkeydown={(e) => e.stopPropagation()}>
								<button class="bpl-act" title="Install" aria-label="Install {p.name}" onclick={() => quiet(plugins.installPlugin(p.id))} disabled={$pluginInstalling[p.id]}>
									{#if $pluginInstalling[p.id]}<Loader2 size={15} class="bpl-spin" />{:else}<Download size={15} strokeWidth={1.7} />{/if}
								</button>
							</div>
						</div>
					{/each}
				</div>
			{/if}

		<!-- ─── SOURCES ─── -->
		{:else if activeTab === 'marketplaces'}
			<div class="bpl-list">
				{#each $marketplaces as m (m.id)}
					<div class="bpl-row bpl-row--static">
						<span class="bpl-ic"><Database size={17} strokeWidth={1.7} /></span>
						<div class="bpl-body">
							<h4 class="bpl-name">{m.id}<span class="bpl-tag">{m.source}</span></h4>
							<p class="bpl-desc">{m.url}</p>
							<p class="bpl-subline"><Clock size={11} strokeWidth={1.7} /> Last synced: {formatDate(m.last_updated)}</p>
						</div>
						<div class="bpl-actions bpl-actions--static" role="toolbar" tabindex="-1" onclick={(e) => e.stopPropagation()} onkeydown={(e) => e.stopPropagation()}>
							<RowActionMenu
								open={actionsOpenId === m.id}
								onToggle={(e) => toggleRowActions(e, m.id)}
								label="Source actions"
							>
								<button class="bpl-act" title="Sync from git" aria-label="Sync {m.id} from git" onclick={() => { actionsOpenId = null; quiet(plugins.syncMarketplace(m.id)); }} disabled={$marketplaceSyncing[m.id]}>
									<RefreshCw size={15} strokeWidth={1.7} class={$marketplaceSyncing[m.id] ? 'bpl-spin' : ''} />
								</button>
								{#if confirmDeleteSource === m.id}
									<button class="bpl-btn bpl-btn--danger bpl-btn--sm" onclick={() => handleRemoveSource(m.id)}>Remove</button>
									<button class="bpl-btn bpl-btn--sm" onclick={() => (confirmDeleteSource = null)}>Cancel</button>
								{:else}
									<button class="bpl-act bpl-act--danger" title="Remove source" aria-label="Remove source {m.id}" onclick={() => handleRemoveSource(m.id)}>
										<Trash2 size={15} strokeWidth={1.7} />
									</button>
								{/if}
							</RowActionMenu>
						</div>
					</div>
				{/each}

				<button class="bpl-new" onclick={() => (showAddSource = true)}>
					<span class="bpl-new-plus"><Plus size={16} strokeWidth={2.2} /></span>
					<span>Add source</span>
				</button>
			</div>

			<!-- Auto-update settings -->
			<div class="bpl-autoupdate">
				<button class="bpl-au-header" onclick={() => (showAutoUpdate = !showAutoUpdate)}>
					<span class="bpl-au-title"><Settings size={15} strokeWidth={1.7} /> Auto-update settings</span>
					<svg class="bpl-au-chevron" class:open={showAutoUpdate} width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="6 9 12 15 18 9" /></svg>
				</button>
				{#if showAutoUpdate && $autoUpdateSettings}
					<div class="bpl-au-body">
						<div class="bpl-au-row">
							<div><span class="bpl-au-label">Auto-updates</span><span class="bpl-au-desc">Automatically check and install updates</span></div>
							<button class="bpl-toggle" class:on={$autoUpdateSettings.enabled} onclick={() => handleAutoUpdateToggle('enabled', !$autoUpdateSettings?.enabled)} aria-label="Toggle auto-updates"><span class="bpl-toggle-thumb"></span></button>
						</div>
						<div class="bpl-au-row">
							<div><span class="bpl-au-label">Check interval</span><span class="bpl-au-desc">How often to check for updates</span></div>
							<select class="bpl-au-select" value={$autoUpdateSettings.interval_hours} disabled={!$autoUpdateSettings.enabled} onchange={(e) => handleAutoUpdateToggle('interval_hours', Number((e.target as HTMLSelectElement).value))}>
								{#each intervalOptions as opt}<option value={opt.value}>{opt.label}</option>{/each}
							</select>
						</div>
						<div class="bpl-au-row">
							<div><span class="bpl-au-label">Auto-install updates</span><span class="bpl-au-desc">Install updates automatically when found</span></div>
							<button class="bpl-toggle" class:on={$autoUpdateSettings.auto_update_plugins} disabled={!$autoUpdateSettings.enabled} onclick={() => handleAutoUpdateToggle('auto_update_plugins', !$autoUpdateSettings?.auto_update_plugins)} aria-label="Toggle auto-install"><span class="bpl-toggle-thumb"></span></button>
						</div>
						<div class="bpl-au-footer">
							<span>Last checked: {formatLastChecked($autoUpdateSettings.last_check)}</span>
							<button class="bpl-btn bpl-btn--sm" onclick={() => quiet(plugins.runAutoUpdate())} disabled={$checkingUpdates}>
								<RefreshCw size={13} strokeWidth={1.7} class={$checkingUpdates ? 'bpl-spin' : ''} /> Check now
							</button>
						</div>
					</div>
				{/if}
			</div>
		{/if}
	{/if}
</div>

<!-- Add-source modal -->
{#if showAddSource}
	<div class="bpl-modal-overlay" role="dialog" aria-modal="true" aria-labelledby="bpl-add-source-title">
		<button class="bpl-modal-scrim" onclick={() => (showAddSource = false)} tabindex="-1" aria-label="Close"></button>
		<div class="bpl-modal">
			<h3 id="bpl-add-source-title">Add plugin source</h3>
			<div class="bpl-field">
				<label for="bpl-src-name">Name</label>
				<input id="bpl-src-name" type="text" bind:value={newSourceName} placeholder="e.g. my-plugins" />
			</div>
			<div class="bpl-field">
				<label for="bpl-src-url">Git URL</label>
				<input id="bpl-src-url" type="text" bind:value={newSourceUrl} placeholder="https://github.com/user/plugins.git" />
			</div>
			<div class="bpl-modal-actions">
				<button class="bpl-btn" onclick={() => (showAddSource = false)}>Cancel</button>
				<button class="bpl-btn bpl-btn--primary" onclick={handleAddSource} disabled={!newSourceUrl.trim() || !newSourceName.trim() || addingSource}>
					{#if addingSource}<Loader2 size={14} class="bpl-spin" />{/if}Add source
				</button>
			</div>
		</div>
	</div>
{/if}

<ConfirmDialog
	open={pendingUninstall !== null}
	title="Uninstall plugin"
	message={`Uninstall "${pendingUninstall?.name ?? ''}"? Its commands, agents, skills and hooks will no longer be available.`}
	confirmLabel="Uninstall"
	danger
	onConfirm={confirmUninstall}
	onCancel={() => (pendingUninstall = null)}
/>

<style>
	.bpl { display: flex; flex-direction: column; gap: 14px; }

	/* Tabs (left-aligned underline sub-nav — matches the Build page tabs) */
	.bpl-tabnav { display: flex; border-bottom: 1px solid var(--border); }
	.bpl-tabs { display: flex; gap: 2px; flex-wrap: wrap; }
	.bpl-tab {
		position: relative; display: inline-flex; align-items: center; gap: 7px; padding: 10px 14px;
		border: none; background: transparent;
		color: var(--muted-foreground); font-size: 0.85rem; font-weight: 500; cursor: pointer;
		white-space: nowrap; transition: color 120ms ease;
	}
	.bpl-tab:hover { color: var(--foreground); }
	.bpl-tab.on { color: var(--foreground); }
	.bpl-tab.on::after {
		content: ''; position: absolute; left: 12px; right: 12px; bottom: -1px;
		height: 2px; border-radius: 2px; background: var(--primary);
	}
	.bpl-tab:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; border-radius: 6px; }
	.bpl-tab-count { font-family: var(--font-mono, monospace); font-size: 0.6875rem; color: var(--muted-foreground); }
	.bpl-tab.on .bpl-tab-count { color: var(--foreground); }
	.bpl-tab-badge { display: inline-grid; place-items: center; min-width: 16px; height: 16px; padding: 0 4px; border-radius: 8px; background: var(--warning, #f59e0b); color: #1a1a1a; font-size: 0.625rem; font-weight: 700; }

	/* Toolbar */
	.bpl-toolbar { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
	.bpl-spacer { flex: 1; }
	.bpl-search { position: relative; display: flex; align-items: center; flex: 1; max-width: 320px; }
	.bpl-search :global(svg) { position: absolute; left: 11px; color: var(--muted-foreground); pointer-events: none; }
	.bpl-search input {
		width: 100%; height: 34px; padding: 0 12px 0 32px;
		background: var(--input-bg); border: 1px solid var(--border);
		border-radius: calc(var(--radius, 0.75rem) - 2px);
		color: var(--foreground); font-size: 0.8125rem; outline: none;
		transition: border-color 120ms ease, box-shadow 120ms ease;
	}
	.bpl-search input::placeholder { color: var(--muted-foreground); }
	.bpl-search input:focus { border-color: var(--ring); box-shadow: 0 0 0 2px color-mix(in oklch, var(--ring) 15%, transparent); }
	.bpl-tools { display: flex; align-items: center; gap: 8px; }

	.bpl-btn {
		display: inline-flex; align-items: center; gap: 6px; height: 34px; padding: 0 13px;
		background: var(--card); color: var(--foreground);
		border: 1px solid var(--border); border-radius: calc(var(--radius, 0.75rem) - 2px);
		font-size: 0.8125rem; font-weight: 500; cursor: pointer; white-space: nowrap;
		transition: background-color 120ms ease, border-color 120ms ease, filter 120ms ease;
	}
	.bpl-btn:hover { background: var(--hover-overlay); }
	.bpl-btn:disabled { opacity: 0.55; cursor: not-allowed; }
	.bpl-btn--sm { height: 30px; padding: 0 11px; font-size: 0.78rem; }
	.bpl-btn--primary { background: var(--primary); color: var(--primary-foreground); border-color: transparent; }
	.bpl-btn--primary:hover { filter: brightness(1.08); background: var(--primary); }
	.bpl-btn--accent { background: var(--warning, #f59e0b); color: #1a1a1a; border-color: transparent; }
	.bpl-btn--accent:hover { filter: brightness(1.06); }
	.bpl-btn--danger { color: var(--destructive); border-color: color-mix(in oklch, var(--destructive) 35%, var(--border)); }
	.bpl-btn--danger:hover { background: color-mix(in oklch, var(--destructive) 12%, transparent); }

	/* Error */
	.bpl-error { display: flex; align-items: center; gap: 8px; padding: 10px 12px; border-radius: calc(var(--radius, 0.75rem) - 2px); background: color-mix(in oklch, var(--destructive) 12%, transparent); color: var(--destructive); font-size: 0.8125rem; }
	.bpl-error-close { margin-left: auto; background: transparent; border: none; color: var(--destructive); cursor: pointer; padding: 2px; border-radius: 4px; display: grid; place-items: center; }

	/* List + rows */
	.bpl-list { display: flex; flex-direction: column; border: 1px solid var(--border); border-radius: var(--radius, 0.75rem); overflow: hidden; background: var(--card); }
	.bpl-row { display: flex; align-items: center; gap: 14px; padding: 13px 16px; min-height: 60px; border-bottom: 1px solid var(--border-subtle, var(--border)); cursor: pointer; transition: background-color 120ms ease; }
	.bpl-row:last-child { border-bottom: none; }
	.bpl-row:hover { background: var(--hover-overlay); }
	.bpl-row:focus-visible { outline: 2px solid var(--ring); outline-offset: -2px; }
	.bpl-row--static { cursor: default; }
	.bpl-row--static:hover { background: transparent; }
	.bpl-row--sel { background: color-mix(in oklch, var(--primary) 8%, transparent); }

	.bpl-check { width: 20px; height: 20px; flex-shrink: 0; border: 2px solid var(--border); border-radius: 5px; background: transparent; cursor: pointer; display: grid; place-items: center; transition: all 100ms ease; }
	.bpl-check:hover { border-color: color-mix(in oklch, var(--primary) 50%, var(--border)); }
	.bpl-check.on { background: var(--primary); border-color: var(--primary); }
	.bpl-check :global(svg) { width: 12px; height: 12px; color: var(--primary-foreground); }

	.bpl-ic { display: grid; place-items: center; width: 36px; height: 36px; flex-shrink: 0; border-radius: calc(var(--radius, 0.75rem) - 4px); background: color-mix(in oklch, var(--primary) 12%, transparent); color: var(--primary); }
	.bpl-body { flex: 1; min-width: 0; }
	.bpl-name { display: flex; align-items: center; gap: 8px; margin: 0 0 3px; font-family: var(--font-display, Georgia, serif); font-size: 0.9rem; font-weight: 700; letter-spacing: -0.01em; color: var(--foreground); overflow: hidden; }
	.bpl-badge { flex-shrink: 0; font-size: 0.6rem; font-weight: 600; letter-spacing: 0.03em; color: var(--muted-foreground); background: color-mix(in oklch, var(--muted) 50%, transparent); padding: 2px 7px; border-radius: 999px; }
	.bpl-badge.on { color: var(--primary); background: color-mix(in oklch, var(--primary) 14%, transparent); }
	.bpl-badge--update { color: var(--warning, #f59e0b); background: color-mix(in oklch, var(--warning, #f59e0b) 16%, transparent); }
	.bpl-desc { margin: 0; font-size: 0.78rem; color: var(--muted-foreground); line-height: 1.4; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.bpl-subline { display: flex; align-items: center; gap: 5px; margin: 4px 0 0; font-size: 0.6875rem; color: var(--muted-foreground); }

	.bpl-meta { display: flex; align-items: center; gap: 6px; flex-shrink: 0; flex-wrap: wrap; justify-content: flex-end; max-width: 45%; }
	.bpl-tag { font-size: 0.6875rem; font-weight: 500; color: var(--muted-foreground); background: color-mix(in oklch, var(--muted) 45%, transparent); padding: 2px 8px; border-radius: 6px; white-space: nowrap; }

	.bpl-actions { display: flex; align-items: center; gap: 2px; flex-shrink: 0; }
	/* Installed / Sources rows: RowActionMenu owns reveal (via --ram-reveal) + the
	   touch/narrow ⋮ collapse. Static (source) rows keep their rail always visible on
	   desktop; the enable/disable toggle on installed rows lives outside the menu. */
	.bpl-row { --ram-reveal: 0; }
	.bpl-row:hover, .bpl-row:focus-within { --ram-reveal: 1; }
	.bpl-row--static { --ram-reveal: 1; }
	/* Available rows: a single Install button, no menu — simple hover-reveal that
	   stays visible on touch and on the mobile shell, where there is no hover to
	   reveal it with. */
	.bpl-actions--hover { opacity: 0; transition: opacity 120ms ease; }
	.bpl-row:hover .bpl-actions--hover, .bpl-row:focus-within .bpl-actions--hover { opacity: 1; }
	@media (hover: none) {
		.bpl-actions--hover { opacity: 1; }
	}
	.bpl-act { display: grid; place-items: center; width: 28px; height: 28px; background: transparent; border: 1px solid transparent; border-radius: 7px; color: var(--muted-foreground); cursor: pointer; transition: color 120ms ease, background-color 120ms ease; }
	.bpl-act:hover { color: var(--foreground); background: var(--hover-overlay); }
	.bpl-act:disabled { opacity: 0.5; cursor: not-allowed; }
	.bpl-act--danger:hover { color: var(--destructive); background: color-mix(in oklch, var(--destructive) 12%, transparent); }
	.bpl-act--update { color: var(--warning, #f59e0b); }
	.bpl-act--update:hover { background: color-mix(in oklch, var(--warning, #f59e0b) 14%, transparent); }

	/* New row (matches the Subagents/MCP dashed add row) */
	.bpl-new {
		display: flex; align-items: center; justify-content: center; gap: 8px;
		padding: 13px 16px; min-height: 52px;
		background: transparent; border: none; border-top: 1px dashed color-mix(in oklch, var(--muted-foreground) 35%, transparent);
		color: var(--muted-foreground); font-size: 0.8rem; font-weight: 600; cursor: pointer;
		transition: color 120ms ease, background-color 120ms ease;
	}
	.bpl-new:hover { color: var(--foreground); background: var(--hover-overlay); }
	.bpl-new-plus { display: grid; place-items: center; width: 24px; height: 24px; border-radius: 50%; background: var(--primary); color: var(--primary-foreground); }

	/* Empty + loading */
	.bpl-empty { display: flex; flex-direction: column; align-items: center; gap: 8px; padding: 48px 24px; text-align: center; color: var(--muted-foreground); }
	.bpl-empty-title { margin: 4px 0 0; font-size: 0.875rem; color: var(--foreground); }
	.bpl-link { background: none; border: none; color: var(--primary); cursor: pointer; font-size: 0.8125rem; }
	.bpl-link:hover { text-decoration: underline; }
	.bpl-loading { display: flex; flex-direction: column; align-items: center; gap: 10px; padding: 48px; color: var(--muted-foreground); }
	.bpl-loading :global(svg) { color: var(--primary); }

	/* Auto-update settings */
	.bpl-autoupdate { border: 1px solid var(--border); border-radius: var(--radius, 0.75rem); overflow: hidden; background: var(--card); }
	.bpl-au-header { display: flex; align-items: center; justify-content: space-between; width: 100%; padding: 12px 16px; background: transparent; border: none; cursor: pointer; color: var(--foreground); font-size: 0.8125rem; font-weight: 500; }
	.bpl-au-header:hover { background: var(--hover-overlay); }
	.bpl-au-title { display: flex; align-items: center; gap: 8px; }
	.bpl-au-title :global(svg) { color: var(--muted-foreground); }
	.bpl-au-chevron { color: var(--muted-foreground); transition: transform 200ms ease; }
	.bpl-au-chevron.open { transform: rotate(180deg); }
	.bpl-au-body { border-top: 1px solid var(--border); }
	.bpl-au-row { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 10px 16px; }
	.bpl-au-row > div { display: flex; flex-direction: column; gap: 1px; min-width: 0; }
	.bpl-au-label { font-size: 0.8125rem; font-weight: 500; color: var(--foreground); }
	.bpl-au-desc { font-size: 0.6875rem; color: var(--muted-foreground); }
	.bpl-au-footer { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 10px 16px; border-top: 1px solid color-mix(in oklch, var(--border) 50%, transparent); font-size: 0.6875rem; color: var(--muted-foreground); }
	.bpl-toggle { position: relative; width: 36px; height: 20px; border-radius: 10px; border: none; background: var(--muted); cursor: pointer; flex-shrink: 0; transition: background 200ms ease; }
	.bpl-toggle.on { background: var(--primary); }
	.bpl-toggle:disabled { opacity: 0.4; cursor: not-allowed; }
	.bpl-toggle-thumb { position: absolute; top: 2px; left: 2px; width: 16px; height: 16px; border-radius: 50%; background: var(--foreground); transition: transform 200ms ease; }
	.bpl-toggle.on .bpl-toggle-thumb { transform: translateX(16px); background: var(--primary-foreground); }
	.bpl-au-select { padding: 6px 10px; font-size: 0.78rem; border-radius: 6px; background: var(--input-bg); border: 1px solid var(--border); color: var(--foreground); cursor: pointer; }
	.bpl-au-select:disabled { opacity: 0.4; cursor: not-allowed; }

	/* Modal */
	.bpl-modal-overlay { position: fixed; inset: 0; z-index: 100; display: flex; align-items: center; justify-content: center; padding: 16px; }
	.bpl-modal-scrim { position: absolute; inset: 0; width: 100%; height: 100%; background: color-mix(in oklch, var(--background) 75%, transparent); backdrop-filter: blur(6px); border: none; cursor: default; }
	.bpl-modal { position: relative; z-index: 1; width: 100%; max-width: 400px; padding: 22px; background: var(--card); border: 1px solid var(--border); border-radius: 14px; box-shadow: var(--shadow-l, 0 20px 50px -12px rgba(0,0,0,0.5)); }
	.bpl-modal h3 { margin: 0 0 18px; font-size: 1.05rem; font-weight: 700; color: var(--foreground); }
	.bpl-field { margin-bottom: 14px; }
	.bpl-field label { display: block; font-size: 0.78rem; font-weight: 500; color: var(--muted-foreground); margin-bottom: 6px; }
	.bpl-field input { width: 100%; padding: 9px 12px; border-radius: 8px; background: var(--input-bg); border: 1px solid var(--border); color: var(--foreground); font-size: 0.85rem; outline: none; }
	.bpl-field input:focus { border-color: var(--ring); box-shadow: 0 0 0 2px color-mix(in oklch, var(--ring) 18%, transparent); }
	.bpl-modal-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 20px; }

	:global(.bpl-spin) { animation: bpl-spin 1s linear infinite; }
	@keyframes bpl-spin { to { transform: rotate(360deg); } }

	@media (max-width: 639px) {
		.bpl-search { max-width: none; }
		.bpl-meta { display: none; }
	}
</style>
