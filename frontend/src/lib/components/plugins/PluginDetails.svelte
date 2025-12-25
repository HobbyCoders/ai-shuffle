<script lang="ts">
	/**
	 * PluginDetails - Displays detailed information about a plugin
	 *
	 * Shows:
	 * - Plugin metadata (name, description, version, marketplace)
	 * - Feature badges (commands, agents, skills, hooks)
	 * - README content
	 * - Lists of commands, agents, skills, and hooks
	 */
	import type { PluginDetails as PluginDetailsType } from '$lib/api/plugins';

	interface Props {
		plugin: PluginDetailsType;
		onClose?: () => void;
	}

	let { plugin, onClose }: Props = $props();

	// Tabs for different content sections
	type DetailTab = 'overview' | 'commands' | 'agents' | 'skills' | 'hooks';
	let activeTab = $state<DetailTab>('overview');

	// Count non-empty arrays
	const hasCommands = $derived(plugin.commands && plugin.commands.length > 0);
	const hasAgents = $derived(plugin.agents && plugin.agents.length > 0);
	const hasSkills = $derived(plugin.skills && plugin.skills.length > 0);
	const hasHooks = $derived(plugin.hooks && plugin.hooks.length > 0);

	// Format install date
	function formatDate(dateStr: string): string {
		try {
			const date = new Date(dateStr);
			return date.toLocaleDateString(undefined, {
				year: 'numeric',
				month: 'long',
				day: 'numeric'
			});
		} catch {
			return 'Unknown';
		}
	}

	// Simple markdown to HTML (very basic)
	function renderMarkdown(text: string): string {
		if (!text) return '';

		return text
			// Headers
			.replace(/^### (.+)$/gm, '<h3 class="text-lg font-semibold text-foreground mt-4 mb-2">$1</h3>')
			.replace(/^## (.+)$/gm, '<h2 class="text-xl font-semibold text-foreground mt-4 mb-2">$1</h2>')
			.replace(/^# (.+)$/gm, '<h1 class="text-2xl font-bold text-foreground mt-4 mb-2">$1</h1>')
			// Bold
			.replace(/\*\*(.+?)\*\*/g, '<strong class="font-semibold">$1</strong>')
			// Italic
			.replace(/\*(.+?)\*/g, '<em>$1</em>')
			// Inline code
			.replace(/`([^`]+)`/g, '<code class="px-1.5 py-0.5 rounded bg-muted text-sm font-mono">$1</code>')
			// Links
			.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener" class="text-primary hover:underline">$1</a>')
			// Line breaks
			.replace(/\n\n/g, '</p><p class="my-2">')
			// Wrap in paragraph
			.replace(/^(.+)$/, '<p class="my-2">$1</p>');
	}
</script>

<div class="p-5">
	<!-- Header with back button -->
	<div class="flex items-start gap-4 mb-6">
		<button
			onclick={onClose}
			class="p-2 hover:bg-muted rounded-lg transition-colors flex-shrink-0"
		>
			<svg class="w-5 h-5 text-muted-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
				<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 19l-7-7 7-7" />
			</svg>
		</button>

		<div class="flex-1 min-w-0">
			<div class="flex items-center gap-3 flex-wrap">
				<h2 class="text-xl font-bold text-foreground">{plugin.name}</h2>
				<span class="px-2 py-0.5 text-xs bg-muted text-muted-foreground rounded">
					{plugin.marketplace}
				</span>
				<span class="px-2 py-0.5 text-xs bg-primary/10 text-primary rounded">
					v{plugin.version}
				</span>
				<span class="px-2 py-0.5 text-xs rounded {plugin.enabled ? 'bg-green-500/10 text-green-500' : 'bg-muted text-muted-foreground'}">
					{plugin.enabled ? 'Enabled' : 'Disabled'}
				</span>
			</div>

			<p class="text-muted-foreground mt-1">
				{plugin.description || 'No description available'}
			</p>

			<div class="flex items-center gap-4 mt-2 text-sm text-muted-foreground">
				<span class="flex items-center gap-1">
					<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
					</svg>
					Installed {formatDate(plugin.installed_at)}
				</span>
				<span class="flex items-center gap-1">
					<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z" />
					</svg>
					Scope: {plugin.scope}
				</span>
			</div>
		</div>
	</div>

	<!-- Feature badges -->
	<div class="flex flex-wrap gap-2 mb-4">
		{#if hasCommands}
			<span class="px-2 py-1 text-sm bg-blue-500/10 text-blue-500 rounded-lg flex items-center gap-1.5">
				<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 9l3 3-3 3m5 0h3M5 20h14a2 2 0 002-2V6a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z" />
				</svg>
				{plugin.commands.length} Commands
			</span>
		{/if}
		{#if hasAgents}
			<span class="px-2 py-1 text-sm bg-purple-500/10 text-purple-500 rounded-lg flex items-center gap-1.5">
				<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0zm6 3a2 2 0 11-4 0 2 2 0 014 0zM7 10a2 2 0 11-4 0 2 2 0 014 0z" />
				</svg>
				{plugin.agents.length} Agents
			</span>
		{/if}
		{#if hasSkills}
			<span class="px-2 py-1 text-sm bg-green-500/10 text-green-500 rounded-lg flex items-center gap-1.5">
				<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z" />
				</svg>
				{plugin.skills.length} Skills
			</span>
		{/if}
		{#if hasHooks}
			<span class="px-2 py-1 text-sm bg-orange-500/10 text-orange-500 rounded-lg flex items-center gap-1.5">
				<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13.828 10.172a4 4 0 00-5.656 0l-4 4a4 4 0 105.656 5.656l1.102-1.101m-.758-4.899a4 4 0 005.656 0l4-4a4 4 0 00-5.656-5.656l-1.1 1.1" />
				</svg>
				{plugin.hooks.length} Hooks
			</span>
		{/if}
	</div>

	<!-- Tabs -->
	<div class="flex items-center gap-1 mb-4 border-b border-border">
		<button
			onclick={() => activeTab = 'overview'}
			class="px-4 py-2 text-sm font-medium transition-colors border-b-2 -mb-px {activeTab === 'overview' ? 'border-primary text-foreground' : 'border-transparent text-muted-foreground hover:text-foreground'}"
		>
			Overview
		</button>
		{#if hasCommands}
			<button
				onclick={() => activeTab = 'commands'}
				class="px-4 py-2 text-sm font-medium transition-colors border-b-2 -mb-px {activeTab === 'commands' ? 'border-primary text-foreground' : 'border-transparent text-muted-foreground hover:text-foreground'}"
			>
				Commands
			</button>
		{/if}
		{#if hasAgents}
			<button
				onclick={() => activeTab = 'agents'}
				class="px-4 py-2 text-sm font-medium transition-colors border-b-2 -mb-px {activeTab === 'agents' ? 'border-primary text-foreground' : 'border-transparent text-muted-foreground hover:text-foreground'}"
			>
				Agents
			</button>
		{/if}
		{#if hasSkills}
			<button
				onclick={() => activeTab = 'skills'}
				class="px-4 py-2 text-sm font-medium transition-colors border-b-2 -mb-px {activeTab === 'skills' ? 'border-primary text-foreground' : 'border-transparent text-muted-foreground hover:text-foreground'}"
			>
				Skills
			</button>
		{/if}
		{#if hasHooks}
			<button
				onclick={() => activeTab = 'hooks'}
				class="px-4 py-2 text-sm font-medium transition-colors border-b-2 -mb-px {activeTab === 'hooks' ? 'border-primary text-foreground' : 'border-transparent text-muted-foreground hover:text-foreground'}"
			>
				Hooks
			</button>
		{/if}
	</div>

	<!-- Tab content -->
	<div class="min-h-[200px]">
		{#if activeTab === 'overview'}
			{#if plugin.readme}
				<div class="prose prose-sm dark:prose-invert max-w-none text-foreground">
					{@html renderMarkdown(plugin.readme)}
				</div>
			{:else}
				<div class="text-center py-8 text-muted-foreground">
					<svg class="w-12 h-12 mx-auto mb-3 opacity-50" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
					</svg>
					<p>No README available for this plugin</p>
				</div>
			{/if}

			<!-- Install path info -->
			<div class="mt-6 p-3 rounded-lg bg-muted/50 border border-border">
				<h4 class="text-sm font-medium text-muted-foreground mb-1">Install Location</h4>
				<code class="text-xs text-foreground break-all">{plugin.install_path}</code>
			</div>
		{:else if activeTab === 'commands'}
			<div class="space-y-2">
				{#each plugin.commands as command}
					<div class="p-3 rounded-lg bg-muted/50 border border-border">
						<div class="flex items-center gap-2">
							<svg class="w-4 h-4 text-blue-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 9l3 3-3 3m5 0h3M5 20h14a2 2 0 002-2V6a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z" />
							</svg>
							<code class="text-sm font-mono text-foreground">/{command}</code>
						</div>
					</div>
				{/each}
			</div>
		{:else if activeTab === 'agents'}
			<div class="space-y-2">
				{#each plugin.agents as agent}
					<div class="p-3 rounded-lg bg-muted/50 border border-border">
						<div class="flex items-center gap-2">
							<svg class="w-4 h-4 text-purple-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0zm6 3a2 2 0 11-4 0 2 2 0 014 0zM7 10a2 2 0 11-4 0 2 2 0 014 0z" />
							</svg>
							<span class="text-sm font-medium text-foreground">{agent}</span>
						</div>
					</div>
				{/each}
			</div>
		{:else if activeTab === 'skills'}
			<div class="space-y-2">
				{#each plugin.skills as skill}
					<div class="p-3 rounded-lg bg-muted/50 border border-border">
						<div class="flex items-center gap-2">
							<svg class="w-4 h-4 text-green-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z" />
							</svg>
							<span class="text-sm font-medium text-foreground">{skill}</span>
						</div>
					</div>
				{/each}
			</div>
		{:else if activeTab === 'hooks'}
			<div class="space-y-2">
				{#each plugin.hooks as hook}
					<div class="p-3 rounded-lg bg-muted/50 border border-border">
						<div class="flex items-center gap-2">
							<svg class="w-4 h-4 text-orange-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13.828 10.172a4 4 0 00-5.656 0l-4 4a4 4 0 105.656 5.656l1.102-1.101m-.758-4.899a4 4 0 005.656 0l4-4a4 4 0 00-5.656-5.656l-1.1 1.1" />
							</svg>
							<span class="text-sm font-medium text-foreground">{hook}</span>
						</div>
					</div>
				{/each}
			</div>
		{/if}
	</div>
</div>
