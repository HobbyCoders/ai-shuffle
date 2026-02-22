<script lang="ts">
	/**
	 * McpManagerCard - MCP Server management card for The Deck
	 *
	 * Two view modes (like ProfileCard):
	 * - List view: Browse all globally available MCP servers with enable/disable toggles
	 * - Form view: Add or edit an MCP server configuration (inline, not modal)
	 */
	import FeaturePageLayout from '$lib/components/layout/FeaturePageLayout.svelte';
	import { onMount } from 'svelte';
	import {
		mcp,
		globalMcpServers,
		mcpLoading,
		mcpError,
		mcpDisplayStats
	} from '$lib/stores/mcp';
	import type { McpServerInfo, McpServerType } from '$lib/api/mcp';
	import { getServerTypeLabel, validateMcpServerConfig } from '$lib/api/mcp';
	import { Server, AlertCircle, X, Plus, Search, Trash2, Check, Globe, Terminal, Zap, ToggleLeft, ToggleRight } from 'lucide-svelte';
	import './card-design-system.css';

	interface Props {
		mobile?: boolean;
	}

	let {
		mobile = false
	}: Props = $props();

	// View state
	let viewMode = $state<'list' | 'form'>('list');
	let editingServer = $state<McpServerInfo | null>(null);
	let searchQuery = $state('');

	// Delete confirmation
	let deletingName = $state<string | null>(null);
	let deleteLoading = $state(false);

	// Form state
	let formName = $state('');
	let formType = $state<McpServerType>('stdio');
	let formCommand = $state('');
	let formArgsText = $state('');
	let formUrl = $state('');
	let formDescription = $state('');
	let formEnvPairs = $state<Array<{ key: string; value: string }>>([]);
	let formHeaderPairs = $state<Array<{ key: string; value: string }>>([]);
	let formErrors = $state<string[]>([]);

	// Derived
	const isEditing = $derived(!!editingServer);
	const filteredServers = $derived(
		searchQuery.trim()
			? ($globalMcpServers ?? []).filter(
					(s) =>
						s.name.toLowerCase().includes(searchQuery.toLowerCase()) ||
						(s.description && s.description.toLowerCase().includes(searchQuery.toLowerCase()))
				)
			: ($globalMcpServers ?? [])
	);

	// Load data on mount
	onMount(() => {
		mcp.loadGlobalServers();
	});

	// Form helpers
	function resetForm() {
		formName = '';
		formType = 'stdio';
		formCommand = '';
		formArgsText = '';
		formUrl = '';
		formDescription = '';
		formEnvPairs = [];
		formHeaderPairs = [];
		formErrors = [];
	}

	function openNewForm() {
		resetForm();
		editingServer = null;
		viewMode = 'form';
	}

	function openEditForm(server: McpServerInfo) {
		editingServer = server;
		formName = server.name;
		formType = server.type;
		formCommand = server.command || '';
		formArgsText = server.args?.join(' ') || '';
		formUrl = server.url || '';
		formDescription = server.description || '';
		formEnvPairs = Object.entries(server.env || {}).map(([key, value]) => ({ key, value }));
		formHeaderPairs = Object.entries(server.headers || {}).map(([key, value]) => ({ key, value }));
		formErrors = [];
		viewMode = 'form';
	}

	function backToList() {
		viewMode = 'list';
		resetForm();
		editingServer = null;
	}

	function addEnvPair() {
		formEnvPairs = [...formEnvPairs, { key: '', value: '' }];
	}

	function removeEnvPair(index: number) {
		formEnvPairs = formEnvPairs.filter((_, i) => i !== index);
	}

	function addHeaderPair() {
		formHeaderPairs = [...formHeaderPairs, { key: '', value: '' }];
	}

	function removeHeaderPair(index: number) {
		formHeaderPairs = formHeaderPairs.filter((_, i) => i !== index);
	}

	async function handleSave() {
		const config = {
			name: formName.trim(),
			type: formType,
			command: formType === 'stdio' ? formCommand.trim() : undefined,
			args: formType === 'stdio' && formArgsText.trim() ? formArgsText.trim().split(/\s+/) : undefined,
			url: formType !== 'stdio' ? formUrl.trim() : undefined,
			env: formEnvPairs.length > 0
				? Object.fromEntries(formEnvPairs.filter(p => p.key.trim()).map(p => [p.key.trim(), p.value]))
				: undefined,
			headers: formType !== 'stdio' && formHeaderPairs.length > 0
				? Object.fromEntries(formHeaderPairs.filter(p => p.key.trim()).map(p => [p.key.trim(), p.value]))
				: undefined,
			description: formDescription.trim() || undefined,
		};

		const validation = validateMcpServerConfig(config);
		if (!validation.valid) {
			formErrors = validation.errors;
			return;
		}

		formErrors = [];

		try {
			if (editingServer) {
				await mcp.updateGlobalServer(editingServer.name, config);
			} else {
				await mcp.addGlobalServer(config);
			}
			backToList();
		} catch (e) {
			console.error('Failed to save MCP server:', e);
		}
	}

	async function handleToggle(server: McpServerInfo) {
		try {
			await mcp.toggleGlobalServer(server.name, !server.enabled);
		} catch (e) {
			console.error('Failed to toggle MCP server:', e);
		}
	}

	async function confirmDelete() {
		if (!deletingName) return;
		deleteLoading = true;
		try {
			await mcp.removeGlobalServer(deletingName);
			await mcp.loadGlobalServers();
			deletingName = null;
		} catch (e) {
			console.error('Failed to remove MCP server:', e);
		} finally {
			deleteLoading = false;
		}
	}

	function getTypeIcon(type: string) {
		switch (type) {
			case 'stdio': return Terminal;
			case 'sse': return Zap;
			case 'http': return Globe;
			default: return Server;
		}
	}

	function getTypeBadgeClass(type: string): string {
		switch (type) {
			case 'stdio': return 'mcp-badge--stdio';
			case 'sse': return 'mcp-badge--sse';
			case 'http': return 'mcp-badge--http';
			default: return '';
		}
	}
