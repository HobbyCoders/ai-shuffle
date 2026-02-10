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
	import '$lib/components/deck/cards/card-design-system.css';

	interface Props {
		plugin: PluginDetailsType;
		onClose?: () => void;
		onInstall?: () => void;
		installing?: boolean;
	}

	let { plugin, onClose, onInstall, installing = false }: Props = $props();

	const isAvailable = $derived(plugin.scope === 'marketplace');

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
			.replace(/^### (.+)$/gm, '<h3 class="detail-md-h3">$1</h3>')
			.replace(/^## (.+)$/gm, '<h2 class="detail-md-h2">$1</h2>')
			.replace(/^# (.+)$/gm, '<h1 class="detail-md-h1">$1</h1>')
			// Bold
			.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
			// Italic
			.replace(/\*(.+?)\*/g, '<em>$1</em>')
			// Inline code
			.replace(/`([^`]+)`/g, '<code class="detail-md-code">$1</code>')
			// Links
			.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener" class="detail-md-link">$1</a>')
			// Line breaks
			.replace(/\n\n/g, '</p><p class="detail-md-p">')
			// Wrap in paragraph
			.replace(/^(.+)$/, '<p class="detail-md-p">$1</p>');
	}
</script>

<div class="plugin-detail card-system">
	<!-- Hero header -->
	<div class="plugin-detail-hero">
		<button onclick={onClose} class="card-back-btn">
			<svg fill="none" stroke="currentColor" viewBox="0 0 24 24">
				<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 19l-7-7 7-7" />
			</svg>
		</button>

		<div class="plugin-detail-hero-icon">
			<svg fill="none" stroke="currentColor" viewBox="0 0 24 24">
				<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 7v10c0 2.21 3.582 4 8 4s8-1.79 8-4V7M4 7c0 2.21 3.582 4 8 4s8-1.79 8-4M4 7c0-2.21 3.582-4 8-4s8 1.79 8 4" />
			</svg>
		</div>

		<div class="plugin-detail-hero-info">
			<h2 class="plugin-detail-name">{plugin.name}</h2>
			<div class="plugin-detail-meta">
				<span class="card-badge card-badge--muted">{plugin.marketplace}</span>
				{#if !isAvailable}
					<span class="card-badge card-badge--primary">v{plugin.version}</span>
					<span class="card-badge {plugin.enabled ? 'card-badge--success' : 'card-badge--muted'}">
						{plugin.enabled ? 'Enabled' : 'Disabled'}
					</span>
				{/if}
			</div>
			<p class="plugin-detail-description">{plugin.description || 'No description available'}</p>
			{#if isAvailable}
				<div class="plugin-detail-actions">
					<button
						onclick={onInstall}
						disabled={installing}
						class="card-btn-primary"
					>
						{#if installing}
							<span class="card-spinner card-spinner--small"></span>
						{:else}
							<svg fill="none" stroke="currentColor" viewBox="0 0 24 24" style="width:16px;height:16px">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" />
							</svg>
						{/if}
						Install
					</button>
				</div>
			{:else}
				<div class="plugin-detail-info-row">
					<span class="plugin-detail-info-item">
						<svg fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
						</svg>
						Installed {formatDate(plugin.installed_at)}
					</span>
					<span class="plugin-detail-info-item">
						<svg fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z" />
						</svg>
						Scope: {plugin.scope}
					</span>
				</div>
			{/if}
		</div>
	</div>

	<!-- Feature badges -->
	{#if hasCommands || hasAgents || hasSkills || hasHooks}
		<div class="plugin-detail-features">
			{#if hasCommands}
				<span class="plugin-detail-feature-badge plugin-detail-feature-badge--commands">
					<svg fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 9l3 3-3 3m5 0h3M5 20h14a2 2 0 002-2V6a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z" />
					</svg>
					{plugin.commands.length} Commands
				</span>
			{/if}
			{#if hasAgents}
				<span class="plugin-detail-feature-badge plugin-detail-feature-badge--agents">
					<svg fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0zm6 3a2 2 0 11-4 0 2 2 0 014 0zM7 10a2 2 0 11-4 0 2 2 0 014 0z" />
					</svg>
					{plugin.agents.length} Agents
				</span>
			{/if}
			{#if hasSkills}
				<span class="plugin-detail-feature-badge plugin-detail-feature-badge--skills">
					<svg fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z" />
					</svg>
					{plugin.skills.length} Skills
				</span>
			{/if}
			{#if hasHooks}
				<span class="plugin-detail-feature-badge plugin-detail-feature-badge--hooks">
					<svg fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13.828 10.172a4 4 0 00-5.656 0l-4 4a4 4 0 105.656 5.656l1.102-1.101m-.758-4.899a4 4 0 005.656 0l4-4a4 4 0 00-5.656-5.656l-1.1 1.1" />
					</svg>
					{plugin.hooks.length} Hooks
				</span>
			{/if}
		</div>
	{/if}

	<!-- Tabs -->
	<div class="plugin-detail-tabs">
		<button
			onclick={() => activeTab = 'overview'}
			class="plugin-detail-tab {activeTab === 'overview' ? 'plugin-detail-tab--active' : ''}"
		>
			Overview
		</button>
		{#if hasCommands}
			<button
				onclick={() => activeTab = 'commands'}
				class="plugin-detail-tab {activeTab === 'commands' ? 'plugin-detail-tab--active' : ''}"
			>
				Commands
			</button>
		{/if}
		{#if hasAgents}
			<button
				onclick={() => activeTab = 'agents'}
				class="plugin-detail-tab {activeTab === 'agents' ? 'plugin-detail-tab--active' : ''}"
			>
				Agents
			</button>
		{/if}
		{#if hasSkills}
			<button
				onclick={() => activeTab = 'skills'}
				class="plugin-detail-tab {activeTab === 'skills' ? 'plugin-detail-tab--active' : ''}"
			>
				Skills
			</button>
		{/if}
		{#if hasHooks}
			<button
				onclick={() => activeTab = 'hooks'}
				class="plugin-detail-tab {activeTab === 'hooks' ? 'plugin-detail-tab--active' : ''}"
			>
				Hooks
			</button>
		{/if}
	</div>

	<!-- Tab content -->
	<div class="plugin-detail-content">
		{#if activeTab === 'overview'}
			{#if plugin.readme}
				<div class="plugin-detail-readme">
					{@html renderMarkdown(plugin.readme)}
				</div>
			{:else}
				<div class="plugin-detail-empty">
					<svg fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
					</svg>
					<p>No README available for this plugin</p>
				</div>
			{/if}

			<!-- Install path info -->
			<div class="plugin-detail-install-info">
				<h4 class="plugin-detail-install-label">Install Location</h4>
				<code class="plugin-detail-install-path">{plugin.install_path}</code>
			</div>
		{:else if activeTab === 'commands'}
			<div class="plugin-detail-items">
				{#each plugin.commands as command}
					<div class="plugin-detail-item">
						<svg fill="none" stroke="currentColor" viewBox="0 0 24 24" class="plugin-detail-item-icon plugin-detail-item-icon--commands">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 9l3 3-3 3m5 0h3M5 20h14a2 2 0 002-2V6a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z" />
						</svg>
						<code class="plugin-detail-item-name">/{command}</code>
					</div>
				{/each}
			</div>
		{:else if activeTab === 'agents'}
			<div class="plugin-detail-items">
				{#each plugin.agents as agent}
					<div class="plugin-detail-item">
						<svg fill="none" stroke="currentColor" viewBox="0 0 24 24" class="plugin-detail-item-icon plugin-detail-item-icon--agents">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0zm6 3a2 2 0 11-4 0 2 2 0 014 0zM7 10a2 2 0 11-4 0 2 2 0 014 0z" />
						</svg>
						<span class="plugin-detail-item-name">{agent}</span>
					</div>
				{/each}
			</div>
		{:else if activeTab === 'skills'}
			<div class="plugin-detail-items">
				{#each plugin.skills as skill}
					<div class="plugin-detail-item">
						<svg fill="none" stroke="currentColor" viewBox="0 0 24 24" class="plugin-detail-item-icon plugin-detail-item-icon--skills">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z" />
						</svg>
						<span class="plugin-detail-item-name">{skill}</span>
					</div>
				{/each}
			</div>
		{:else if activeTab === 'hooks'}
			<div class="plugin-detail-items">
				{#each plugin.hooks as hook}
					<div class="plugin-detail-item">
						<svg fill="none" stroke="currentColor" viewBox="0 0 24 24" class="plugin-detail-item-icon plugin-detail-item-icon--hooks">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13.828 10.172a4 4 0 00-5.656 0l-4 4a4 4 0 105.656 5.656l1.102-1.101m-.758-4.899a4 4 0 005.656 0l4-4a4 4 0 00-5.656-5.656l-1.1 1.1" />
						</svg>
						<span class="plugin-detail-item-name">{hook}</span>
					</div>
				{/each}
			</div>
		{/if}
	</div>
</div>

<style>
	.plugin-detail {
		padding: var(--space-5, 20px);
	}

	/* Hero header */
	.plugin-detail-hero {
		display: flex;
		align-items: flex-start;
		gap: var(--space-4, 16px);
		padding-bottom: var(--space-5, 20px);
		border-bottom: 1px solid var(--border-subtle, var(--border));
		margin-bottom: var(--space-4, 16px);
	}

	.plugin-detail-hero-icon {
		width: 56px;
		height: 56px;
		flex-shrink: 0;
		border-radius: var(--radius-full, 9999px);
		background: linear-gradient(
			135deg,
			color-mix(in oklch, var(--primary) 18%, transparent) 0%,
			color-mix(in oklch, var(--primary) 8%, transparent) 100%
		);
		border: 1px solid color-mix(in oklch, var(--primary) 30%, transparent);
		box-shadow: var(--shadow-sm, 0 1px 3px rgba(0,0,0,0.12)),
			var(--shadow-glow-soft, 0 0 12px color-mix(in oklch, var(--primary) 15%, transparent));
		display: flex;
		align-items: center;
		justify-content: center;
		color: var(--primary);
	}

	.plugin-detail-hero-icon svg {
		width: 24px;
		height: 24px;
	}

	.plugin-detail-hero-info {
		flex: 1;
		min-width: 0;
	}

	.plugin-detail-name {
		font-size: var(--text-xl, 1.125rem);
		font-weight: 700;
		color: var(--text-primary, var(--foreground));
		letter-spacing: var(--tracking-tight, -0.01em);
		margin: 0;
	}

	.plugin-detail-meta {
		display: flex;
		align-items: center;
		gap: var(--space-2, 8px);
		flex-wrap: wrap;
		margin-top: var(--space-1, 4px);
	}

	.plugin-detail-description {
		font-size: var(--text-base, 0.8125rem);
		color: var(--text-secondary, var(--muted-foreground));
		margin-top: var(--space-2, 8px);
		line-height: 1.5;
	}

	.plugin-detail-actions {
		display: flex;
		gap: var(--space-2, 8px);
		margin-top: var(--space-3, 12px);
	}

	.plugin-detail-info-row {
		display: flex;
		align-items: center;
		gap: var(--space-4, 16px);
		margin-top: var(--space-2, 8px);
		flex-wrap: wrap;
	}

	.plugin-detail-info-item {
		display: flex;
		align-items: center;
		gap: var(--space-1, 4px);
		font-size: var(--text-sm, 0.75rem);
		color: var(--text-secondary, var(--muted-foreground));
	}

	.plugin-detail-info-item svg {
		width: 14px;
		height: 14px;
		flex-shrink: 0;
	}

	/* Feature badges */
	.plugin-detail-features {
		display: flex;
		flex-wrap: wrap;
		gap: var(--space-2, 8px);
		margin-bottom: var(--space-4, 16px);
	}

	.plugin-detail-feature-badge {
		display: flex;
		align-items: center;
		gap: 6px;
		padding: 6px 12px;
		font-size: var(--text-sm, 0.75rem);
		font-weight: 500;
		border-radius: var(--radius-lg, 10px);
	}

	.plugin-detail-feature-badge svg {
		width: 16px;
		height: 16px;
	}

	.plugin-detail-feature-badge--commands {
		background: color-mix(in oklch, var(--feature-commands, oklch(0.65 0.15 240)) 10%, transparent);
		color: var(--feature-commands, oklch(0.65 0.15 240));
		border: 1px solid color-mix(in oklch, var(--feature-commands, oklch(0.65 0.15 240)) 25%, transparent);
	}

	.plugin-detail-feature-badge--agents {
		background: color-mix(in oklch, var(--feature-agents, oklch(0.65 0.20 300)) 10%, transparent);
		color: var(--feature-agents, oklch(0.65 0.20 300));
		border: 1px solid color-mix(in oklch, var(--feature-agents, oklch(0.65 0.20 300)) 25%, transparent);
	}

	.plugin-detail-feature-badge--skills {
		background: color-mix(in oklch, var(--feature-skills, oklch(0.65 0.18 145)) 10%, transparent);
		color: var(--feature-skills, oklch(0.65 0.18 145));
		border: 1px solid color-mix(in oklch, var(--feature-skills, oklch(0.65 0.18 145)) 25%, transparent);
	}

	.plugin-detail-feature-badge--hooks {
		background: color-mix(in oklch, var(--feature-hooks, oklch(0.70 0.16 50)) 10%, transparent);
		color: var(--feature-hooks, oklch(0.70 0.16 50));
		border: 1px solid color-mix(in oklch, var(--feature-hooks, oklch(0.70 0.16 50)) 25%, transparent);
	}

	/* Back button - override via card-design-system card-back-btn */
	:global(.card-back-btn) {
		flex-shrink: 0;
	}

	/* Primary badge */
	:global(.card-badge--primary) {
		background: color-mix(in oklch, var(--primary) 15%, transparent);
		color: var(--primary);
		border: 1px solid color-mix(in oklch, var(--primary) 30%, transparent);
	}

	/* Tabs */
	.plugin-detail-tabs {
		display: flex;
		gap: 2px;
		border-bottom: 1px solid var(--border-subtle, var(--border));
		margin-bottom: var(--space-4, 16px);
	}

	.plugin-detail-tab {
		padding: var(--space-2, 8px) var(--space-4, 16px);
		font-size: var(--text-base, 0.8125rem);
		font-weight: 500;
		border: none;
		background: transparent;
		border-bottom: 2px solid transparent;
		color: var(--text-secondary, var(--muted-foreground));
		cursor: pointer;
		transition: all 150ms ease;
		margin-bottom: -1px;
	}

	.plugin-detail-tab:hover {
		color: var(--text-primary, var(--foreground));
	}

	.plugin-detail-tab--active {
		color: var(--primary);
		border-bottom-color: var(--primary);
	}

	/* Content area */
	.plugin-detail-content {
		min-height: 200px;
	}

	/* README markdown styles */
	.plugin-detail-readme {
		color: var(--text-primary, var(--foreground));
		line-height: 1.6;
	}

	.plugin-detail-readme :global(.detail-md-h1) {
		font-size: var(--text-xl, 1.125rem);
		font-weight: 700;
		color: var(--text-primary, var(--foreground));
		margin-top: var(--space-4, 16px);
		margin-bottom: var(--space-2, 8px);
	}

	.plugin-detail-readme :global(.detail-md-h2) {
		font-size: var(--text-lg, 1rem);
		font-weight: 600;
		color: var(--text-primary, var(--foreground));
		margin-top: var(--space-4, 16px);
		margin-bottom: var(--space-2, 8px);
	}

	.plugin-detail-readme :global(.detail-md-h3) {
		font-size: var(--text-md, 0.875rem);
		font-weight: 600;
		color: var(--text-primary, var(--foreground));
		margin-top: var(--space-4, 16px);
		margin-bottom: var(--space-2, 8px);
	}

	.plugin-detail-readme :global(.detail-md-p) {
		margin: var(--space-2, 8px) 0;
	}

	.plugin-detail-readme :global(.detail-md-code) {
		padding: 2px 6px;
		border-radius: var(--radius-sm, 4px);
		background: var(--surface-1, var(--muted));
		font-size: var(--text-sm, 0.75rem);
		font-family: var(--card-font-mono, 'JetBrains Mono', monospace);
	}

	.plugin-detail-readme :global(.detail-md-link) {
		color: var(--primary);
		text-decoration: none;
	}

	.plugin-detail-readme :global(.detail-md-link:hover) {
		text-decoration: underline;
	}

	/* Empty state */
	.plugin-detail-empty {
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		padding: var(--space-8, 32px) var(--space-4, 16px);
		text-align: center;
		color: var(--text-secondary, var(--muted-foreground));
	}

	.plugin-detail-empty svg {
		width: 48px;
		height: 48px;
		margin-bottom: var(--space-3, 12px);
		opacity: 0.5;
	}

	/* Install path info */
	.plugin-detail-install-info {
		margin-top: var(--space-6, 24px);
		padding: var(--space-3, 12px);
		border-radius: var(--radius-md, 8px);
		background: var(--surface-1, var(--muted));
		border: 1px solid var(--border-subtle, var(--border));
	}

	.plugin-detail-install-label {
		font-size: var(--text-sm, 0.75rem);
		font-weight: 500;
		color: var(--text-secondary, var(--muted-foreground));
		margin: 0 0 var(--space-1, 4px) 0;
	}

	.plugin-detail-install-path {
		font-size: var(--text-xs, 0.6875rem);
		color: var(--text-primary, var(--foreground));
		word-break: break-all;
		font-family: var(--card-font-mono, 'JetBrains Mono', monospace);
	}

	/* Item lists (commands, agents, skills, hooks) */
	.plugin-detail-items {
		display: flex;
		flex-direction: column;
		gap: var(--space-2, 8px);
	}

	.plugin-detail-item {
		display: flex;
		align-items: center;
		gap: var(--space-2, 8px);
		padding: var(--space-3, 12px);
		border-radius: var(--radius-md, 8px);
		background: var(--surface-1, var(--muted));
		border: 1px solid var(--border-subtle, var(--border));
	}

	.plugin-detail-item-icon {
		width: 16px;
		height: 16px;
		flex-shrink: 0;
	}

	.plugin-detail-item-icon--commands {
		color: var(--feature-commands, oklch(0.65 0.15 240));
	}

	.plugin-detail-item-icon--agents {
		color: var(--feature-agents, oklch(0.65 0.20 300));
	}

	.plugin-detail-item-icon--skills {
		color: var(--feature-skills, oklch(0.65 0.18 145));
	}

	.plugin-detail-item-icon--hooks {
		color: var(--feature-hooks, oklch(0.70 0.16 50));
	}

	.plugin-detail-item-name {
		font-size: var(--text-base, 0.8125rem);
		font-weight: 500;
		color: var(--text-primary, var(--foreground));
		font-family: var(--card-font-mono, 'JetBrains Mono', monospace);
	}

	/* Mobile adjustments */
	@media (max-width: 639px) {
		.plugin-detail {
			padding: var(--space-3, 12px);
		}

		.plugin-detail-hero {
			flex-wrap: wrap;
		}

		.plugin-detail-hero-icon {
			width: 48px;
			height: 48px;
		}

		.plugin-detail-hero-icon svg {
			width: 20px;
			height: 20px;
		}

		.plugin-detail-info-row {
			gap: var(--space-2, 8px);
		}
	}
</style>
