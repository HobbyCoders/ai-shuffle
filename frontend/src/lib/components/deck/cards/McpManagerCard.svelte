<script lang="ts">
	/**
	 * McpManagerCard - MCP Server management card for The Deck
	 *
	 * Two view modes (like ProfileCard):
	 * - List view: Browse all globally available MCP servers (add/remove only)
	 * - Form view: Add or edit an MCP server configuration (inline, not modal)
	 */
	import StudioEditorShell from '$lib/components/layout/StudioEditorShell.svelte';
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
	import { AlertCircle, Check, Trash2, Server, Network, KeyRound } from 'lucide-svelte';
	import BuildMcpList from '$lib/components/layout/build/BuildMcpList.svelte';
	import Banner from '$lib/components/Banner.svelte';

	interface Props {
		mobile?: boolean;
		/** When embedded in the Build page, render the header-less rows list
		 *  while keeping the same editor form. */
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
	<StudioEditorShell
		title={editingServer ? editingServer.name : 'New Server'}
		subtitle={isEditing ? 'MCP server' : 'Add a new MCP server'}
		backLabel="MCP Servers"
		onBack={backToList}
	>
		{#snippet actions()}
			<button type="button" class="ed-btn primary" onclick={handleSave} disabled={$mcpLoading}>
				<Check size={14} />
				<span>{isEditing ? 'Save' : 'Add Server'}</span>
			</button>
		{/snippet}

		<!-- Errors -->
		{#if formErrors.length > 0}
			<div class="form-errors">
				<AlertCircle size={15} />
				<ul>
					{#each formErrors as error}
						<li>{error}</li>
					{/each}
				</ul>
			</div>
		{/if}

		{#if $mcpError}
			<Banner severity="error" message={$mcpError} dismissible onDismiss={() => mcp.clearError()} />
		{/if}

		<!-- Server -->
		<div class="set-group">
			<div class="set-grouphead"><h3><Server />Server</h3><p>Identify this MCP server.</p></div>
			<div class="set-row">
				<div class="lab"><h4>Name <span class="req">*</span></h4><p>Alphanumeric with hyphens/underscores only.</p></div>
				<div class="ctl"><input type="text" bind:value={formName} disabled={isEditing} placeholder="my-mcp-server" class="inp mono" /></div>
			</div>
			<div class="set-row">
				<div class="lab"><h4>Description</h4><p>What this server provides. Optional.</p></div>
				<div class="ctl"><input type="text" bind:value={formDescription} placeholder="What this server provides" class="inp" /></div>
			</div>
		</div>

		<!-- Transport -->
		<div class="set-group">
			<div class="set-grouphead"><h3><Network />Transport</h3><p>How the app connects to this server.</p></div>
			<div class="set-row">
				<div class="lab"><h4>Type</h4><p>Local subprocess or a remote endpoint.</p></div>
				<div class="ctl">
					<select bind:value={formType} class="sel">
						<option value="stdio">Local Process (stdio)</option>
						<option value="sse">SSE (Remote)</option>
						<option value="http">HTTP (Remote)</option>
					</select>
				</div>
			</div>
			{#if formType === 'stdio'}
				<div class="set-row">
					<div class="lab"><h4>Command <span class="req">*</span></h4><p>The executable to launch.</p></div>
					<div class="ctl"><input type="text" bind:value={formCommand} placeholder="node, python, npx, etc." class="inp mono" /></div>
				</div>
				<div class="set-row stack">
					<div class="lab"><h4>Arguments</h4><p>Space-separated arguments. Optional.</p></div>
					<div class="ctl"><input type="text" bind:value={formArgsText} placeholder="./server.js --port 3000" class="inp mono" /></div>
				</div>
			{:else}
				<div class="set-row stack">
					<div class="lab"><h4>URL <span class="req">*</span></h4><p>The remote endpoint to connect to.</p></div>
					<div class="ctl"><input type="url" bind:value={formUrl} placeholder="https://api.example.com/mcp" class="inp mono" /></div>
				</div>
			{/if}
		</div>

		<!-- Headers (remote only) -->
		{#if formType !== 'stdio'}
			<div class="set-group">
				<div class="set-grouphead grouphead-row">
					<h3><KeyRound />Headers</h3>
					<button type="button" class="lnk" onclick={addHeaderPair}>+ Add header</button>
				</div>
				{#if formHeaderPairs.length > 0}
					<div class="kv-list">
						{#each formHeaderPairs as pair, i}
							<div class="kv-row">
								<input type="text" bind:value={pair.key} placeholder="Header name" class="inp mono" />
								<input type="text" bind:value={pair.value} placeholder="Value" class="inp mono" />
								<button type="button" class="icon-btn danger" onclick={() => removeHeaderPair(i)} aria-label="Remove header"><Trash2 size={14} /></button>
							</div>
						{/each}
					</div>
				{:else}
					<div class="empty">No headers configured.</div>
				{/if}
			</div>
		{/if}

		<!-- Environment variables -->
		<div class="set-group">
			<div class="set-grouphead grouphead-row">
				<h3><KeyRound />Environment variables</h3>
				<button type="button" class="lnk" onclick={addEnvPair}>+ Add variable</button>
			</div>
			{#if formEnvPairs.length > 0}
				<div class="kv-list">
					{#each formEnvPairs as pair, i}
						<div class="kv-row">
							<input type="text" bind:value={pair.key} placeholder="VARIABLE_NAME" class="inp mono" />
							<input type="text" bind:value={pair.value} placeholder="Value" class="inp mono" />
							<button type="button" class="icon-btn danger" onclick={() => removeEnvPair(i)} aria-label="Remove variable"><Trash2 size={14} /></button>
						</div>
					{/each}
				</div>
			{:else}
				<div class="empty">No environment variables configured.</div>
			{/if}
		</div>
	</StudioEditorShell>
{/if}

<style>
	/* ═══════════════════════════════════════════════════════════════════════════
	   MCP MANAGER CARD — form view (Studio-v2 grouped fields)
	   ═══════════════════════════════════════════════════════════════════════════ */

	.req {
		color: var(--destructive);
		font-weight: 700;
	}

	/* Validation + error banners */
	.form-errors {
		display: flex;
		align-items: flex-start;
		gap: 9px;
		padding: 11px 14px;
		border-radius: 10px;
		background: color-mix(in oklch, var(--destructive) 10%, transparent);
		border: 1px solid color-mix(in oklch, var(--destructive) 24%, transparent);
		color: var(--destructive);
	}
	.form-errors :global(svg) {
		flex-shrink: 0;
		margin-top: 1px;
	}
	.form-errors ul {
		margin: 0;
		padding: 0 0 0 2px;
		list-style: none;
		font-size: 12.5px;
		display: flex;
		flex-direction: column;
		gap: 3px;
	}

	/* Group head with a trailing action link */
	.grouphead-row {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 12px;
	}
	.lnk {
		background: none;
		border: none;
		padding: 0;
		font: inherit;
		font-size: 12.5px;
		font-weight: 600;
		color: var(--foreground);
		cursor: pointer;
	}
	.lnk:hover {
		text-decoration: underline;
	}

	/* Key/value rows (headers + env vars) */
	.kv-list {
		display: flex;
		flex-direction: column;
		gap: 8px;
		padding: 6px 18px 18px;
	}
	.kv-row {
		display: flex;
		gap: 8px;
		align-items: center;
	}
	.kv-row :global(.inp) {
		flex: 1;
		min-width: 0;
	}
	.icon-btn {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 34px;
		height: 34px;
		flex-shrink: 0;
		background: var(--background);
		border: 1px solid var(--border);
		border-radius: 9px;
		color: var(--muted-foreground);
		cursor: pointer;
		transition: background 0.12s, color 0.12s, border-color 0.12s;
	}
	.icon-btn:hover {
		background: var(--muted);
		color: var(--foreground);
	}
	.icon-btn.danger {
		color: var(--destructive);
	}
	.icon-btn.danger:hover {
		background: color-mix(in oklch, var(--destructive) 14%, transparent);
		border-color: color-mix(in oklch, var(--destructive) 30%, transparent);
	}
	.empty {
		font-size: 12.5px;
		color: var(--muted-foreground);
		padding: 0 18px 18px;
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
