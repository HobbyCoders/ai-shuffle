<script lang="ts">
	/**
	 * PluginListItem - Individual plugin display as an App Store-style grid card
	 *
	 * Displays plugin information with different modes:
	 * - available: Shows install button, selection for batch install
	 * - installed: Shows enable/disable toggle, uninstall button
	 */
	import type { AvailablePlugin, PluginInfo } from '$lib/api/plugins';
	import { ToggleLeft, ToggleRight } from 'lucide-svelte';
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
	class="plugin-grid-card card-system {selected ? 'plugin-grid-card--selected' : ''}"
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

	<div class="plugin-grid-card-icon">
		<svg fill="none" stroke="currentColor" viewBox="0 0 24 24">
			<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 7v10c0 2.21 3.582 4 8 4s8-1.79 8-4V7M4 7c0 2.21 3.582 4 8 4s8-1.79 8-4M4 7c0-2.21 3.582-4 8-4s8 1.79 8 4" />
		</svg>
	</div>

	<div class="plugin-grid-card-content">
		<div class="plugin-grid-card-header">
			<span class="plugin-grid-card-name">{plugin.name}</span>
			<div class="plugin-grid-card-chips">
				<span class="card-badge card-badge--muted">{plugin.marketplace}</span>
				{#if mode === 'installed' && installedPlugin}
					<span class="card-badge {installedPlugin.enabled ? 'card-badge--success' : 'card-badge--muted'}">
						{installedPlugin.enabled ? 'Enabled' : 'Disabled'}
					</span>
				{:else if mode === 'available' && availablePlugin?.installed}
					<span class="card-badge card-badge--success">Installed</span>
				{/if}
			</div>
		</div>

		<p class="plugin-grid-card-description">{plugin.description || 'No description available'}</p>

		{#if plugin.has_commands || plugin.has_agents || plugin.has_skills || plugin.has_hooks || plugin.has_mcp}
			<div class="plugin-grid-card-features">
				{#if plugin.has_commands}
					<span class="card-badge card-badge--feature-commands">Commands</span>
				{/if}
				{#if plugin.has_agents}
					<span class="card-badge card-badge--feature-agents">Agents</span>
				{/if}
				{#if plugin.has_skills}
					<span class="card-badge card-badge--feature-skills">Skills</span>
				{/if}
				{#if plugin.has_hooks}
					<span class="card-badge card-badge--feature-hooks">Hooks</span>
				{/if}
				{#if plugin.has_mcp}
					<span class="card-badge card-badge--feature-mcp">MCP</span>
				{/if}
			</div>
		{/if}
	</div>

	<!-- Actions -->
	<div class="plugin-grid-card-actions" onclick={(e) => e.stopPropagation()} onkeydown={(e) => e.stopPropagation()} role="toolbar" tabindex="-1">
		{#if mode === 'available'}
			{#if availablePlugin?.installed}
				<span class="version-info">v{availablePlugin.installed_version}</span>
			{:else}
				<button
					onclick={onInstall}
					disabled={installing}
					class="card-action-btn"
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
				class="card-action-btn"
				title={installedPlugin.enabled ? 'Disable plugin' : 'Enable plugin'}
			>
				{#if installedPlugin.enabled}
					<ToggleRight size={16} />
				{:else}
					<ToggleLeft size={16} />
				{/if}
			</button>

			<div class="plugin-grid-card-actions-right">
				<button
					onclick={onDetails}
					class="card-action-btn"
					title="View details"
				>
					<svg fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
					</svg>
				</button>

				<button
					onclick={onUninstall}
					disabled={installing}
					class="card-action-btn card-action-btn--destructive"
					title="Uninstall"
				>
					{#if installing}
						<span class="card-spinner card-spinner--small"></span>
					{:else}
						<svg fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
						</svg>
					{/if}
				</button>
			</div>
		{/if}
	</div>
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

	/* Feature badge colors */
	.card-badge--feature-commands {
		background: color-mix(in oklch, var(--feature-commands, oklch(0.6 0.2 250)) 15%, transparent);
		color: var(--feature-commands, oklch(0.6 0.2 250));
		border: 1px solid color-mix(in oklch, var(--feature-commands, oklch(0.6 0.2 250)) 30%, transparent);
	}

	.card-badge--feature-agents {
		background: color-mix(in oklch, var(--feature-agents, oklch(0.6 0.2 300)) 15%, transparent);
		color: var(--feature-agents, oklch(0.6 0.2 300));
		border: 1px solid color-mix(in oklch, var(--feature-agents, oklch(0.6 0.2 300)) 30%, transparent);
	}

	.card-badge--feature-skills {
		background: color-mix(in oklch, var(--feature-skills, oklch(0.6 0.2 145)) 15%, transparent);
		color: var(--feature-skills, oklch(0.6 0.2 145));
		border: 1px solid color-mix(in oklch, var(--feature-skills, oklch(0.6 0.2 145)) 30%, transparent);
	}

	.card-badge--feature-hooks {
		background: color-mix(in oklch, var(--feature-hooks, oklch(0.7 0.15 55)) 15%, transparent);
		color: var(--feature-hooks, oklch(0.7 0.15 55));
		border: 1px solid color-mix(in oklch, var(--feature-hooks, oklch(0.7 0.15 55)) 30%, transparent);
	}

	.card-badge--feature-mcp {
		background: color-mix(in oklch, var(--feature-mcp, oklch(0.6 0.18 30)) 15%, transparent);
		color: var(--feature-mcp, oklch(0.6 0.18 30));
		border: 1px solid color-mix(in oklch, var(--feature-mcp, oklch(0.6 0.18 30)) 30%, transparent);
	}

	/* Success badge */
	.card-badge--success {
		background: color-mix(in oklch, var(--success, oklch(0.6 0.2 145)) 15%, transparent);
		color: var(--success, oklch(0.6 0.2 145));
		border: 1px solid color-mix(in oklch, var(--success, oklch(0.6 0.2 145)) 30%, transparent);
	}

	/* Version info */
	.version-info {
		font-size: var(--text-xs, 0.6875rem);
		color: var(--muted-foreground);
		padding: 0 8px;
	}

</style>
