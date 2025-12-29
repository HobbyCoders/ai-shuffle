<script lang="ts">
	/**
	 * SubagentCard - Subagent management card for The Deck
	 *
	 * Features:
	 * - Subagent list with search filtering
	 * - Model badge (color-coded)
	 * - Tool count badge
	 * - Group assignment display
	 * - CRUD operations via API
	 * - 3-tab inline editor (Identity, Config, Prompt)
	 * - Tab validation indicators
	 * - Inline delete confirmation banner
	 * - Import/export JSON
	 */
	import BaseCard from './BaseCard.svelte';
	import type { DeckCard } from './types';
	import { api } from '$lib/api/client';
	import { groups } from '$lib/stores/groups';
	import { Search, Plus, Trash2, Download, Upload, X, ArrowLeft, Monitor, User, Settings, Terminal, Check } from 'lucide-svelte';
	import './card-design-system.css';

	interface Subagent {
		id: string;
		name: string;
		description: string;
		prompt: string;
		tools?: string[];
		model?: string;
		is_builtin: boolean;
		created_at: string;
		updated_at: string;
	}

	interface ToolInfo {
		name: string;
		category: string;
		description: string;
		mcp_server?: string;
	}

	interface ToolCategory {
		id: string;
		name: string;
		description: string;
		tools: ToolInfo[];
	}

	interface ToolsResponse {
		categories: ToolCategory[];
		all_tools: ToolInfo[];
	}

	interface Props {
		card: DeckCard;
		onClose: () => void;
		onMaximize: () => void;
		onFocus: () => void;
		onMove: (x: number, y: number) => void;
		onResize: (w: number, h: number) => void;
		onDragEnd?: () => void;
		onResizeEnd?: () => void;
		mobile?: boolean;
	}

	let {
		card,
		onClose,
		onMaximize,
		onFocus,
		onMove,
		onResize,
		onDragEnd,
		onResizeEnd,
		mobile = false
	}: Props = $props();

	// View mode state
	type ViewMode = 'list' | 'editor';
	let viewMode = $state<ViewMode>('list');

	// List state
	let subagents = $state<Subagent[]>([]);
	let loading = $state(true);
	let error = $state<string | null>(null);
	let searchQuery = $state('');

	// Delete state
	let deletingId = $state<string | null>(null);
	let deleteLoading = $state(false);

	// Import state
	let importInput: HTMLInputElement;
	let importing = $state(false);

	// Editor state
	type EditorTab = 'identity' | 'config' | 'prompt';
	let activeTab = $state<EditorTab>('identity');
	let editingSubagent = $state<Subagent | null>(null);
	let isEditMode = $derived(!!editingSubagent);

	// Editor form state
	let formId = $state('');
	let formName = $state('');
	let formDescription = $state('');
	let formPrompt = $state('');
	let formTools = $state<string[]>([]);
	let formModel = $state('');
	let saving = $state(false);

	// Tools state
	let availableTools = $state<ToolsResponse>({ categories: [], all_tools: [] });
	let toolSelectionMode = $state<'all' | 'select'>('all');

	// Model options
	const MODEL_OPTIONS = [
		{ value: '', label: 'Inherit', description: 'Use profile default' },
		{ value: 'haiku', label: 'Haiku', description: 'Fast & cheap' },
		{ value: 'sonnet', label: 'Sonnet', description: 'Balanced' },
		{ value: 'sonnet-1m', label: 'Sonnet 1M', description: 'Extended context' },
		{ value: 'opus', label: 'Opus', description: 'Most capable' }
	];

	// Filtered subagents
	const filteredSubagents = $derived(() => {
		if (!searchQuery.trim()) return subagents;
		const q = searchQuery.toLowerCase();
		return subagents.filter(
			s =>
				s.name.toLowerCase().includes(q) ||
				s.id.toLowerCase().includes(q) ||
				s.description.toLowerCase().includes(q)
		);
	});

	// Validation per tab
	const identityValid = $derived(() => {
		if (!isEditMode && !formId.match(/^[a-z0-9-]+$/)) return false;
		if (!isEditMode && formId.length === 0) return false;
		if (formName.length === 0) return false;
		if (formDescription.length === 0) return false;
		return true;
	});

	const configValid = $derived(() => true);
	const promptValid = $derived(() => formPrompt.length > 0);
	const isValid = $derived(() => identityValid() && configValid() && promptValid());

	const tabStatus = $derived(() => ({
		identity: identityValid(),
		config: configValid(),
		prompt: promptValid()
	}));

	// Load subagents
	async function loadSubagents() {
		loading = true;
		error = null;
		try {
			subagents = await api.get<Subagent[]>('/subagents');
		} catch (e: unknown) {
			const err = e as { detail?: string; message?: string };
			error = err.detail || err.message || 'Failed to load subagents';
		} finally {
			loading = false;
		}
	}

	// Load tools
	async function loadTools() {
		try {
			availableTools = await api.get<ToolsResponse>('/tools');
		} catch (e) {
			console.error('Failed to load tools:', e);
		}
	}

	// Initialize
	$effect(() => {
		loadSubagents();
		loadTools();
	});

	// Get model display info
	function getModelDisplay(model?: string): string {
		const opt = MODEL_OPTIONS.find(o => o.value === model);
		return opt?.label || 'Inherit';
	}

	function getModelBadgeClass(model?: string): string {
		if (!model || model === '') return 'card-badge--inherit';
		return `card-badge--${model}`;
	}

	// Open editor for new subagent
	function handleCreate() {
		editingSubagent = null;
		formId = '';
		formName = '';
		formDescription = '';
		formPrompt = '';
		formTools = [];
		formModel = '';
		toolSelectionMode = 'all';
		activeTab = 'identity';
		viewMode = 'editor';
	}

	// Open editor for existing subagent
	function handleEdit(agent: Subagent) {
		editingSubagent = agent;
		formId = agent.id;
		formName = agent.name;
		formDescription = agent.description;
		formPrompt = agent.prompt;
		formTools = agent.tools || [];
		formModel = agent.model || '';
		toolSelectionMode = agent.tools && agent.tools.length > 0 ? 'select' : 'all';
		activeTab = 'identity';
		viewMode = 'editor';
	}

	// Back to list
	function handleBack() {
		viewMode = 'list';
		editingSubagent = null;
		error = null;
	}

	// Save subagent
	async function handleSave() {
		if (!isValid()) return;

		saving = true;
		error = null;

		try {
			const body: Record<string, unknown> = {
				name: formName,
				description: formDescription,
				prompt: formPrompt,
			};

			if (toolSelectionMode === 'select' && formTools.length > 0) {
				body.tools = formTools;
			}
			if (formModel) {
				body.model = formModel;
			}

			if (isEditMode && editingSubagent) {
				await api.put(`/subagents/${editingSubagent.id}`, body);
			} else {
				body.id = formId;
				await api.post('/subagents', body);
			}

			await loadSubagents();
			handleBack();
		} catch (e: unknown) {
			const err = e as { detail?: string; message?: string };
			error = err.detail || err.message || 'Failed to save subagent';
		} finally {
			saving = false;
		}
	}

	// Delete subagent
	function handleDeleteClick(id: string, e: Event) {
		e.stopPropagation();
		deletingId = id;
	}

	async function confirmDelete() {
		if (!deletingId) return;
		deleteLoading = true;
		try {
			await api.delete(`/subagents/${deletingId}`);
			deletingId = null;
			await loadSubagents();
		} catch (e: unknown) {
			const err = e as { detail?: string; message?: string };
			error = err.detail || err.message || 'Failed to delete subagent';
		} finally {
			deleteLoading = false;
		}
	}

	// Export subagent
	async function exportSubagent(agentId: string, e: Event) {
		e.stopPropagation();
		try {
			const data = await api.get<unknown>(`/export-import/subagents/${agentId}`);
			const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
			const url = URL.createObjectURL(blob);
			const a = document.createElement('a');
			a.href = url;
			a.download = `subagent-${agentId}-${new Date().toISOString().split('T')[0]}.json`;
			document.body.appendChild(a);
			a.click();
			document.body.removeChild(a);
			URL.revokeObjectURL(url);
		} catch (e: unknown) {
			const err = e as { detail?: string; message?: string };
			error = 'Export failed: ' + (err.detail || err.message || 'Unknown error');
		}
	}

	// Import
	function triggerImport() {
		importInput?.click();
	}

	async function handleImport(event: Event) {
		const input = event.target as HTMLInputElement;
		const file = input.files?.[0];
		if (!file) return;
		input.value = '';

		importing = true;
		error = null;

		try {
			const content = await file.text();
			const data = JSON.parse(content);

			if (!data.subagents || data.subagents.length === 0) {
				error = 'No subagents found in file';
				importing = false;
				return;
			}

			const result = await api.post<{ subagents_imported: number; subagents_skipped: number; errors?: string[] }>('/export-import/import/json?overwrite_existing=false', data);

			if (result.subagents_imported > 0) {
				await loadSubagents();
			} else if (result.subagents_skipped > 0) {
				error = 'Subagent already exists. Use a different ID or delete the existing one first.';
			} else if (result.errors?.length) {
				error = 'Import failed: ' + result.errors.join(', ');
			}
		} catch (e: unknown) {
			const err = e as { detail?: string; message?: string };
			error = 'Import failed: ' + (err.detail || err.message || 'Invalid JSON file');
		}

		importing = false;
	}

	// Tool helpers
	function toggleTool(toolName: string) {
		if (formTools.includes(toolName)) {
			formTools = formTools.filter(t => t !== toolName);
		} else {
			formTools = [...formTools, toolName];
		}
	}

	function selectAllInCategory(category: ToolCategory) {
		const toolNames = category.tools.map(t => t.name);
		const allSelected = toolNames.every(name => formTools.includes(name));

		if (allSelected) {
			formTools = formTools.filter(t => !toolNames.includes(t));
		} else {
			const newTools = toolNames.filter(name => !formTools.includes(name));
			formTools = [...formTools, ...newTools];
		}
	}

	function getCategorySelectionState(category: ToolCategory): 'none' | 'partial' | 'all' {
		const toolNames = category.tools.map(t => t.name);
		const selectedCount = toolNames.filter(name => formTools.includes(name)).length;
		if (selectedCount === 0) return 'none';
		if (selectedCount === toolNames.length) return 'all';
		return 'partial';
	}

	function selectAllTools() {
		formTools = availableTools.all_tools.map(t => t.name);
	}

	function clearAllTools() {
		formTools = [];
	}

	// Editor tabs config
	const tabs: { id: EditorTab; label: string; icon: typeof User }[] = [
		{ id: 'identity', label: 'Identity', icon: User },
		{ id: 'config', label: 'Config', icon: Settings },
		{ id: 'prompt', label: 'Prompt', icon: Terminal }
	];
