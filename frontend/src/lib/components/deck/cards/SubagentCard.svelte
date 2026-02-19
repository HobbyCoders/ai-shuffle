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
	import AIGenerateDialog from './AIGenerateDialog.svelte';
	import FeaturePageLayout from '$lib/components/layout/FeaturePageLayout.svelte';
	import { api, type SubagentExport } from '$lib/api/client';
	import { groups } from '$lib/stores/groups';
	import { Bot, Search, Plus, Trash2, Download, Upload, X, ArrowLeft, Monitor, User, Settings, Terminal, Check, Lock, RotateCcw, Zap, Sparkles } from 'lucide-svelte';
	import GroupDropdown from '$lib/components/GroupDropdown.svelte';
	import './card-design-system.css';

	interface Subagent {
		id: string;
		name: string;
		description: string;
		prompt: string;
		tools?: string[];
		model?: string;
		is_builtin: boolean;
		is_modified: boolean;  // Whether built-in subagent has been modified from defaults
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
		mobile?: boolean;
	}

	let {
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
	let showGenerateDialog = $state(false);

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
			// Always include model - use null to clear/inherit
			body.model = formModel || null;

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
		// Don't allow deleting built-in subagents
		const agent = subagents.find(s => s.id === id);
		if (agent?.is_builtin) {
			error = 'Built-in subagents cannot be deleted. You can disable them in profile settings.';
			return;
		}
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

	// Revert built-in subagent to defaults
	let revertingId = $state<string | null>(null);
	let revertLoading = $state(false);

	async function handleRevert(id: string, e: Event) {
		e.stopPropagation();
		revertingId = id;
	}

	async function confirmRevert() {
		if (!revertingId) return;
		revertLoading = true;
		try {
			await api.post(`/subagents/${revertingId}/revert`);
			revertingId = null;
			await loadSubagents();
			// If we're in the editor for this agent, reload the form
			if (editingSubagent?.id === revertingId) {
				const updated = subagents.find(s => s.id === revertingId);
				if (updated) {
					handleEdit(updated);
				}
			}
		} catch (e: unknown) {
			const err = e as { detail?: string; message?: string };
			error = err.detail || err.message || 'Failed to revert subagent';
		} finally {
			revertLoading = false;
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

	/**
	 * Opens the editor view populated with AI-generated subagent data.
	 * User can review/edit the values before saving.
	 */
	function openEditorFromGenerated(data: SubagentExport) {
		editingSubagent = null; // Creating new, not editing existing

		const sub = data.subagent;
		formId = sub.id;
		formName = sub.name;
		formDescription = sub.description;
		formPrompt = sub.prompt;
		formTools = sub.tools || [];
		formModel = sub.model || '';
		toolSelectionMode = sub.tools && sub.tools.length > 0 ? 'select' : 'all';

		viewMode = 'editor';
		activeTab = 'identity';
	}

	// Handle AI-generated subagent
	function handleGeneratedSubagent(data: SubagentExport | unknown) {
		// Close the dialog and open the editor with generated data
		showGenerateDialog = false;
		openEditorFromGenerated(data as SubagentExport);
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

{#if viewMode === 'list'}
	<FeaturePageLayout
		icon={Bot}
		title="Subagents"
		itemCount={subagents.length}
		itemLabel="subagents"
	>
		{#snippet headerActions()}
			{#if subagents.length > 3}
				<div class="fph-search-wrapper">
					<Search />
					<input
						type="text"
						bind:value={searchQuery}
						placeholder="Search subagents..."
						class="fph-search-input"
					/>
				</div>
			{/if}
			<button
				onclick={() => showGenerateDialog = true}
				class="fp-btn fp-btn--secondary"
				title="Generate with AI"
			>
				<Sparkles />
			</button>
			<button
				onclick={triggerImport}
				disabled={importing}
				class="fp-btn fp-btn--secondary"
				title="Import from file"
			>
				{#if importing}
					<div class="card-spinner card-spinner--small"></div>
				{:else}
					<Upload />
				{/if}
			</button>
			<button
				onclick={handleCreate}
				class="fp-btn fp-btn--primary"
			>
				<Plus />
				<span class="btn-label-hide-mobile">New Subagent</span>
			</button>
		{/snippet}

		{#if error}
			<div class="card-error-banner">
				<span class="flex-1">{error}</span>
				<button onclick={() => (error = null)}>
					<X class="w-4 h-4" />
				</button>
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
					<Bot class="card-empty-icon" />
					<p class="card-empty-title">No subagents configured yet</p>
					<p class="card-empty-description">
						Subagents are specialized AI assistants that can be delegated to for specific tasks.
					</p>
				{/if}
			</div>
		{:else}
			<div class="fp-list">
				{#each filteredSubagents() as agent (agent.id)}
					{#if deletingId === agent.id}
						<div class="fp-list-item" style="border-color: color-mix(in oklch, var(--destructive) 40%, transparent);">
							<div class="fp-item-icon" style="background: color-mix(in oklch, var(--destructive) 12%, transparent); color: var(--destructive);">
								<Trash2 />
							</div>
							<div class="fp-item-content">
								<p class="fp-item-name">
									Delete <strong>{agent.name}</strong>? This cannot be undone.
								</p>
								<div style="display: flex; gap: var(--space-2); margin-top: var(--space-2);">
									<button
										onclick={() => (deletingId = null)}
										disabled={deleteLoading}
										class="fp-btn fp-btn--secondary"
									>
										Cancel
									</button>
									<button
										onclick={confirmDelete}
										disabled={deleteLoading}
										class="fp-btn fp-btn--primary"
										style="background: var(--destructive);"
									>
										{#if deleteLoading}
											<div class="card-spinner card-spinner--small"></div>
										{/if}
										Delete
									</button>
								</div>
							</div>
						</div>
					{:else if revertingId === agent.id}
						<div class="fp-list-item" style="border-color: color-mix(in oklch, var(--warning) 40%, transparent);">
							<div class="fp-item-icon" style="background: color-mix(in oklch, var(--warning) 12%, transparent); color: var(--warning);">
								<RotateCcw />
							</div>
							<div class="fp-item-content">
								<p class="fp-item-name">
									Revert <strong>{agent.name}</strong> to original defaults? Your customizations will be lost.
								</p>
								<div style="display: flex; gap: var(--space-2); margin-top: var(--space-2);">
									<button
										onclick={() => (revertingId = null)}
										disabled={revertLoading}
										class="fp-btn fp-btn--secondary"
									>
										Cancel
									</button>
									<button
										onclick={confirmRevert}
										disabled={revertLoading}
										class="fp-btn fp-btn--primary"
									>
										{#if revertLoading}
											<div class="card-spinner card-spinner--small"></div>
										{/if}
										Revert
									</button>
								</div>
							</div>
						</div>
					{:else}
						<div
							class="fp-list-item"
							onclick={() => handleEdit(agent)}
							role="button"
							tabindex="0"
							onkeydown={(e) => e.key === 'Enter' && handleEdit(agent)}
						>
							<div class="fp-item-icon fp-item-icon--primary">
								<Bot />
							</div>
							<div class="fp-item-content">
								<div class="fp-item-header">
									<span class="fp-item-name">{agent.name}</span>
								</div>
								<p class="fp-item-description">{agent.description}</p>
								<div class="fp-item-meta">
									<span class="fp-meta-chip">{agent.id}</span>
									<span class="fp-meta-dot"></span>
									<span class="fp-meta-chip">{getModelDisplay(agent.model)}</span>
									{#if agent.tools && agent.tools.length > 0}
										<span class="fp-meta-dot"></span>
										<span class="fp-meta-chip">{agent.tools.length} tools</span>
									{/if}
									{#if agent.is_builtin}
										<span class="fp-meta-dot"></span>
										<span class="fp-meta-chip">
											<Lock style="width: 10px; height: 10px;" />
											Built-in
										</span>
										{#if agent.is_modified}
											<span class="fp-meta-dot"></span>
											<span class="fp-meta-chip" style="color: var(--warning);">Modified</span>
										{/if}
									{/if}
									{#if $groups.subagents.assignments[agent.id]}
										<span class="fp-meta-dot"></span>
										<span class="fp-meta-chip" style="color: var(--primary);">{$groups.subagents.assignments[agent.id]}</span>
									{/if}
								</div>
							</div>
							<div class="fp-item-actions">
								<GroupDropdown entityType="subagents" entityId={agent.id} />
								{#if agent.is_builtin && agent.is_modified}
									<button
										onclick={(e) => handleRevert(agent.id, e)}
										class="fp-action-btn"
										title="Revert to defaults"
									>
										<RotateCcw />
									</button>
								{/if}
								<button
									onclick={(e) => exportSubagent(agent.id, e)}
									class="fp-action-btn"
									title="Export"
								>
									<Download />
								</button>
								{#if !agent.is_builtin}
									<button
										onclick={(e) => handleDeleteClick(agent.id, e)}
										class="fp-action-btn fp-action-btn--destructive"
										title="Delete"
									>
										<Trash2 />
									</button>
								{/if}
							</div>
						</div>
					{/if}
				{/each}
			</div>
		{/if}

		<!-- AI Generate Dialog -->
		<AIGenerateDialog
			type="subagent"
			isOpen={showGenerateDialog}
			onClose={() => showGenerateDialog = false}
			onGenerated={handleGeneratedSubagent}
		/>
	</FeaturePageLayout>
{:else}
	<!-- EDITOR VIEW -->
	<FeaturePageLayout
		icon={Bot}
		title="Subagents"
		breadcrumbs={[
			{ label: 'Subagents', onclick: handleBack },
			{ label: isEditMode ? (editingSubagent?.name || 'Edit') : 'New Subagent' }
		]}
	>
		{#snippet headerActions()}
			<button
				onclick={handleBack}
				class="fp-btn fp-btn--secondary"
			>
				Cancel
			</button>
			<button
				onclick={handleSave}
				disabled={!isValid() || saving}
				class="fp-btn fp-btn--primary"
			>
				{#if saving}
					<div class="card-spinner card-spinner--small"></div>
					Saving...
				{:else}
					{isEditMode ? 'Save Changes' : 'Create'}
				{/if}
			</button>
		{/snippet}

		{#snippet headerTabs()}
			<div class="fp-tab-bar">
				{#each tabs as tab}
					<button
						onclick={() => activeTab = tab.id}
						class="fp-tab"
						class:active={activeTab === tab.id}
					>
						<tab.icon />
						{tab.label}
					</button>
				{/each}
			</div>
		{/snippet}

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
					<span class="card-form-label">Model — {#if formModel === ''}use profile default{:else if formModel === 'haiku'}fastest and most compact{:else if formModel === 'sonnet'}balanced performance{:else if formModel === 'sonnet-1m'}extended context (1M tokens){:else if formModel === 'opus'}most capable{/if}</span>
					<div class="card-mode-selector">
						<button
							type="button"
							class="card-mode-btn"
							class:card-mode-btn--active={formModel === ''}
							onclick={() => formModel = ''}
						>
							Inherit
						</button>
						<button
							type="button"
							class="card-mode-btn"
							class:card-mode-btn--active={formModel === 'haiku'}
							onclick={() => formModel = 'haiku'}
						>
							Haiku
						</button>
						<button
							type="button"
							class="card-mode-btn"
							class:card-mode-btn--active={formModel === 'sonnet'}
							onclick={() => formModel = 'sonnet'}
						>
							Sonnet
						</button>
						<button
							type="button"
							class="card-mode-btn"
							class:card-mode-btn--active={formModel === 'sonnet-1m'}
							onclick={() => formModel = 'sonnet-1m'}
						>
							Sonnet 1M
						</button>
						<button
							type="button"
							class="card-mode-btn"
							class:card-mode-btn--active={formModel === 'opus'}
							onclick={() => formModel = 'opus'}
						>
							Opus
						</button>
					</div>
				</div>

				<!-- Tool Access -->
				<div class="card-form-section">
					<div class="section-header">
						<h3 class="card-form-title">Tool Access</h3>
						<span class="card-badge card-badge--primary">{availableTools.all_tools.length} available</span>
					</div>

					<div class="card-mode-selector">
						<button
							type="button"
							onclick={() => { toolSelectionMode = 'all'; formTools = []; }}
							class="card-mode-btn"
							class:card-mode-btn--active={toolSelectionMode === 'all'}
						>
							All Tools
						</button>
						<button
							type="button"
							onclick={() => toolSelectionMode = 'select'}
							class="card-mode-btn"
							class:card-mode-btn--active={toolSelectionMode === 'select'}
						>
							Custom
						</button>
					</div>

					<p class="card-form-hint">
						{#if toolSelectionMode === 'all'}
							Subagent can use all tools available in the profile.
						{:else}
							Subagent can ONLY use selected tools.
						{/if}
					</p>

					{#if toolSelectionMode === 'select'}
						<div class="tools-list">
							{#each availableTools.categories as category}
								{#if category.tools.length > 0}
									{@const state = getCategorySelectionState(category)}
									<div class="tool-category">
										<button
											type="button"
											class="category-header"
											onclick={() => selectAllInCategory(category)}
										>
											<div
												class="category-checkbox"
												class:checked={state !== 'none'}
												class:partial={state === 'partial'}
											>
												{#if state === 'all'}
													<svg class="check-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24">
														<path stroke-linecap="round" stroke-linejoin="round" stroke-width="3" d="M5 13l4 4L19 7" />
													</svg>
												{:else if state === 'partial'}
													<svg class="check-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24">
														<path stroke-linecap="round" stroke-linejoin="round" stroke-width="3" d="M5 12h14" />
													</svg>
												{/if}
											</div>
											<span class="category-name">{category.name}</span>
											<span class="category-count">{category.tools.length}</span>
										</button>
										<div class="category-tools">
											{#each category.tools as tool}
												<label class="tool-item">
													<input
														type="checkbox"
														checked={formTools.includes(tool.name)}
														onchange={() => toggleTool(tool.name)}
														class="tool-checkbox"
													/>
													<span class="tool-name" title={tool.description}>{tool.name}</span>
												</label>
											{/each}
										</div>
									</div>
								{/if}
							{/each}
						</div>

						<div class="selection-count">
							{formTools.length} tool{formTools.length !== 1 ? 's' : ''} selected
						</div>
					{/if}
				</div>
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

		<!-- AI Generate Dialog -->
		<AIGenerateDialog
			type="subagent"
			isOpen={showGenerateDialog}
			onClose={() => showGenerateDialog = false}
			onGenerated={handleGeneratedSubagent}
		/>
	</FeaturePageLayout>
{/if}

<style>
	.subagent-card {
		display: flex;
		flex-direction: column;
		height: 100%;
		background: var(--card);
		overflow: hidden;
	}

	/* Centered content wrapper */
	.centered-content {
		width: 100%;
		display: flex;
		flex-direction: column;
		gap: var(--space-4, 16px);
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

	/* Tools List */
	.tools-list {
		max-height: 256px;
		overflow-y: auto;
		padding-right: var(--space-2, 8px);
		margin-top: var(--space-3, 12px);
	}

	.tool-category {
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-md, 10px);
		overflow: hidden;
		margin-bottom: var(--space-2, 8px);
	}

	.category-header {
		display: flex;
		align-items: center;
		gap: var(--space-3, 12px);
		width: 100%;
		padding: var(--space-3, 12px);
		background: var(--surface-1);
		border: none;
		color: var(--text-primary);
		font-size: var(--text-md, 0.875rem);
		cursor: pointer;
		transition: all var(--transition-smooth, 200ms);
	}

	.category-header:hover {
		background: var(--surface-hover);
	}

	.category-checkbox {
		width: 16px;
		height: 16px;
		border-radius: 4px;
		display: flex;
		align-items: center;
		justify-content: center;
		background: var(--surface-1);
		border: 1px solid var(--border-default);
		transition: all var(--transition-fast, 100ms);
	}

	.category-checkbox.checked {
		background: var(--primary);
		border-color: var(--primary);
	}

	.category-checkbox.partial {
		background: var(--primary);
		border-color: var(--primary);
	}

	.check-icon {
		width: 12px;
		height: 12px;
		color: var(--primary-foreground);
	}

	.category-name {
		flex: 1;
		text-align: left;
		font-weight: 500;
	}

	.category-count {
		font-size: var(--text-sm, 0.75rem);
		color: var(--text-secondary);
	}

	.category-tools {
		display: grid;
		grid-template-columns: repeat(2, 1fr);
		gap: 4px;
		padding: var(--space-2, 8px);
		background: var(--surface-0);
	}

	@media (max-width: 480px) {
		.category-tools {
			grid-template-columns: 1fr;
		}
	}

	.tool-item {
		display: flex;
		align-items: center;
		gap: var(--space-2, 8px);
		padding: 6px var(--space-2, 8px);
		border-radius: var(--radius-sm, 6px);
		cursor: pointer;
		transition: background var(--transition-fast, 100ms);
	}

	.tool-item:hover {
		background: var(--surface-1);
	}

	.tool-checkbox {
		width: 14px;
		height: 14px;
		flex-shrink: 0;
	}

	.tool-name {
		font-size: var(--text-base, 0.8125rem);
		color: var(--text-primary);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.selection-count {
		font-size: var(--text-sm, 0.75rem);
		color: var(--text-secondary);
		margin-top: var(--space-3, 12px);
		padding-top: var(--space-3, 12px);
		border-top: 1px solid var(--border-subtle);
	}

	/* Required indicator */
	.required {
		color: var(--destructive);
	}

	/* Responsive footer */
	.subagent-card :global(.card-footer) {
		flex-wrap: wrap;
	}

	/* Hide button label text on mobile to save space */
	@media (max-width: 639px) {
		.btn-label-hide-mobile {
			display: none;
		}
	}
</style>
