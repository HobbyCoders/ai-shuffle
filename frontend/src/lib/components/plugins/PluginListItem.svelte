<script lang="ts">
	/**
	 * PluginListItem - Individual plugin display as an App Store-style grid card
	 *
	 * Displays plugin information with different modes:
	 * - available: Shows install button, selection for batch install
	 * - installed: Shows enable/disable toggle, uninstall button
	 */
	import type { AvailablePlugin, PluginInfo } from '$lib/api/plugins';
	import { ToggleLeft, ToggleRight, ChevronRight } from 'lucide-svelte';
	import '$lib/components/deck/cards/card-design-system.css';

	interface Props {
		plugin: AvailablePlugin | PluginInfo;
		installing?: boolean;
		selected?: boolean;
		mode: 'available' | 'installed';
		onInstall?: () => void;
		onUninstall?: () => void;
		onToggle?: () => void;
		onSelect?: () => void;
		onDetails?: () => void;
	}

	let {
		plugin,
		installing = false,
		selected = false,
		mode,
		onInstall,
		onUninstall,
		onToggle,
		onSelect,
		onDetails
	}: Props = $props();

	// Check if this is an installed plugin
	const isInstalled = $derived('enabled' in plugin);
	const installedPlugin = $derived(isInstalled ? (plugin as PluginInfo) : null);
	const availablePlugin = $derived(!isInstalled ? (plugin as AvailablePlugin) : null);
</script>

<div
	class="fp-list-item {selected ? 'fp-list-item--selected' : ''}"
	onclick={onDetails}
	onkeydown={(e) => { if (e.key === 'Enter' || e.key === ' ') onDetails?.(); }}
	role="button"
	tabindex="0"
>
	{#if mode === 'available' && !availablePlugin?.installed}
		<button
			onclick={(e) => { e.stopPropagation(); onSelect?.(); }}
			class="batch-checkbox {selected ? 'selected' : ''}"
		>
			{#if selected}
				<svg fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="3" d="M5 13l4 4L19 7" />
				</svg>
			{/if}
		</button>
	{/if}

	<div class="fp-item-indicator"></div>

	<div class="fp-item-content">
		<div class="fp-item-header">
			<span class="fp-item-name">{plugin.name}</span>
			{#if mode === 'installed' && installedPlugin}
				<span class="fp-item-badge {installedPlugin.enabled ? 'fp-item-badge--accent' : ''}">
					{installedPlugin.enabled ? 'Enabled' : 'Disabled'}
				</span>
			{:else if mode === 'available' && availablePlugin?.installed}
				<span class="fp-item-badge fp-item-badge--accent">Installed</span>
			{/if}
		</div>

		<p class="fp-item-description">{plugin.description || 'No description available'}</p>

		<div class="fp-item-meta">
			<span class="fp-meta-model">{plugin.marketplace}</span>
			{#if plugin.has_commands}
				<span class="fp-meta-sep">&middot;</span>
				<span class="fp-meta-chip">Commands</span>
			{/if}
			{#if plugin.has_agents}
				<span class="fp-meta-sep">&middot;</span>
				<span class="fp-meta-chip">Agents</span>
			{/if}
			{#if plugin.has_skills}
				<span class="fp-meta-sep">&middot;</span>
				<span class="fp-meta-chip">Skills</span>
			{/if}
			{#if plugin.has_hooks}
				<span class="fp-meta-sep">&middot;</span>
				<span class="fp-meta-chip">Hooks</span>
			{/if}
			{#if plugin.has_mcp}
				<span class="fp-meta-sep">&middot;</span>
				<span class="fp-meta-chip">MCP</span>
			{/if}
		</div>
	</div>

	<!-- Actions -->
	<div class="fp-item-actions" onclick={(e) => e.stopPropagation()} onkeydown={(e) => e.stopPropagation()} role="toolbar" tabindex="-1">
		{#if mode === 'available'}
			{#if availablePlugin?.installed}
				<span class="version-info">v{availablePlugin.installed_version}</span>
			{:else}
				<button
					onclick={onInstall}
					disabled={installing}
					class="fp-action-btn"
					title="Install"
				>
					{#if installing}
						<span class="card-spinner card-spinner--small"></span>
					{:else}
						<svg fill="none" stroke="currentColor" viewBox="0 0 24 24" width="16" height="16">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" />
						</svg>
					{/if}
				</button>
			{/if}
		{:else if mode === 'installed' && installedPlugin}
			<button
				onclick={onToggle}
				class="fp-action-btn"
				title={installedPlugin.enabled ? 'Disable plugin' : 'Enable plugin'}
			>
				{#if installedPlugin.enabled}
					<ToggleRight size={16} />
				{:else}
					<ToggleLeft size={16} />
				{/if}
			</button>

			<button
				onclick={onUninstall}
				disabled={installing}
				class="fp-action-btn fp-action-btn--destructive"
				title="Uninstall"
			>
				{#if installing}
					<span class="card-spinner card-spinner--small"></span>
				{:else}
					<svg fill="none" stroke="currentColor" viewBox="0 0 24 24" width="16" height="16">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
					</svg>
				{/if}
			</button>
		{/if}
	</div>

	<div class="fp-item-chevron"><ChevronRight size={16} /></div>
</div>

<style>
	/* Batch selection checkbox */
	.batch-checkbox {
		width: 20px;
		height: 20px;
		flex-shrink: 0;
		border: 2px solid var(--border);
		border-radius: 4px;
		display: flex;
		align-items: center;
		justify-content: center;
		background: transparent;
		cursor: pointer;
		transition: all var(--transition-fast, 100ms);
	}

	.batch-checkbox:hover {
		border-color: color-mix(in oklch, var(--primary) 50%, transparent);
	}

	.batch-checkbox.selected {
		background: var(--primary);
		border-color: var(--primary);
	}

	.batch-checkbox svg {
		width: 12px;
		height: 12px;
		color: var(--primary-foreground);
	}

	/* Selected state for batch install */
	:global(.fp-list-item).fp-list-item--selected {
		background: color-mix(in oklch, var(--primary) 8%, transparent);
	}

	/* Version info */
	.version-info {
		font-size: var(--text-xs, 0.6875rem);
		color: var(--muted-foreground);
		padding: 0 8px;
	}

</style>
