<script lang="ts">
	/**
	 * PluginListItem - Individual plugin display component
	 *
	 * Displays plugin information with different modes:
	 * - available: Shows install button, selection for batch install
	 * - installed: Shows enable/disable toggle, uninstall button
	 */
	import type { AvailablePlugin, PluginInfo } from '$lib/api/plugins';

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
	const installedPlugin = $derived(isInstalled ? plugin as PluginInfo : null);
	const availablePlugin = $derived(!isInstalled ? plugin as AvailablePlugin : null);

	// Badge colors for plugin features
	const featureBadges = [
		{ key: 'has_commands', label: 'Commands', color: 'bg-blue-500/10 text-blue-500' },
		{ key: 'has_agents', label: 'Agents', color: 'bg-purple-500/10 text-purple-500' },
		{ key: 'has_skills', label: 'Skills', color: 'bg-green-500/10 text-green-500' },
		{ key: 'has_hooks', label: 'Hooks', color: 'bg-orange-500/10 text-orange-500' }
	] as const;
</script>

<div
	class="border border-border rounded-lg overflow-hidden bg-card hover:border-primary/30 transition-colors {selected ? 'ring-2 ring-primary' : ''}"
>
	<div class="px-4 py-3">
		<div class="flex items-start gap-3">
			<!-- Checkbox for batch selection (available mode only) -->
			{#if mode === 'available' && !availablePlugin?.installed}
				<button
					onclick={onSelect}
					class="mt-0.5 w-5 h-5 rounded border-2 flex items-center justify-center transition-colors {selected ? 'bg-primary border-primary' : 'border-border hover:border-primary/50'}"
				>
					{#if selected}
						<svg class="w-3 h-3 text-primary-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="3" d="M5 13l4 4L19 7" />
						</svg>
					{/if}
				</button>
			{/if}

			<!-- Plugin icon -->
			<div class="w-10 h-10 rounded-lg bg-primary/10 flex items-center justify-center flex-shrink-0">
				<svg class="w-5 h-5 text-primary" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 7v10c0 2.21 3.582 4 8 4s8-1.79 8-4V7M4 7c0 2.21 3.582 4 8 4s8-1.79 8-4M4 7c0-2.21 3.582-4 8-4s8 1.79 8 4" />
				</svg>
			</div>

			<!-- Plugin info -->
			<div class="flex-1 min-w-0">
				<div class="flex items-center gap-2 flex-wrap">
					<button
						onclick={onDetails}
						class="font-medium text-foreground hover:text-primary transition-colors text-left"
					>
						{plugin.name}
					</button>

					<!-- Marketplace badge -->
					<span class="px-1.5 py-0.5 text-xs bg-muted text-muted-foreground rounded">
						{plugin.marketplace}
					</span>

					<!-- Status badges -->
					{#if mode === 'installed' && installedPlugin}
						<span class="px-1.5 py-0.5 text-xs rounded {installedPlugin.enabled ? 'bg-green-500/10 text-green-500' : 'bg-muted text-muted-foreground'}">
							{installedPlugin.enabled ? 'Enabled' : 'Disabled'}
						</span>
					{:else if mode === 'available' && availablePlugin?.installed}
						<span class="px-1.5 py-0.5 text-xs bg-green-500/10 text-green-500 rounded">
							Installed
						</span>
					{/if}
				</div>

				<p class="text-sm text-muted-foreground mt-0.5 line-clamp-2">
					{plugin.description || 'No description available'}
				</p>

				<!-- Feature badges -->
				<div class="flex flex-wrap gap-1 mt-2">
					{#each featureBadges as badge}
						{#if plugin[badge.key]}
							<span class="px-1.5 py-0.5 text-xs rounded {badge.color}">
								{badge.label}
							</span>
						{/if}
					{/each}

					{#if mode === 'installed' && installedPlugin}
						<span class="px-1.5 py-0.5 text-xs bg-muted text-muted-foreground rounded">
							v{installedPlugin.version}
						</span>
					{/if}
				</div>
			</div>

			<!-- Actions -->
			<div class="flex items-center gap-1 flex-shrink-0">
				{#if mode === 'available'}
					{#if availablePlugin?.installed}
						<!-- Already installed - show version info -->
						<span class="text-xs text-muted-foreground px-2">
							v{availablePlugin.installed_version}
						</span>
					{:else}
						<button
							onclick={onInstall}
							disabled={installing}
							class="px-3 py-1.5 text-sm font-medium text-primary-foreground bg-primary rounded-md hover:bg-primary/90 transition-colors disabled:opacity-50 flex items-center gap-1"
						>
							{#if installing}
								<svg class="w-3 h-3 animate-spin" fill="none" viewBox="0 0 24 24">
									<circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
									<path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
								</svg>
							{:else}
								<svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
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
						class="relative w-11 h-6 rounded-full transition-colors {installedPlugin.enabled ? 'bg-primary' : 'bg-muted'}"
						title={installedPlugin.enabled ? 'Disable plugin' : 'Enable plugin'}
					>
						<span
							class="absolute top-1 w-4 h-4 rounded-full bg-white shadow transition-transform {installedPlugin.enabled ? 'left-6' : 'left-1'}"
						></span>
					</button>

					<!-- Details button -->
					<button
						onclick={onDetails}
						class="p-1.5 hover:bg-muted rounded-md transition-colors"
						title="View details"
					>
						<svg class="w-4 h-4 text-muted-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
						</svg>
					</button>

					<!-- Uninstall button -->
					<button
						onclick={onUninstall}
						disabled={installing}
						class="p-1.5 hover:bg-red-500/10 rounded-md transition-colors"
						title="Uninstall"
					>
						{#if installing}
							<svg class="w-4 h-4 text-muted-foreground animate-spin" fill="none" viewBox="0 0 24 24">
								<circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
								<path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
							</svg>
						{:else}
							<svg class="w-4 h-4 text-red-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
							</svg>
						{/if}
					</button>
				{/if}
			</div>
		</div>
	</div>
</div>
