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
	import { Bot, Search, Plus, Trash2, Download, Upload, X, ArrowLeft, Monitor, User, Settings, Terminal, Check, Lock, RotateCcw, Zap, Sparkles, Cpu, Wrench, ChevronRight } from 'lucide-svelte';
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
	let expandedSections = $state<Record<string, boolean>>({ sdk: false });

	// Model options
	const MODEL_OPTIONS = [
		{ value: '', label: 'Inherit' },
		{ value: 'haiku', label: 'Haiku' },
		{ value: 'sonnet', label: 'Sonnet' },
		{ value: 'opus', label: 'Opus' },
		{ value: 'opus[1m]', label: 'Opus 1M' }
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
		if (model.startsWith('opus')) return 'card-badge--opus';
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

			// Always include tools — null means "all tools" (inherit)
			if (toolSelectionMode === 'select' && formTools.length > 0) {
				body.tools = formTools;
			} else {
				body.tools = null;
			}
			// Always include model — null means "inherit from session"
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

	// Group assignment
	let creatingGroup = $state(false);
	let newGroupName = $state('');

	function assignToGroup(agentId: string, groupName: string) {
		groups.assignToGroup('subagents', agentId, groupName);
	}

	function removeFromGroup(agentId: string) {
		groups.removeFromGroup('subagents', agentId);
	}

	function startGroupCreate() {
		creatingGroup = true;
		newGroupName = '';
	}

	function confirmGroupCreate(agentId: string) {
		const name = newGroupName.trim();
		if (name) {
			groups.createGroup('subagents', name);
			groups.assignToGroup('subagents', agentId, name);
		}
		creatingGroup = false;
		newGroupName = '';
	}

	function cancelGroupCreate() {
		creatingGroup = false;
		newGroupName = '';
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
	function toggleSection(section: string) {
		expandedSections = { ...expandedSections, [section]: !expandedSections[section] };
	}

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
		{ id: 'config', label: 'Tools', icon: Wrench },
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
		{#snippet footer()}
			<div class="pf-footer">
				<div class="pf-footer-left">
					<button
						onclick={() => showGenerateDialog = true}
						class="fp-btn fp-btn--secondary"
						title="Generate with AI"
					>
						<Sparkles size={14} /> <span class="btn-label">AI Generate</span>
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
							<Upload size={14} /> <span class="btn-label">Import</span>
						{/if}
					</button>
				</div>
				<div class="pf-footer-right">
					<button
						onclick={handleCreate}
						class="fp-btn fp-btn--primary"
					>
						<Plus size={14} /> <span class="btn-label">New Subagent</span>
					</button>
				</div>
			</div>
		{/snippet}

		{#if subagents.length > 3}
			<div class="fp-search-bar">
				<div class="fph-search-wrapper">
					<Search />
					<input
						type="text"
						bind:value={searchQuery}
						placeholder="Search subagents..."
						class="fph-search-input"
					/>
				</div>
			</div>
		{/if}

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
									<span class="fp-meta-chip"><Cpu size={12} /> {getModelDisplay(agent.model)}</span>
									{#if agent.tools && agent.tools.length > 0}
										<span class="fp-meta-dot"></span>
										<span class="fp-meta-chip"><Wrench size={12} /> {agent.tools.length} tools</span>
									{/if}
									{#if agent.is_builtin}
										<span class="fp-meta-dot"></span>
										<span class="fp-meta-chip"><Lock size={12} /> Built-in</span>
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
							<div class="fp-item-actions" onclick={(e) => e.stopPropagation()} onkeydown={(e) => e.stopPropagation()} role="toolbar" tabindex="-1">
								{#if agent.is_builtin && agent.is_modified}
									<button
										onclick={(e) => handleRevert(agent.id, e)}
										class="card-action-btn"
										title="Revert to defaults"
									>
										<RotateCcw size={16} />
									</button>
								{/if}
								<button
									class="card-action-btn"
									title="Export"
									onclick={(e) => exportSubagent(agent.id, e)}
								>
									<Download size={16} />
								</button>
								{#if !agent.is_builtin}
									<button
										class="card-action-btn card-action-btn--destructive"
										title="Delete"
										onclick={(e) => { e.stopPropagation(); deletingId = agent.id; }}
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
		{#snippet footer()}
			<button type="button" class="pf-btn-save" onclick={handleSave} disabled={!isValid() || saving}>
				{#if saving}
					<div class="card-spinner card-spinner--small"></div>
					<span>Saving...</span>
				{:else}
					<Check size={14} />
					<span>{isEditMode ? 'Save Subagent' : 'Create Subagent'}</span>
				{/if}
			</button>
		{/snippet}

		{#snippet headerTabs()}
			<div class="fp-tab-bar">
				{#each tabs as tab}
					<button class="fp-tab" class:active={activeTab === tab.id} onclick={() => (activeTab = tab.id)}>
						<tab.icon size={14} />
						<span class="tab-label">{tab.label}</span>
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

				<div class="card-form-section">
					<label for="agent-model" class="card-form-label">Model</label>
					<select
						id="agent-model"
						class="card-form-input"
						value={formModel}
						onchange={(e) => { formModel = (e.target as HTMLSelectElement).value; }}
					>
						{#each MODEL_OPTIONS as opt}
							<option value={opt.value}>{opt.label}</option>
						{/each}
					</select>
				</div>

				{#if isEditMode && editingSubagent}
					<div class="card-form-section">
						<label class="card-form-label" for="subagent-group">Group</label>
						<div class="pf-group-wrapper">
						<div class="pf-group-row">
							<select
								id="subagent-group"
								class="pf-select"
								value={$groups.subagents.assignments[editingSubagent.id] ?? ''}
								onchange={(e) => {
									const v = (e.target as HTMLSelectElement).value;
									if (editingSubagent) {
										if (v) assignToGroup(editingSubagent.id, v);
										else removeFromGroup(editingSubagent.id);
									}
								}}
							>
								<option value="">No group</option>
								{#each $groups.subagents.groups as group}
									<option value={group.name}>{group.name}</option>
								{/each}
							</select>
							{#if creatingGroup}
								<button type="button" class="pf-btn-new-group" onclick={() => editingSubagent && confirmGroupCreate(editingSubagent.id)} title="Create">
									<Check size={14} />
								</button>
							{:else}
								<button type="button" class="pf-btn-new-group" onclick={startGroupCreate} title="New group">
									<Plus size={14} />
								</button>
								{#if ($groups.subagents.assignments[editingSubagent.id] ?? "")}
									<button type="button" class="pf-btn-new-group pf-btn-new-group--delete" onclick={() => { const g = $groups.subagents.assignments[editingSubagent?.id ?? ""]; if (g) { removeFromGroup(editingSubagent!.id); groups.deleteGroup("subagents", g); } }} title="Delete group">
										<Trash2 size={14} />
									</button>
								{/if}
							{/if}
						</div>
						{#if creatingGroup}
							<!-- svelte-ignore a11y_autofocus -->
							<input
								type="text"
								class="card-form-input pf-group-input"
								placeholder="Group name..."
								bind:value={newGroupName}
								onkeydown={(e) => {
									if (e.key === 'Enter') { e.preventDefault(); editingSubagent && confirmGroupCreate(editingSubagent.id); }
									if (e.key === 'Escape') { e.preventDefault(); cancelGroupCreate(); }
								}}
								autofocus
							/>
						{/if}
					</div>
					</div>
				{/if}
			</div>
		{/if}

		<!-- Tools Tab -->
		{#if activeTab === 'config'}
			<div class="editor-tab-content card-system pf-content">
				<div class="pf-section">
					<div class="pf-section-header">
						<Wrench size={16} />
						<h3>Tool Access</h3>
					</div>
					<p class="pf-desc">Control which tools this subagent can use.</p>
					<select class="pf-select" value={toolSelectionMode} onchange={(e) => { const v = (e.target as HTMLSelectElement).value as 'all' | 'select'; toolSelectionMode = v; if (v === 'all') formTools = []; }}>
						<option value="all">All Tools</option>
						<option value="select">Custom Selection</option>
					</select>
				</div>

				{#if toolSelectionMode === 'select'}
					<div class="pf-section">
						<div class="pf-section-header">
							<Settings size={16} />
							<h3>Tool Categories</h3>
						</div>
						<p class="pf-desc">Select which tools this subagent can access.</p>
						<div class="pf-category-list">
							<button type="button" class="pf-category-card" onclick={() => toggleSection('sdk')}>
								<div class="pf-category-icon">
									<Wrench size={18} />
								</div>
								<div class="pf-category-info">
									<span class="pf-category-name">Built-in Tools</span>
									<span class="pf-category-desc">{availableTools.categories.map(c => c.name).join(', ')}</span>
								</div>
								<span class="pf-category-count">{formTools.length}/{availableTools.all_tools.length}</span>
								<ChevronRight size={16} class="pf-category-chevron" />
							</button>

							{#if expandedSections.sdk}
								<div class="pf-category-expanded">
									<div class="tools-list">
										{#each availableTools.categories as category}
											{#if category.tools.length > 0}
												{@const state = getCategorySelectionState(category)}
												<div class="tool-category">
													<button type="button" class="category-header" onclick={() => selectAllInCategory(category)}>
														<div class="category-checkbox" class:checked={state !== 'none'} class:partial={state === 'partial'}>
															{#if state === 'all'}
																<svg class="check-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="3" d="M5 13l4 4L19 7" /></svg>
															{:else if state === 'partial'}
																<svg class="check-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="3" d="M5 12h14" /></svg>
															{/if}
														</div>
														<span class="category-name">{category.name}</span>
														<span class="category-count">{category.tools.length}</span>
													</button>
													<div class="category-tools">
														{#each category.tools as tool}
															<label class="tool-item">
																<input type="checkbox" checked={formTools.includes(tool.name)} onchange={() => toggleTool(tool.name)} class="tool-checkbox" />
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
								</div>
							{/if}
						</div>
					</div>
				{:else}
					<div class="pf-summary-bar">
						<div class="pf-summary-left">
							<Wrench size={14} />
							<span>{availableTools.all_tools.length} tools enabled</span>
						</div>
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
	.hidden {
		display: none;
	}

	.flex-1 {
		flex: 1;
	}

	/* Loading state */
	.loading-container {
		display: flex;
		align-items: center;
		justify-content: center;
		padding: var(--space-8);
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

	/* Search bar between header and list */
	.fp-search-bar {
		max-width: 640px;
		width: 100%;
		margin: 0 auto;
	}

	.fp-search-bar :global(.fph-search-wrapper) {
		width: 100%;
	}

	.fp-search-bar :global(.fph-search-input) {
		width: 100% !important;
	}

	.btn-label {
		display: inline;
	}

	/* ─── Profile-style content & sections ─── */
	.pf-content {
		padding: 24px 20px;
		display: flex;
		flex-direction: column;
		gap: 28px;
	}

	.pf-section {
		display: flex;
		flex-direction: column;
		gap: 12px;
	}

	.pf-section-header {
		display: flex;
		align-items: center;
		gap: 8px;
		color: var(--text-primary);
	}

	.pf-section-header h3 {
		font-size: var(--text-md, 0.875rem);
		font-weight: 600;
		margin: 0;
		color: var(--text-primary);
	}

	.pf-desc {
		font-size: var(--text-sm, 0.75rem);
		color: var(--text-secondary);
		margin: 0;
	}

	.pf-select {
		width: 100%;
		padding: 10px 14px;
		padding-right: 36px;
		background: var(--surface-1);
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-md, 10px);
		color: var(--text-primary);
		font-size: var(--text-md, 0.875rem);
		cursor: pointer;
		appearance: none;
		background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%23888' stroke-width='2'%3E%3Cpolyline points='6 9 12 15 18 9'/%3E%3C/svg%3E");
		background-repeat: no-repeat;
		background-position: right 12px center;
		transition: border-color 200ms, box-shadow 200ms;
	}

	.pf-select:focus {
		border-color: var(--primary);
		box-shadow: 0 0 0 3px color-mix(in oklch, var(--primary) 15%, transparent);
	}

	/* ─── Category cards ─── */
	.pf-category-list {
		display: flex;
		flex-direction: column;
		gap: 8px;
	}

	.pf-category-card {
		display: flex;
		align-items: center;
		gap: 12px;
		width: 100%;
		padding: 14px;
		background: var(--surface-1);
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-md, 10px);
		cursor: pointer;
		transition: all 200ms;
		color: var(--text-primary);
		font-size: var(--text-md, 0.875rem);
	}

	.pf-category-card:hover {
		border-color: color-mix(in oklch, var(--primary) 30%, transparent);
		background: var(--surface-hover);
	}

	.pf-category-icon {
		width: 36px;
		height: 36px;
		display: flex;
		align-items: center;
		justify-content: center;
		background: color-mix(in oklch, var(--primary) 10%, transparent);
		border-radius: 8px;
		color: var(--primary);
		flex-shrink: 0;
	}

	.pf-category-info {
		flex: 1;
		min-width: 0;
		display: flex;
		flex-direction: column;
		gap: 2px;
	}

	.pf-category-name {
		font-size: var(--text-md, 0.875rem);
		font-weight: 500;
	}

	.pf-category-desc {
		font-size: var(--text-xs, 0.6875rem);
		color: var(--text-secondary);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.pf-category-count {
		font-size: var(--text-md, 0.875rem);
		font-weight: 600;
		color: var(--primary);
		padding: 2px 8px;
		background: color-mix(in oklch, var(--primary) 10%, transparent);
		border-radius: 6px;
	}

	.pf-category-card :global(.pf-category-chevron) {
		color: var(--text-secondary);
		flex-shrink: 0;
	}

	.pf-category-expanded {
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-md, 10px);
		overflow: hidden;
		margin-top: -4px;
	}

	/* ─── Summary bar ─── */
	.pf-summary-bar {
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding: 12px 16px;
		background: color-mix(in oklch, var(--primary) 3%, transparent);
		border: 1px solid color-mix(in oklch, var(--primary) 20%, transparent);
		border-radius: var(--radius-md, 10px);
	}

	.pf-summary-left {
		display: flex;
		align-items: center;
		gap: 6px;
		font-size: var(--text-sm, 0.75rem);
		color: var(--text-secondary);
	}

	.pf-summary-left :global(svg) {
		color: var(--primary);
	}

	/* ─── Footer (matches ProfileCard) ─── */
	.pf-footer {
		display: flex;
		align-items: center;
		justify-content: space-between;
		width: 100%;
	}

	.pf-footer-left {
		display: flex;
		align-items: center;
		gap: 8px;
	}

	.pf-footer-right {
		display: flex;
		align-items: center;
		gap: 10px;
	}

	.pf-btn-save {
		display: flex;
		align-items: center;
		gap: 6px;
		padding: 8px 22px;
		background: linear-gradient(135deg, var(--primary), color-mix(in oklch, var(--primary) 80%, #2FA88D));
		border: none;
		border-radius: var(--radius-md, 10px);
		color: var(--primary-foreground);
		font-size: var(--text-sm, 0.75rem);
		font-weight: 600;
		cursor: pointer;
		transition: all 200ms;
		box-shadow: 0 1px 3px rgba(0,0,0,0.12);
	}

	.pf-btn-save:hover {
		filter: brightness(1.1);
		box-shadow: 0 0 12px color-mix(in oklch, var(--primary) 30%, transparent);
	}

	.pf-btn-save:disabled {
		opacity: 0.5;
		cursor: not-allowed;
		filter: none;
		box-shadow: none;
	}

	/* ─── Group assignment ─── */
	.pf-group-wrapper {
		display: flex;
		flex-direction: column;
		gap: 6px;
	}

	.pf-group-row {
		display: flex;
		gap: 6px;
		align-items: center;
	}

	.pf-group-row .pf-select {
		flex: 1;
	}

	.pf-btn-new-group {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 36px;
		height: 36px;
		flex-shrink: 0;
		background: var(--surface-1);
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-md, 10px);
		color: var(--text-secondary);
		cursor: pointer;
		transition: all 200ms;
	}

	.pf-btn-new-group:hover {
		background: var(--surface-hover);
		color: var(--text-primary);
		border-color: var(--border-default);
	}

	.pf-btn-new-group--delete {
		color: var(--destructive);
	}

	.pf-btn-new-group--delete:hover {
		background: color-mix(in oklch, var(--destructive) 15%, transparent);
		color: var(--destructive);
		border-color: color-mix(in oklch, var(--destructive) 30%, transparent);
	}

	.pf-group-input {
		width: 100%;
	}
</style>