</script>

<!-- Hidden file input for import -->
<input type="file" accept=".json" bind:this={importInput} onchange={handleImport} class="hidden" />

{#snippet content()}
	<div class="card-system subagent-card" class:maximized={card.maximized}>
		{#if viewMode === 'list'}
			<!-- LIST VIEW -->
			<!-- Header -->
			<div class="card-header">
				<div class="card-header-info">
					{#if mobile}
						<span class="card-header-title">Subagents</span>
					{/if}
					<span class="card-header-subtitle">
						{subagents.length} subagent{subagents.length !== 1 ? 's' : ''} configured
					</span>
				</div>
			</div>

			<!-- Search bar (show if > 3 subagents) -->
			{#if subagents.length > 3}
				<div class="card-search-wrapper">
					<div class="card-search-container">
						<Search class="card-search-icon" />
						<input
							type="text"
							bind:value={searchQuery}
							placeholder="Search subagents..."
							class="card-search-input"
						/>
					</div>
				</div>
			{/if}

			<!-- Content -->
			<div class="card-content">
				<div class="centered-content">
				{#if error}
					<div class="card-error-banner">
						<span class="flex-1">{error}</span>
						<button onclick={() => (error = null)}>
							<X class="w-4 h-4" />
						</button>
					</div>
				{/if}

				<!-- Delete confirmation banner -->
				{#if deletingId}
					<div class="card-delete-banner">
						<p>
							Delete <strong>{subagents.find(s => s.id === deletingId)?.name}</strong>? This cannot be undone.
						</p>
						<div class="button-group">
							<button
								onclick={() => (deletingId = null)}
								disabled={deleteLoading}
								class="card-btn-secondary flex-1"
							>
								Cancel
							</button>
							<button
								onclick={confirmDelete}
								disabled={deleteLoading}
								class="card-btn-destructive flex-1"
							>
								{#if deleteLoading}
									<div class="card-spinner card-spinner--small"></div>
								{/if}
								Delete
							</button>
						</div>
					</div>
				{/if}

				{#if loading}
					<div class="loading-container">
						<div class="card-spinner"></div>
					</div>
				{:else if filteredSubagents().length === 0}
					<div class="card-empty-state">
						{#if searchQuery}
							<Search class="card-empty-icon" />
							<p class="card-empty-title">No subagents match "{searchQuery}"</p>
							<button onclick={() => (searchQuery = '')} class="card-empty-action">
								Clear search
							</button>
						{:else}
							<Monitor class="card-empty-icon" />
							<p class="card-empty-title">No subagents configured yet</p>
							<p class="card-empty-description">
								Subagents are specialized AI assistants that can be delegated to for specific tasks.
							</p>
						{/if}
					</div>
				{:else}
					{#each filteredSubagents() as agent (agent.id)}
						<!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
						<div
							class="card-list-item"
							onclick={() => handleEdit(agent)}
						>
							<div class="card-item-icon">
								<Monitor />
							</div>
							<div class="card-item-content">
								<div class="card-item-header">
									<span class="card-item-name">{agent.name}</span>
									<span class="card-badge {getModelBadgeClass(agent.model)}">
										{getModelDisplay(agent.model)}
									</span>
									{#if agent.tools && agent.tools.length > 0}
										<span class="card-badge card-badge--tools">
											{agent.tools.length} tools
										</span>
									{/if}
									{#if $groups.subagents.assignments[agent.id]}
										<span class="card-badge card-badge--primary">
											{$groups.subagents.assignments[agent.id]}
										</span>
									{/if}
								</div>
								<span class="card-item-id">{agent.id}</span>
								<p class="card-item-description">{agent.description}</p>
							</div>
							<div class="card-item-actions">
								<button
									onclick={(e) => exportSubagent(agent.id, e)}
									class="card-action-btn"
									title="Export"
								>
									<Download />
								</button>
								<button
									onclick={(e) => handleDeleteClick(agent.id, e)}
									class="card-action-btn card-action-btn--destructive"
									title="Delete"
								>
									<Trash2 />
								</button>
							</div>
						</div>
					{/each}
				{/if}
				</div>
			</div>

			<!-- Footer -->
			<div class="card-footer">
				<button
					onclick={handleCreate}
					class="card-btn-primary"
				>
					<Plus />
					New Subagent
				</button>
				<button
					onclick={triggerImport}
					disabled={importing}
					class="card-btn-secondary"
					title="Import from file"
				>
					{#if importing}
						<div class="card-spinner card-spinner--small"></div>
					{:else}
						<Upload />
					{/if}
				</button>
			</div>
		{:else}
			<!-- EDITOR VIEW -->
			<!-- Header -->
			<div class="card-header">
				<div class="editor-header-left">
					<button
						onclick={handleBack}
						class="card-back-btn"
						title="Back to list"
					>
						<ArrowLeft />
					</button>
					<div class="card-header-info">
						<span class="card-header-title">
							{isEditMode ? 'Edit Subagent' : 'New Subagent'}
						</span>
						{#if isEditMode && editingSubagent}
							<span class="card-header-subtitle">{editingSubagent.id}</span>
						{/if}
					</div>
				</div>
			</div>

			<!-- Tab Navigation -->
			<div class="card-tabs">
				{#each tabs as tab}
					<button
						onclick={() => activeTab = tab.id}
						class="card-tab"
						class:card-tab--active={activeTab === tab.id}
					>
						<tab.icon />
						<span class="tab-label">{tab.label}</span>
						<span
							class="card-tab-indicator"
							class:card-tab-indicator--valid={tabStatus()[tab.id]}
							class:card-tab-indicator--invalid={!tabStatus()[tab.id]}
						></span>
					</button>
				{/each}
			</div>

			<!-- Tab Content -->
			<div class="card-content">
				<div class="centered-content">
				{#if error}
					<div class="card-error-banner">
						<span class="flex-1">{error}</span>
						<button onclick={() => (error = null)}>
							<X class="w-4 h-4" />
						</button>
					</div>
				{/if}

				<!-- Identity Tab -->
				{#if activeTab === 'identity'}
					<div class="editor-tab-content">
						{#if !isEditMode}
							<div class="card-form-section">
								<label for="agent-id" class="card-form-label">
									ID <span class="required">*</span>
								</label>
								<input
									id="agent-id"
									type="text"
									bind:value={formId}
									placeholder="my-agent-id"
									class="card-form-input card-form-input--mono"
								/>
								<p class="card-form-hint">
									Lowercase letters, numbers, and hyphens only.
								</p>
							</div>
						{/if}

						<div class="card-form-section">
							<label for="agent-name" class="card-form-label">
								Display Name <span class="required">*</span>
							</label>
							<input
								id="agent-name"
								type="text"
								bind:value={formName}
								placeholder="Research Assistant"
								class="card-form-input"
							/>
						</div>

						<div class="card-form-section">
							<label for="agent-description" class="card-form-label">
								Description <span class="required">*</span>
							</label>
							<textarea
								id="agent-description"
								bind:value={formDescription}
								placeholder="Use this agent when you need to research topics..."
								rows={3}
								class="card-form-input card-form-textarea"
							></textarea>
							<p class="card-form-hint">
								Describes when Claude should invoke this subagent.
							</p>
						</div>
					</div>
				{/if}

				<!-- Config Tab -->
				{#if activeTab === 'config'}
					<div class="editor-tab-content">
						<!-- Model Selection -->
						<div class="card-form-section">
							<label class="card-form-label">Model</label>
							<div class="card-selection-grid">
								{#each MODEL_OPTIONS as opt}
									<button
										type="button"
										onclick={() => formModel = opt.value}
										class="card-selection-item"
										class:card-selection-item--selected={formModel === opt.value}
									>
										<span class="card-selection-label">{opt.label}</span>
										<span class="card-selection-desc">{opt.description}</span>
									</button>
								{/each}
							</div>
						</div>

						<!-- Tool Access Mode -->
						<div class="card-form-section">
							<div class="section-header">
								<label class="card-form-label">Tool Access</label>
								{#if toolSelectionMode === 'select'}
									<span class="card-badge card-badge--primary">{formTools.length} selected</span>
								{/if}
							</div>

							<div class="card-mode-selector">
								<button
									type="button"
									onclick={() => { toolSelectionMode = 'all'; formTools = []; }}
									class="card-mode-btn"
									class:card-mode-btn--active={toolSelectionMode === 'all'}
								>
									<Check />
									All Tools
								</button>
								<button
									type="button"
									onclick={() => toolSelectionMode = 'select'}
									class="card-mode-btn"
									class:card-mode-btn--active={toolSelectionMode === 'select'}
								>
									<Settings />
									Custom
								</button>
							</div>

							{#if toolSelectionMode === 'all'}
								<p class="info-box">
									The subagent will have access to all tools available in the profile.
								</p>
							{/if}
						</div>

						<!-- Tool Selection -->
						{#if toolSelectionMode === 'select'}
							<div class="card-form-section">
								<div class="tool-actions">
									<button type="button" onclick={selectAllTools} class="text-action">
										Select all
									</button>
									<span class="separator">|</span>
									<button type="button" onclick={clearAllTools} class="text-action">
										Clear all
									</button>
								</div>

								<div class="tool-categories">
									{#each availableTools.categories as category}
										{#if category.tools.length > 0}
											<div class="tool-category">
												<button
													type="button"
													onclick={() => selectAllInCategory(category)}
													class="category-header"
												>
													<span
														class="category-checkbox"
														class:category-checkbox--partial={getCategorySelectionState(category) === 'partial'}
														class:category-checkbox--checked={getCategorySelectionState(category) !== 'none'}
													>
														{#if getCategorySelectionState(category) !== 'none'}
															<Check class="w-3 h-3" />
														{/if}
													</span>
													<span class="category-name">{category.name}</span>
												</button>

												<div class="card-tool-chips">
													{#each category.tools as tool}
														<button
															type="button"
															onclick={() => toggleTool(tool.name)}
															class="card-tool-chip"
															class:card-tool-chip--selected={formTools.includes(tool.name)}
															title={tool.description}
														>
															{tool.name}
														</button>
													{/each}
												</div>
											</div>
										{/if}
									{/each}
								</div>

								{#if formTools.length === 0}
									<p class="warning-box">
										No tools selected. The subagent won't be able to perform any actions.
									</p>
								{/if}
							</div>
						{/if}
					</div>
				{/if}

				<!-- Prompt Tab -->
				{#if activeTab === 'prompt'}
					<div class="editor-tab-content prompt-tab">
						<div class="prompt-header">
							<label for="agent-prompt" class="card-form-label">
								System Prompt <span class="required">*</span>
							</label>
							<span class="char-count">
								{formPrompt.length} characters
							</span>
						</div>
						<textarea
							id="agent-prompt"
							bind:value={formPrompt}
							placeholder="You are a specialized agent that helps users with..."
							class="card-form-input card-form-textarea prompt-textarea"
						></textarea>
						<p class="card-form-hint">
							Instructions that define the subagent's behavior and capabilities.
						</p>
					</div>
				{/if}
				</div>
			</div>

			<!-- Footer -->
			<div class="card-footer">
				<button
					onclick={handleBack}
					class="card-btn-secondary"
				>
					Cancel
				</button>
				<button
					onclick={handleSave}
					disabled={!isValid() || saving}
					class="card-btn-primary"
				>
					{#if saving}
						<div class="card-spinner card-spinner--small"></div>
						Saving...
					{:else}
						{isEditMode ? 'Save Changes' : 'Create'}
					{/if}
				</button>
			</div>
		{/if}
	</div>
{/snippet}

{#if mobile}
	<!-- Mobile: No BaseCard wrapper, just the content -->
	{@render content()}
{:else}
	<!-- Desktop: Wrap in BaseCard -->
	<BaseCard
		{card}
		{onClose}
		{onMaximize}
		{onFocus}
		{onMove}
		{onResize}
		{onDragEnd}
		{onResizeEnd}
	>
		{@render content()}
	</BaseCard>
{/if}

<style>
	.subagent-card {
		display: flex;
		flex-direction: column;
		height: 100%;
		background: var(--card);
		overflow: hidden;
	}

	/* Maximized state - constrain width and center content */
	.subagent-card.maximized {
		--card-max-width: 900px;
	}

	.subagent-card.maximized :global(.card-tabs),
	.subagent-card.maximized :global(.card-header),
	.subagent-card.maximized .card-search-wrapper,
	.subagent-card.maximized :global(.card-footer) {
		max-width: var(--card-max-width);
		margin-left: auto;
		margin-right: auto;
		width: 100%;
	}

	.subagent-card.maximized .centered-content {
		max-width: var(--card-max-width);
	}

	/* Centered content wrapper - prevents content from stretching too wide on large screens */
	.centered-content {
		width: 100%;
		max-width: 800px;
		margin: 0 auto;
	}

	.hidden {
		display: none;
	}

	.flex-1 {
		flex: 1;
	}

	/* Search wrapper */
	.card-search-wrapper {
		padding: 0 var(--space-4) var(--space-3);
		border-bottom: 1px solid var(--border-subtle);
		background: linear-gradient(180deg, transparent 0%, color-mix(in oklch, var(--muted) 20%, transparent) 100%);
	}

	/* Loading state */
	.loading-container {
		display: flex;
		align-items: center;
		justify-content: center;
		padding: var(--space-8);
	}

	/* Editor header */
	.editor-header-left {
		display: flex;
		align-items: center;
		gap: var(--space-3);
	}

	/* Tab label visibility */
	.tab-label {
		display: none;
	}

	@media (min-width: 400px) {
		.tab-label {
			display: inline;
		}
	}

	/* Editor tab content */
	.editor-tab-content {
		display: flex;
		flex-direction: column;
		gap: var(--space-4);
	}

	.prompt-tab {
		flex: 1;
		display: flex;
		flex-direction: column;
	}

	.prompt-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
	}

	.char-count {
		font-size: var(--text-xs);
		color: var(--text-tertiary);
	}

	.prompt-textarea {
		flex: 1;
		min-height: 200px;
		font-family: var(--card-font-mono);
		font-size: var(--text-sm);
	}

	/* Section header */
	.section-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		margin-bottom: var(--space-2);
	}

	.section-header .card-form-label {
		margin-bottom: 0;
	}

	/* Info and warning boxes */
	.info-box {
		font-size: var(--text-xs);
		padding: var(--space-3);
		background: var(--surface-1);
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-md);
		color: var(--text-primary);
		margin-top: var(--space-3);
	}

	.warning-box {
		font-size: var(--text-xs);
		padding: var(--space-3);
		background: color-mix(in oklch, var(--warning) 10%, transparent);
		border: 1px solid color-mix(in oklch, var(--warning) 25%, transparent);
		border-radius: var(--radius-md);
		color: var(--warning);
		margin-top: var(--space-3);
	}

	/* Tool actions */
	.tool-actions {
		display: flex;
		gap: var(--space-3);
		align-items: center;
		margin-bottom: var(--space-3);
	}

	.text-action {
		background: none;
		border: none;
		color: var(--primary);
		font-size: var(--text-xs);
		font-weight: 500;
		cursor: pointer;
		padding: 0;
	}

	.text-action:hover {
		text-decoration: underline;
	}

	.separator {
		color: var(--text-tertiary);
	}

	/* Tool categories */
	.tool-categories {
		display: flex;
		flex-direction: column;
		gap: var(--space-4);
		max-height: 260px;
		overflow-y: auto;
		padding-right: var(--space-2);
	}

	.tool-category {
		display: flex;
		flex-direction: column;
		gap: var(--space-2);
	}

	.category-header {
		display: flex;
		align-items: center;
		gap: var(--space-2);
		background: none;
		border: none;
		padding: 0;
		cursor: pointer;
		color: var(--text-secondary);
		font-size: var(--text-xs);
		font-weight: 500;
		text-transform: uppercase;
		letter-spacing: var(--tracking-caps);
	}

	.category-header:hover {
		color: var(--text-primary);
	}

	.category-checkbox {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 16px;
		height: 16px;
		background: var(--surface-1);
		border: 1px solid var(--border-default);
		border-radius: var(--radius-sm);
		transition: all var(--transition-fast);
	}

	.category-checkbox--checked {
		background: var(--primary);
		border-color: var(--primary);
		color: var(--primary-foreground);
	}

	.category-checkbox--partial {
		background: color-mix(in oklch, var(--primary) 50%, var(--surface-1));
	}

	.category-name {
		flex: 1;
		text-align: left;
	}

	/* Required indicator */
	.required {
		color: var(--destructive);
	}

	/* Responsive footer */
	.subagent-card :global(.card-footer) {
		flex-wrap: wrap;
	}

	.subagent-card :global(.card-footer .card-btn-primary) {
		flex: 1;
	}
</style>
