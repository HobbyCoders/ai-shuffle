<script lang="ts">
	/**
	 * PluginListItem - Individual plugin display component
	 *
	 * Displays plugin information with different modes:
	 * - available: Shows install button, selection for batch install
	 * - installed: Shows enable/disable toggle, uninstall button
	 */
	import type { AvailablePlugin, PluginInfo } from '$lib/api/plugins';
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

<!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
<div
	class="card-list-item card-system {selected ? 'ring-2 ring-primary' : ''}"
	onclick={onDetails}
>
	<!-- Checkbox for batch selection (available mode only) -->
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

	<!-- Plugin icon -->
	<div class="card-item-icon">
		<svg fill="none" stroke="currentColor" viewBox="0 0 24 24">
			<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 7v10c0 2.21 3.582 4 8 4s8-1.79 8-4V7M4 7c0 2.21 3.582 4 8 4s8-1.79 8-4M4 7c0-2.21 3.582-4 8-4s8 1.79 8 4" />
		</svg>
	</div>

	<!-- Plugin info -->
	<div class="card-item-content">
		<div class="card-item-header">
			<span class="card-item-name">{plugin.name}</span>
			<span class="card-badge card-badge--muted">{plugin.marketplace}</span>
			{#if mode === 'installed' && installedPlugin}
				<span class="card-badge {installedPlugin.enabled ? 'card-badge--success' : 'card-badge--muted'}">
					{installedPlugin.enabled ? 'Enabled' : 'Disabled'}
				</span>
			{:else if mode === 'available' && availablePlugin?.installed}
				<span class="card-badge card-badge--success">Installed</span>
			{/if}
		</div>
		<p class="card-item-description">{plugin.description || 'No description available'}</p>

		<!-- Feature badges -->
		<div class="feature-badges">
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
			{#if mode === 'installed' && installedPlugin}
				<span class="card-badge card-badge--muted">v{installedPlugin.version}</span>
			{/if}
		</div>
	</div>

	<!-- Actions -->
	<!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
	<div class="card-item-actions" onclick={(e) => e.stopPropagation()}>
		{#if mode === 'available'}
			{#if availablePlugin?.installed}
				<span class="version-info">v{availablePlugin.installed_version}</span>
			{:else}
				<button
					onclick={onInstall}
					disabled={installing}
					class="card-btn-primary card-btn-primary--small"
				>
					{#if installing}
						<span class="card-spinner card-spinner--small"></span>
					{:else}
						<svg fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" />
						</svg>
					{/if}
					Install
				</button>
			{/if}
		{:else if mode === 'installed' && installedPlugin}
			<!-- Enable/Disable toggle -->
			<button
				onclick={onToggle}
				class="toggle-switch {installedPlugin.enabled ? 'enabled' : ''}"
				title={installedPlugin.enabled ? 'Disable plugin' : 'Enable plugin'}
			>
				<span class="toggle-knob"></span>
			</button>

			<!-- Details button -->
			<button
				onclick={onDetails}
				class="card-action-btn"
				title="View details"
			>
				<svg fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
				</svg>
			</button>

			<!-- Uninstall button -->
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

	/* Feature badges row */
	.feature-badges {
		display: flex;
		flex-wrap: wrap;
		gap: 4px;
		margin-top: 6px;
	}

	/* Feature badge colors */
	.card-badge--feature-commands {
		background: color-mix(in oklch, var(--feature-commands, oklch(0.6 0.2 250)) 15%, transparent);
		color: var(--feature-commands, oklch(0.6 0.2 250));
	}

	.card-badge--feature-agents {
		background: color-mix(in oklch, var(--feature-agents, oklch(0.6 0.2 300)) 15%, transparent);
		color: var(--feature-agents, oklch(0.6 0.2 300));
	}

	.card-badge--feature-skills {
		background: color-mix(in oklch, var(--feature-skills, oklch(0.6 0.2 145)) 15%, transparent);
		color: var(--feature-skills, oklch(0.6 0.2 145));
	}

	.card-badge--feature-hooks {
		background: color-mix(in oklch, var(--feature-hooks, oklch(0.7 0.15 55)) 15%, transparent);
		color: var(--feature-hooks, oklch(0.7 0.15 55));
	}

	/* Success badge */
	.card-badge--success {
		background: color-mix(in oklch, var(--success, oklch(0.6 0.2 145)) 15%, transparent);
		color: var(--success, oklch(0.6 0.2 145));
	}

	/* Version info */
	.version-info {
		font-size: var(--text-xs, 0.6875rem);
		color: var(--muted-foreground);
		padding: 0 8px;
	}

	/* Small primary button */
	.card-btn-primary--small {
		padding: 6px 12px;
		font-size: var(--text-sm, 0.75rem);
	}

	.card-btn-primary--small svg {
		width: 14px;
		height: 14px;
	}

	/* Toggle switch */
	.toggle-switch {
		position: relative;
		width: 44px;
		height: 24px;
		border-radius: 12px;
		background: var(--muted);
		border: none;
		cursor: pointer;
		transition: background var(--transition-fast, 100ms);
	}

	.toggle-switch.enabled {
		background: var(--primary);
	}

	.toggle-knob {
		position: absolute;
		top: 4px;
		left: 4px;
		width: 16px;
		height: 16px;
		border-radius: 50%;
		background: var(--card, white);
		box-shadow: 0 1px 3px color-mix(in oklch, var(--foreground, black) 20%, transparent);
		transition: transform var(--transition-fast, 100ms);
	}

	.toggle-switch.enabled .toggle-knob {
		transform: translateX(20px);
	}
</style>