</script>

{#if viewMode === 'form'}
	<!-- ═══ FORM VIEW ═══ -->
	<FeaturePageLayout
		icon={Server}
		title="MCP Servers"
		breadcrumbs={[
			{ label: 'MCP Servers', onclick: backToList },
			{ label: editingServer ? editingServer.name : 'New Server' }
		]}
		noPadding={true}
	>
		{#snippet footer()}
			<div class="mcp-footer">
				<div></div>
				<button class="fp-btn fp-btn--primary" onclick={handleSave} disabled={$mcpLoading}>
					<Check size={14} />
					<span>{isEditing ? 'Save' : 'Add Server'}</span>
				</button>
			</div>
		{/snippet}

		<div class="card-system mcp-form-content">
			<!-- Errors -->
			{#if formErrors.length > 0}
				<div class="mcp-form-errors">
					<ul>
						{#each formErrors as error}
							<li>{error}</li>
						{/each}
					</ul>
				</div>
			{/if}

			{#if $mcpError}
				<div class="mcp-error-banner">
					<AlertCircle size={16} />
					<span>{$mcpError}</span>
					<button onclick={() => mcp.clearError()} class="mcp-error-close">
						<X size={14} />
					</button>
				</div>
			{/if}

			<!-- Server Name -->
			<div class="mcp-field">
				<label class="mcp-label" for="mcp-name">Server Name <span class="mcp-required">*</span></label>
				<input
					id="mcp-name"
					type="text"
					bind:value={formName}
					disabled={isEditing}
					placeholder="my-mcp-server"
					class="mcp-input"
				/>
				<p class="mcp-hint">Alphanumeric with hyphens/underscores only</p>
			</div>

			<!-- Server Type -->
			<div class="mcp-field">
				<label class="mcp-label" for="mcp-type">Server Type</label>
				<select
					id="mcp-type"
					bind:value={formType}
					class="mcp-input"
				>
					<option value="stdio">Local Process (stdio)</option>
					<option value="sse">SSE (Remote)</option>
					<option value="http">HTTP (Remote)</option>
				</select>
			</div>

			<!-- Type-specific fields -->
			{#if formType === 'stdio'}
				<div class="mcp-field">
					<label class="mcp-label" for="mcp-command">Command <span class="mcp-required">*</span></label>
					<input
						id="mcp-command"
						type="text"
						bind:value={formCommand}
						placeholder="node, python, npx, etc."
						class="mcp-input"
					/>
				</div>
				<div class="mcp-field">
					<label class="mcp-label" for="mcp-args">Arguments <span class="mcp-optional">(optional)</span></label>
					<input
						id="mcp-args"
						type="text"
						bind:value={formArgsText}
						placeholder="./server.js --port 3000"
						class="mcp-input"
					/>
					<p class="mcp-hint">Space-separated arguments</p>
				</div>
			{:else}
				<div class="mcp-field">
					<label class="mcp-label" for="mcp-url">URL <span class="mcp-required">*</span></label>
					<input
						id="mcp-url"
						type="url"
						bind:value={formUrl}
						placeholder="https://api.example.com/mcp"
						class="mcp-input"
					/>
				</div>

				<!-- Headers -->
				<div class="mcp-field">
					<div class="mcp-field-header">
						<span class="mcp-label">Headers <span class="mcp-optional">(optional)</span></span>
						<button type="button" onclick={addHeaderPair} class="mcp-add-link">+ Add header</button>
					</div>
					{#if formHeaderPairs.length > 0}
						<div class="mcp-pairs">
							{#each formHeaderPairs as pair, i}
								<div class="mcp-pair-row">
									<input type="text" bind:value={pair.key} placeholder="Header name" class="mcp-input mcp-input--sm" />
									<input type="text" bind:value={pair.value} placeholder="Value" class="mcp-input mcp-input--sm" />
									<button type="button" onclick={() => removeHeaderPair(i)} class="mcp-pair-remove" aria-label="Remove">
										<X size={14} />
									</button>
								</div>
							{/each}
						</div>
					{:else}
						<p class="mcp-hint">No headers configured</p>
					{/if}
				</div>
			{/if}

			<!-- Environment Variables -->
			<div class="mcp-field">
				<div class="mcp-field-header">
					<span class="mcp-label">Environment Variables <span class="mcp-optional">(optional)</span></span>
					<button type="button" onclick={addEnvPair} class="mcp-add-link">+ Add variable</button>
				</div>
				{#if formEnvPairs.length > 0}
					<div class="mcp-pairs">
						{#each formEnvPairs as pair, i}
							<div class="mcp-pair-row">
								<input type="text" bind:value={pair.key} placeholder="VARIABLE_NAME" class="mcp-input mcp-input--sm" />
								<input type="text" bind:value={pair.value} placeholder="Value" class="mcp-input mcp-input--sm" />
								<button type="button" onclick={() => removeEnvPair(i)} class="mcp-pair-remove" aria-label="Remove">
									<X size={14} />
								</button>
							</div>
						{/each}
					</div>
				{:else}
					<p class="mcp-hint">No environment variables configured</p>
				{/if}
			</div>

			<!-- Description -->
			<div class="mcp-field">
				<label class="mcp-label" for="mcp-desc">Description <span class="mcp-optional">(optional)</span></label>
				<input
					id="mcp-desc"
					type="text"
					bind:value={formDescription}
					placeholder="What this server provides"
					class="mcp-input"
				/>
			</div>
		</div>
	</FeaturePageLayout>
{:else}
	<!-- ═══ LIST VIEW ═══ -->
	<FeaturePageLayout
		icon={Server}
		title="MCP Servers"
		itemCount={($globalMcpServers ?? []).length}
		itemLabel="servers"
	>
		{#snippet footer()}
			<div class="mcp-footer">
				<div></div>
				<button class="fp-btn fp-btn--primary" onclick={openNewForm}>
					<Plus size={14} /> <span class="btn-label">Add Server</span>
				</button>
			</div>
		{/snippet}

		{#if ($globalMcpServers ?? []).length > 3}
			<div class="fp-search-bar">
				<div class="fph-search-wrapper">
					<Search size={14} />
					<input type="text" bind:value={searchQuery} placeholder="Search servers..." class="fph-search-input" />
				</div>
			</div>
		{/if}

		{#if $mcpError}
			<div class="mcp-error-banner" style="margin: 12px 16px 0;">
				<AlertCircle size={16} />
				<span>{$mcpError}</span>
				<button onclick={() => mcp.clearError()} class="mcp-error-close">
					<X size={14} />
				</button>
			</div>
		{/if}

		{#if filteredServers.length === 0}
			<div class="card-empty-state">
				{#if searchQuery}
					<p class="card-empty-title">No servers match your search</p>
				{:else}
					<svg class="card-empty-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M9 3v2m6-2v2M9 19v2m6-2v2M5 9H3m2 6H3m18-6h-2m2 6h-2M7 19h10a2 2 0 002-2V7a2 2 0 00-2-2H7a2 2 0 00-2 2v10a2 2 0 002 2zM9 9h6v6H9V9z" />
					</svg>
					<p class="card-empty-title">No MCP servers configured</p>
					<p class="card-empty-description">Add an MCP server to extend Claude's capabilities</p>
				{/if}
			</div>
		{:else}
			<div class="fp-list">
				{#each filteredServers as server (server.name)}
					{#if deletingName === server.name}
						<!-- Inline delete confirmation -->
						<div class="fp-list-item fp-list-item--deleting">
							<div class="fp-item-icon fp-item-icon--destructive">
								<Trash2 size={18} />
							</div>
							<div class="fp-item-content">
								<p class="delete-confirm-text">
									Delete <strong>{server.name}</strong>? This cannot be undone.
								</p>
								<div class="delete-confirm-actions">
									<button onclick={() => (deletingName = null)} disabled={deleteLoading} class="fp-btn fp-btn--secondary fp-btn--sm">Cancel</button>
									<button onclick={confirmDelete} disabled={deleteLoading} class="fp-btn fp-btn--destructive fp-btn--sm">
										{#if deleteLoading}<div class="card-spinner card-spinner--small"></div>{/if}
										Delete
									</button>
								</div>
							</div>
						</div>
					{:else}
						<div
							class="fp-list-item"
							class:mcp-disabled={server.builtin && !server.enabled}
							onclick={() => { if (!server.builtin) openEditForm(server); }}
							role="button"
							tabindex="0"
							onkeydown={(e) => e.key === 'Enter' && !server.builtin && openEditForm(server)}
						>
							<div class="fp-item-icon fp-item-icon--primary" class:fp-item-icon--muted={server.builtin && !server.enabled}>
								{#if server.type === 'stdio'}
									<Terminal size={18} />
								{:else if server.type === 'sse'}
									<Zap size={18} />
								{:else if server.type === 'http'}
									<Globe size={18} />
								{:else}
									<Server size={18} />
								{/if}
							</div>
							<div class="fp-item-content">
								<div class="fp-item-header">
									<span class="fp-item-name">{server.name}</span>
									<span class="mcp-type-badge {getTypeBadgeClass(server.type)}">
										{getServerTypeLabel(server.type)}
									</span>
									{#if server.builtin}
										<span class="mcp-builtin-badge">Built-in</span>
										<span class="card-badge {server.enabled ? 'card-badge--success' : 'card-badge--muted'}">
											{server.enabled ? 'Enabled' : 'Disabled'}
										</span>
									{/if}
								</div>
								{#if server.description}
									<span class="fp-item-description">{server.description}</span>
								{/if}
								<div class="fp-item-meta">
									{#if server.type === 'stdio' && server.command}
										<span class="fp-meta-chip"><Terminal size={12} /> <code>{server.command}{server.args ? ' ' + server.args.join(' ') : ''}</code></span>
									{:else if (server.type === 'sse' || server.type === 'http') && server.url}
										<span class="fp-meta-chip"><Globe size={12} /> {server.url}</span>
									{:else if server.builtin}
										<span class="fp-meta-chip"><Server size={12} /> In-process SDK server</span>
									{/if}
								</div>
							</div>
							<!-- Item actions -->
							<div class="fp-item-actions" onclick={(e) => e.stopPropagation()} onkeydown={(e) => e.stopPropagation()} role="toolbar" tabindex="-1">
								{#if server.builtin}
									<button
										class="card-action-btn"
										title={server.enabled ? 'Disable server' : 'Enable server'}
										onclick={() => handleToggle(server)}
									>
										{#if server.enabled}
											<ToggleRight size={16} />
										{:else}
											<ToggleLeft size={16} />
										{/if}
									</button>
								{:else}
									<button
										class="card-action-btn card-action-btn--destructive"
										title="Delete"
										onclick={() => { deletingName = server.name; }}
									>
										<Trash2 size={16} />
									</button>
								{/if}
							</div>
						</div>
					{/if}
				{/each}
			</div>
		{/if}
	</FeaturePageLayout>
{/if}

<style>
	/* ═══════════════════════════════════════════════════════════════════════════
	   MCP MANAGER CARD
	   ═══════════════════════════════════════════════════════════════════════════ */

	/* Footer */
	.mcp-footer {
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding: 8px 16px;
		gap: 8px;
	}

	/* Error banner */
	.mcp-error-banner {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 10px 12px;
		border-radius: 8px;
		background: color-mix(in oklch, var(--destructive) 12%, transparent);
		color: var(--destructive);
		font-size: 0.8125rem;
	}

	.mcp-error-close {
		margin-left: auto;
		padding: 4px;
		background: transparent;
		border: none;
		color: var(--destructive);
		cursor: pointer;
		border-radius: 4px;
	}

	/* Disabled server row */
	.mcp-disabled {
		opacity: 0.55;
	}

	/* Type badges */
	.mcp-type-badge {
		display: inline-flex;
		align-items: center;
		padding: 1px 6px;
		font-size: 0.6875rem;
		font-weight: 500;
		border-radius: 4px;
		background: var(--surface-2, color-mix(in oklch, var(--muted) 50%, transparent));
		color: var(--text-secondary);
	}

	.mcp-badge--stdio {
		background: color-mix(in oklch, oklch(0.7 0.15 250) 12%, transparent);
		color: oklch(0.7 0.15 250);
	}

	.mcp-badge--sse {
		background: color-mix(in oklch, oklch(0.75 0.15 85) 12%, transparent);
		color: oklch(0.75 0.15 85);
	}

	.mcp-badge--http {
		background: color-mix(in oklch, oklch(0.7 0.15 150) 12%, transparent);
		color: oklch(0.7 0.15 150);
	}

	.mcp-builtin-badge {
		display: inline-flex;
		align-items: center;
		padding: 1px 6px;
		font-size: 0.6875rem;
		font-weight: 500;
		border-radius: 4px;
		background: color-mix(in oklch, var(--primary) 12%, transparent);
		color: var(--primary);
	}

	:global(.fp-item-icon--muted) {
		opacity: 0.4;
	}

	/* ═══════════════════════════════════════════════════════════════════════════
	   FORM VIEW
	   ═══════════════════════════════════════════════════════════════════════════ */

	.mcp-form-content {
		padding: 16px;
		display: flex;
		flex-direction: column;
		gap: 16px;
		overflow-y: auto;
	}

	.mcp-form-errors {
		padding: 10px 12px;
		border-radius: 8px;
		background: color-mix(in oklch, var(--destructive) 10%, transparent);
		border: 1px solid color-mix(in oklch, var(--destructive) 20%, transparent);
	}

	.mcp-form-errors ul {
		margin: 0;
		padding: 0 0 0 16px;
		font-size: 0.8125rem;
		color: var(--destructive);
	}

	.mcp-field {
		display: flex;
		flex-direction: column;
		gap: 6px;
	}

	.mcp-field-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
	}

	.mcp-label {
		font-size: 0.8125rem;
		font-weight: 500;
		color: var(--foreground);
	}

	.mcp-required {
		color: var(--destructive);
	}

	.mcp-optional {
		font-weight: 400;
		color: var(--muted-foreground);
	}

	.mcp-input {
		width: 100%;
		padding: 8px 12px;
		background: var(--background);
		border: 1px solid var(--border);
		border-radius: 8px;
		color: var(--foreground);
		font-size: 0.875rem;
		outline: none;
		transition: border-color 150ms ease, box-shadow 150ms ease;
	}

	.mcp-input:focus {
		border-color: var(--ring);
		box-shadow: 0 0 0 2px color-mix(in oklch, var(--ring) 25%, transparent);
	}

	.mcp-input:disabled {
		opacity: 0.5;
	}

	.mcp-input::placeholder {
		color: var(--muted-foreground);
	}

	.mcp-input--sm {
		padding: 6px 10px;
		font-size: 0.8125rem;
	}

	.mcp-hint {
		font-size: 0.75rem;
		color: var(--muted-foreground);
		margin: 0;
	}

	.mcp-add-link {
		background: none;
		border: none;
		color: var(--primary);
		font-size: 0.75rem;
		cursor: pointer;
		padding: 0;
	}

	.mcp-add-link:hover {
		text-decoration: underline;
	}

	.mcp-pairs {
		display: flex;
		flex-direction: column;
		gap: 8px;
	}

	.mcp-pair-row {
		display: flex;
		gap: 8px;
		align-items: center;
	}

	.mcp-pair-row .mcp-input {
		flex: 1;
	}

	.mcp-pair-remove {
		padding: 6px;
		background: transparent;
		border: none;
		color: var(--destructive);
		cursor: pointer;
		border-radius: 6px;
		transition: background 150ms ease;
		flex-shrink: 0;
	}

	.mcp-pair-remove:hover {
		background: color-mix(in oklch, var(--destructive) 10%, transparent);
	}

	/* Meta code styling */
	:global(.fp-meta-chip code) {
		font-family: var(--font-mono, monospace);
		font-size: 0.6875rem;
	}
</style>
