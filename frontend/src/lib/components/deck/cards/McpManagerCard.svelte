<script lang="ts">
	/**
	 * McpManagerCard - MCP Server management card for The Deck
	 *
	 * Two view modes (like ProfileCard):
	 * - List view: Browse all globally available MCP servers (add/remove only)
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
	} from '$lib/stores/mcp.svelte';
	import type { McpServerInfo, McpServerType } from '$lib/api/mcp';
	import { validateMcpServerConfig } from '$lib/api/mcp';
	import { Server, AlertCircle, X, Check } from 'lucide-svelte';
	import BuildMcpList from '$lib/components/layout/build/BuildMcpList.svelte';
	import './card-design-system.css';

	interface Props {
		mobile?: boolean;
		/** When embedded in the Build page, render the header-less rows list
		 *  instead of the FeaturePageLayout list. The form is unchanged. */
		embedded?: boolean;
		/** Reports full-canvas detail/editor state so BuildPage can hide its chrome. */
		onDetailChange?: (detail: boolean) => void;
	}

	let {
		mobile = false,
		embedded = false,
		onDetailChange
	}: Props = $props();

	// View state
	let viewMode = $state<'list' | 'form'>('list');

	// Tell the Build page when we enter/leave the editor (form takes the canvas).
	$effect(() => {
		if (embedded) onDetailChange?.(viewMode === 'form');
	});
	let editingServer = $state<McpServerInfo | null>(null);


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


	// Direct delete for the embedded Build list (which owns its inline-confirm UI).
	async function embeddedDelete(name: string) {
		await mcp.removeGlobalServer(name);
		await mcp.loadGlobalServers();
	}

</script>

{#if viewMode === 'list'}
	<!-- ═══ LIST (Build page) ═══ -->
	<BuildMcpList
		servers={$globalMcpServers ?? []}
		error={$mcpError}
		onEdit={openEditForm}
		onNew={openNewForm}
		onDelete={embeddedDelete}
		onClearError={() => mcp.clearError()}
	/>
{:else if viewMode === 'form'}
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

	/* Meta code styling — truncate long paths */
	:global(.fp-meta-chip) {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		max-width: 100%;
	}

	:global(.fp-meta-chip code) {
		font-family: var(--font-mono, monospace);
		font-size: 0.6875rem;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	/* Constrain meta row to parent width so truncation works */
	:global(.fp-item-meta) {
		overflow: hidden;
		min-width: 0;
	}

	/* Mobile: actions wrap below, but chevron stays inline */
	@media (max-width: 639px) {
		:global(.fp-list-item) {
			flex-wrap: wrap;
		}

		:global(.fp-item-actions) {
			flex: 1 0 100%;
			justify-content: center;
			padding-top: var(--space-2, 8px);
			border-top: 1px solid var(--border-subtle);
			margin-top: var(--space-1, 4px);
		}

		:global(.fp-item-content) {
			min-width: 0;
			overflow: hidden;
		}
	}
</style>
