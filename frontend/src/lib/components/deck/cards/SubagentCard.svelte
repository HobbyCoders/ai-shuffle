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
		{ value: '', label: 'Inherit', description: 'Use profile default', color: 'bg-muted text-muted-foreground' },
		{ value: 'haiku', label: 'Haiku', description: 'Fast & cheap', color: 'bg-emerald-500/10 text-emerald-500' },
		{ value: 'sonnet', label: 'Sonnet', description: 'Balanced', color: 'bg-blue-500/10 text-blue-500' },
		{ value: 'sonnet-1m', label: 'Sonnet 1M', description: 'Extended context', color: 'bg-violet-500/10 text-violet-500' },
		{ value: 'opus', label: 'Opus', description: 'Most capable', color: 'bg-amber-500/10 text-amber-500' }
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

	function getModelColor(model?: string): string {
		const opt = MODEL_OPTIONS.find(o => o.value === model);
		return opt?.color || 'bg-muted text-muted-foreground';
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
	<div class="subagent-card-content flex flex-col h-full bg-card">
		{#if viewMode === 'list'}
			<!-- LIST VIEW -->
			<!-- Header -->
			<div class="px-4 py-3 border-b border-border bg-gradient-to-r from-primary/5 to-transparent">
				<div class="flex items-center justify-between">
					<div>
						{#if !mobile}
							<p class="text-xs text-muted-foreground">
								{subagents.length} subagent{subagents.length !== 1 ? 's' : ''} configured
							</p>
						{:else}
							<h2 class="text-lg font-semibold text-foreground">Subagents</h2>
							<p class="text-xs text-muted-foreground">
								{subagents.length} agent{subagents.length !== 1 ? 's' : ''}
							</p>
						{/if}
					</div>
				</div>

				<!-- Search bar (show if > 3 subagents) -->
				{#if subagents.length > 3}
					<div class="mt-3 relative">
						<Search class="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground" />
						<input
							type="text"
							bind:value={searchQuery}
							placeholder="Search subagents..."
							class="search-input w-full pl-10 pr-4 py-2.5 border rounded-xl text-sm focus:outline-none"
						/>
					</div>
				{/if}
			</div>

			<!-- Content -->
			<div class="flex-1 overflow-y-auto p-4">
				{#if error}
					<div class="error-banner mb-4 p-3 rounded-xl text-sm flex items-center gap-2">
						<span class="flex-1">{error}</span>
						<button onclick={() => (error = null)} class="hover:opacity-70 transition-opacity">
							<X class="w-4 h-4" />
						</button>
					</div>
				{/if}

				<!-- Delete confirmation banner -->
				{#if deletingId}
					<div class="delete-confirm-banner mb-4 p-4 rounded-xl">
						<p class="text-sm text-foreground mb-3">
							Delete <strong>{subagents.find(s => s.id === deletingId)?.name}</strong>? This cannot be undone.
						</p>
						<div class="flex gap-2">
							<button
								onclick={() => (deletingId = null)}
								disabled={deleteLoading}
								class="btn-secondary flex-1 px-3 py-2.5 rounded-lg text-sm"
							>
								Cancel
							</button>
							<button
								onclick={confirmDelete}
								disabled={deleteLoading}
								class="btn-destructive flex-1 px-3 py-2.5 rounded-lg text-sm flex items-center justify-center gap-2"
							>
								{#if deleteLoading}
									<svg class="w-4 h-4 animate-spin" fill="none" viewBox="0 0 24 24">
										<circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
										<path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
									</svg>
								{/if}
								Delete
							</button>
						</div>
					</div>
				{/if}

				{#if loading}
					<div class="flex items-center justify-center py-12">
						<svg class="w-8 h-8 animate-spin text-primary" fill="none" viewBox="0 0 24 24">
							<circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
							<path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
						</svg>
					</div>
				{:else if filteredSubagents().length === 0}
					<div class="text-center py-8">
						{#if searchQuery}
							<Search class="w-12 h-12 mx-auto text-muted-foreground/50 mb-3" />
							<p class="text-sm text-muted-foreground">No subagents match "{searchQuery}"</p>
							<button onclick={() => (searchQuery = '')} class="mt-2 text-xs text-primary hover:underline">
								Clear search
							</button>
						{:else}
							<Monitor class="w-12 h-12 mx-auto text-muted-foreground/50 mb-3" />
							<p class="text-sm text-muted-foreground mb-2">No subagents configured yet</p>
							<p class="text-xs text-muted-foreground/70 max-w-xs mx-auto">
								Subagents are specialized AI assistants that can be delegated to for specific tasks.
							</p>
						{/if}
					</div>
				{:else}
					<div class="space-y-3">
						{#each filteredSubagents() as agent (agent.id)}
							<!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
							<div
								class="agent-list-item p-4 rounded-xl group cursor-pointer"
								onclick={() => handleEdit(agent)}
							>
								<div class="flex items-start gap-3">
									<div class="agent-icon w-10 h-10 rounded-lg flex items-center justify-center flex-shrink-0">
										<Monitor class="w-5 h-5 text-primary" />
									</div>
									<div class="flex-1 min-w-0">
										<div class="flex items-center gap-2 flex-wrap">
											<span class="font-semibold text-foreground text-sm">{agent.name}</span>
											<span class="model-badge model-badge-{agent.model || 'inherit'} text-[10px]">
												{getModelDisplay(agent.model)}
											</span>
											{#if agent.tools && agent.tools.length > 0}
												<span class="tools-badge text-[10px]">
													{agent.tools.length} tools
												</span>
											{/if}
											{#if $groups.subagents.assignments[agent.id]}
												<span class="group-badge text-[10px]">
													{$groups.subagents.assignments[agent.id]}
												</span>
											{/if}
										</div>
										<p class="text-xs text-muted-foreground font-mono mt-1">{agent.id}</p>
										<p class="text-xs text-muted-foreground mt-1.5 line-clamp-2">{agent.description}</p>
									</div>
									<div class="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
										<button
											onclick={(e) => exportSubagent(agent.id, e)}
											class="action-btn text-muted-foreground hover:text-foreground"
											title="Export"
										>
											<Download class="w-4 h-4" />
										</button>
										<button
											onclick={(e) => handleDeleteClick(agent.id, e)}
											class="action-btn action-btn-delete text-muted-foreground"
											title="Delete"
										>
											<Trash2 class="w-4 h-4" />
										</button>
									</div>
								</div>
							</div>
						{/each}
					</div>
				{/if}
			</div>

			<!-- Footer -->
			<div class="footer-section px-4 py-4">
				<div class="flex gap-3">
					<button
						onclick={handleCreate}
						class="btn-primary flex-1 flex items-center justify-center gap-2 px-4 py-2.5 rounded-xl"
					>
						<Plus class="w-4 h-4" />
						New Subagent
					</button>
					<button
						onclick={triggerImport}
						disabled={importing}
						class="btn-secondary px-4 py-2.5 rounded-xl flex items-center gap-2"
						title="Import from file"
					>
						{#if importing}
							<svg class="w-4 h-4 animate-spin" fill="none" viewBox="0 0 24 24">
								<circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
								<path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
							</svg>
						{:else}
							<Upload class="w-4 h-4" />
						{/if}
					</button>
				</div>
			</div>
		{:else}
			<!-- EDITOR VIEW -->
			<!-- Header -->
			<div class="px-4 py-3 border-b border-border bg-gradient-to-r from-primary/5 to-transparent">
				<div class="flex items-center gap-3">
					<button
						onclick={handleBack}
						class="p-2 hover:bg-muted rounded-lg transition-colors"
						title="Back to list"
					>
						<ArrowLeft class="w-4 h-4 text-muted-foreground" />
					</button>
					<div>
						<h3 class="text-sm font-semibold text-foreground">
							{isEditMode ? 'Edit Subagent' : 'New Subagent'}
						</h3>
						{#if isEditMode && editingSubagent}
							<p class="text-xs text-muted-foreground font-mono">{editingSubagent.id}</p>
						{/if}
					</div>
				</div>
			</div>

			<!-- Tab Navigation -->
			<div class="tab-nav flex border-b border-border">
				{#each tabs as tab}
					<button
						onclick={() => activeTab = tab.id}
						class="tab-button flex-1 flex items-center justify-center gap-2 px-4 py-3 text-sm font-medium transition-all relative
							{activeTab === tab.id
								? 'text-primary active'
								: 'text-muted-foreground hover:text-foreground'}"
					>
						<tab.icon class="w-4 h-4" />
						<span>{tab.label}</span>

						<!-- Validation indicator -->
						{#if !tabStatus()[tab.id]}
							<span class="validation-dot-invalid w-2 h-2 rounded-full"></span>
						{:else}
							<span class="validation-dot-valid w-2 h-2 rounded-full"></span>
						{/if}

						<!-- Active indicator -->
						{#if activeTab === tab.id}
							<div class="absolute bottom-0 left-0 right-0 h-0.5 bg-primary"></div>
						{/if}
					</button>
				{/each}
			</div>

			<!-- Tab Content -->
			<div class="flex-1 overflow-y-auto p-4">
				{#if error}
					<div class="error-banner mb-4 p-3 rounded-xl text-sm flex items-center gap-2">
						<span class="flex-1">{error}</span>
						<button onclick={() => (error = null)} class="hover:opacity-70 transition-opacity">
							<X class="w-4 h-4" />
						</button>
					</div>
				{/if}

				<!-- Identity Tab -->
				{#if activeTab === 'identity'}
					<div class="space-y-4">
						{#if !isEditMode}
							<div class="space-y-2">
								<label for="agent-id" class="block text-sm font-medium text-foreground">
									ID <span class="text-destructive">*</span>
								</label>
								<input
									id="agent-id"
									type="text"
									bind:value={formId}
									placeholder="my-agent-id"
									class="form-input w-full px-4 py-2.5 rounded-xl"
								/>
								<p class="text-xs text-muted-foreground">
									Lowercase letters, numbers, and hyphens only.
								</p>
							</div>
						{/if}

						<div class="space-y-2">
							<label for="agent-name" class="block text-sm font-medium text-foreground">
								Display Name <span class="text-destructive">*</span>
							</label>
							<input
								id="agent-name"
								type="text"
								bind:value={formName}
								placeholder="Research Assistant"
								class="form-input w-full px-4 py-2.5 rounded-xl"
							/>
						</div>

						<div class="space-y-2">
							<label for="agent-description" class="block text-sm font-medium text-foreground">
								Description <span class="text-destructive">*</span>
							</label>
							<textarea
								id="agent-description"
								bind:value={formDescription}
								placeholder="Use this agent when you need to research topics..."
								rows={3}
								class="form-input w-full px-4 py-2.5 rounded-xl resize-none"
							></textarea>
							<p class="text-xs text-muted-foreground">
								Describes when Claude should invoke this subagent.
							</p>
						</div>
					</div>
				{/if}

				<!-- Config Tab -->
				{#if activeTab === 'config'}
					<div class="space-y-6">
						<!-- Model Selection -->
						<div class="space-y-3">
							<label class="block text-sm font-medium text-foreground">Model</label>
							<div class="grid grid-cols-2 gap-3">
								{#each MODEL_OPTIONS as opt}
									<button
										type="button"
										onclick={() => formModel = opt.value}
										class="model-option flex flex-col items-center gap-1 p-3 rounded-xl
											{formModel === opt.value ? 'selected' : ''}"
									>
										<span class="text-sm font-semibold">{opt.label}</span>
										<span class="text-[10px] text-muted-foreground">{opt.description}</span>
									</button>
								{/each}
							</div>
						</div>

						<!-- Tool Access Mode -->
						<div class="space-y-3">
							<div class="flex items-center justify-between">
								<label class="block text-sm font-medium text-foreground">Tool Access</label>
								{#if toolSelectionMode === 'select'}
									<span class="text-xs text-primary font-medium">{formTools.length} selected</span>
								{/if}
							</div>

							<div class="flex gap-3">
								<button
									type="button"
									onclick={() => { toolSelectionMode = 'all'; formTools = []; }}
									class="tool-access-btn flex-1 flex items-center justify-center gap-2 px-4 py-2.5 rounded-xl
										{toolSelectionMode === 'all' ? 'selected' : ''}"
								>
									<Check class="w-4 h-4" />
									All Tools
								</button>
								<button
									type="button"
									onclick={() => toolSelectionMode = 'select'}
									class="tool-access-btn flex-1 flex items-center justify-center gap-2 px-4 py-2.5 rounded-xl
										{toolSelectionMode === 'select' ? 'selected' : ''}"
								>
									<Settings class="w-4 h-4" />
									Custom
								</button>
							</div>

							{#if toolSelectionMode === 'all'}
								<p class="info-box text-xs rounded-lg p-3">
									The subagent will have access to all tools available in the profile.
								</p>
							{/if}
						</div>

						<!-- Tool Selection -->
						{#if toolSelectionMode === 'select'}
							<div class="space-y-4">
								<div class="flex gap-3 text-xs">
									<button type="button" onclick={selectAllTools} class="text-primary hover:underline font-medium">
										Select all
									</button>
									<span class="text-muted-foreground">|</span>
									<button type="button" onclick={clearAllTools} class="text-primary hover:underline font-medium">
										Clear all
									</button>
								</div>

								<div class="space-y-4 max-h-64 overflow-y-auto pr-2">
									{#each availableTools.categories as category}
										{#if category.tools.length > 0}
											<div class="space-y-2">
												<button
													type="button"
													onclick={() => selectAllInCategory(category)}
													class="flex items-center gap-2 text-xs font-medium text-muted-foreground hover:text-foreground transition-colors"
												>
													<span class="category-checkbox w-4 h-4 rounded flex items-center justify-center
														{getCategorySelectionState(category) === 'all'
															? 'checked'
															: getCategorySelectionState(category) === 'partial'
																? 'partial'
																: ''}">
														{#if getCategorySelectionState(category) !== 'none'}
															<Check class="w-3 h-3 text-primary-foreground" />
														{/if}
													</span>
													<span class="uppercase tracking-wider">{category.name}</span>
												</button>

												<div class="flex flex-wrap gap-2">
													{#each category.tools as tool}
														<button
															type="button"
															onclick={() => toggleTool(tool.name)}
															class="tool-chip px-3 py-1.5 text-xs rounded-lg
																{formTools.includes(tool.name) ? 'selected' : ''}"
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
									<p class="warning-box text-xs rounded-lg p-3">
										No tools selected. The subagent won't be able to perform any actions.
									</p>
								{/if}
							</div>
						{/if}
					</div>
				{/if}

				<!-- Prompt Tab -->
				{#if activeTab === 'prompt'}
					<div class="h-full flex flex-col space-y-2">
						<div class="flex items-center justify-between">
							<label for="agent-prompt" class="block text-sm font-medium text-foreground">
								System Prompt <span class="text-destructive">*</span>
							</label>
							<span class="text-xs text-muted-foreground">
								{formPrompt.length} characters
							</span>
						</div>
						<textarea
							id="agent-prompt"
							bind:value={formPrompt}
							placeholder="You are a specialized agent that helps users with..."
							class="form-input flex-1 min-h-[200px] w-full px-4 py-3 rounded-xl font-mono text-sm resize-none"
						></textarea>
						<p class="text-xs text-muted-foreground">
							Instructions that define the subagent's behavior and capabilities.
						</p>
					</div>
				{/if}
			</div>

			<!-- Footer -->
			<div class="footer-section px-4 py-4">
				<div class="flex gap-3">
					<button
						onclick={handleBack}
						class="btn-secondary flex-1 px-4 py-2.5 rounded-xl"
					>
						Cancel
					</button>
					<button
						onclick={handleSave}
						disabled={!isValid() || saving}
						class="btn-primary flex-1 px-4 py-2.5 rounded-xl disabled:opacity-50 flex items-center justify-center gap-2"
					>
						{#if saving}
							<svg class="w-4 h-4 animate-spin" fill="none" viewBox="0 0 24 24">
								<circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
								<path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
							</svg>
							Saving...
						{:else}
							{isEditMode ? 'Save Changes' : 'Create'}
						{/if}
					</button>
				</div>
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
	.subagent-card-content {
		background: var(--card);
	}

	.line-clamp-2 {
		display: -webkit-box;
		-webkit-line-clamp: 2;
		-webkit-box-orient: vertical;
		overflow: hidden;
	}

	/* ============================================
	   IMPROVED THEMING
	   ============================================ */

	/* Card header styling */
	.subagent-card-content :global(.header-section) {
		background: linear-gradient(to right, color-mix(in oklch, var(--primary) 8%, transparent), transparent);
	}

	/* Search input - improved readability */
	.subagent-card-content :global(.search-input) {
		background: var(--input);
		border: 1px solid var(--border);
		color: var(--foreground);
		transition: all 0.15s ease;
		font-size: 0.875rem;
	}

	.subagent-card-content :global(.search-input:hover) {
		border-color: color-mix(in oklch, var(--border) 100%, var(--primary) 30%);
	}

	.subagent-card-content :global(.search-input:focus) {
		background: var(--card);
		border-color: var(--ring);
		box-shadow: 0 0 0 3px color-mix(in oklch, var(--ring) 20%, transparent);
	}

	.subagent-card-content :global(.search-input::placeholder) {
		color: var(--muted-foreground);
	}

	/* List item cards - improved contrast and readability */
	.subagent-card-content :global(.agent-list-item) {
		background: var(--accent);
		border: 1px solid var(--border);
		box-shadow: var(--shadow-s);
		transition: all 0.15s ease;
	}

	.subagent-card-content :global(.agent-list-item:hover) {
		background: color-mix(in oklch, var(--accent) 80%, var(--primary) 10%);
		box-shadow: var(--shadow-m);
		transform: translateY(-1px);
		border-color: color-mix(in oklch, var(--primary) 40%, var(--border));
	}

	.subagent-card-content :global(.agent-list-item:focus-visible) {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
	}

	/* Agent icon container */
	.subagent-card-content :global(.agent-icon) {
		background: color-mix(in oklch, var(--primary) 15%, transparent);
		box-shadow: var(--shadow-s);
		border: 1px solid color-mix(in oklch, var(--primary) 20%, transparent);
	}

	/* Model badges */
	.subagent-card-content :global(.model-badge) {
		font-weight: 600;
		padding: 3px 8px;
		border-radius: 6px;
		box-shadow: var(--shadow-s);
	}

	.subagent-card-content :global(.model-badge-inherit) {
		background: var(--muted);
		color: var(--muted-foreground);
		border: 1px solid var(--border);
	}

	.subagent-card-content :global(.model-badge-haiku) {
		background: color-mix(in oklch, oklch(0.65 0.18 145) 15%, transparent);
		color: oklch(0.65 0.18 145);
		border: 1px solid color-mix(in oklch, oklch(0.65 0.18 145) 30%, transparent);
	}

	.subagent-card-content :global(.model-badge-sonnet) {
		background: color-mix(in oklch, var(--info) 15%, transparent);
		color: var(--info);
		border: 1px solid color-mix(in oklch, var(--info) 30%, transparent);
	}

	.subagent-card-content :global(.model-badge-sonnet-1m) {
		background: color-mix(in oklch, oklch(0.65 0.18 300) 15%, transparent);
		color: oklch(0.7 0.18 300);
		border: 1px solid color-mix(in oklch, oklch(0.65 0.18 300) 30%, transparent);
	}

	.subagent-card-content :global(.model-badge-opus) {
		background: color-mix(in oklch, var(--warning) 15%, transparent);
		color: var(--warning);
		border: 1px solid color-mix(in oklch, var(--warning) 30%, transparent);
	}

	/* Tool count badge */
	.subagent-card-content :global(.tools-badge) {
		background: var(--muted);
		color: var(--foreground);
		border: 1px solid var(--border);
		font-weight: 500;
		padding: 3px 8px;
		border-radius: 6px;
	}

	/* Group assignment badge */
	.subagent-card-content :global(.group-badge) {
		background: color-mix(in oklch, var(--primary) 15%, transparent);
		color: var(--primary);
		border: 1px solid color-mix(in oklch, var(--primary) 30%, transparent);
		font-weight: 500;
		padding: 3px 8px;
		border-radius: 6px;
	}

	/* Action buttons on list items */
	.subagent-card-content :global(.action-btn) {
		padding: 8px;
		border-radius: 8px;
		transition: all 0.15s ease;
	}

	.subagent-card-content :global(.action-btn:hover) {
		background: var(--accent);
	}

	.subagent-card-content :global(.action-btn-delete:hover) {
		background: color-mix(in oklch, var(--destructive) 15%, transparent);
		color: var(--destructive);
	}

	/* Error messages */
	.subagent-card-content :global(.error-banner) {
		background: color-mix(in oklch, var(--destructive) 12%, transparent);
		border: 1px solid color-mix(in oklch, var(--destructive) 25%, transparent);
		color: var(--destructive);
		box-shadow: var(--shadow-s);
	}

	/* Delete confirmation banner */
	.subagent-card-content :global(.delete-confirm-banner) {
		background: color-mix(in oklch, var(--destructive) 10%, transparent);
		border: 1px solid color-mix(in oklch, var(--destructive) 25%, transparent);
		box-shadow: var(--shadow-m);
	}

	/* Primary button */
	.subagent-card-content :global(.btn-primary) {
		background: var(--primary);
		color: var(--primary-foreground);
		font-weight: 600;
		box-shadow: var(--shadow-m);
		transition: all 0.15s ease;
	}

	.subagent-card-content :global(.btn-primary:hover) {
		filter: brightness(1.1);
		box-shadow: var(--shadow-l);
		transform: translateY(-1px);
	}

	.subagent-card-content :global(.btn-primary:focus-visible) {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
	}

	/* Secondary button */
	.subagent-card-content :global(.btn-secondary) {
		background: var(--muted);
		color: var(--foreground);
		border: 1px solid var(--border);
		box-shadow: var(--shadow-s);
		transition: all 0.15s ease;
	}

	.subagent-card-content :global(.btn-secondary:hover) {
		background: var(--accent);
		box-shadow: var(--shadow-m);
	}

	.subagent-card-content :global(.btn-secondary:focus-visible) {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
	}

	/* Destructive button */
	.subagent-card-content :global(.btn-destructive) {
		background: var(--destructive);
		color: var(--destructive-foreground);
		font-weight: 600;
		box-shadow: var(--shadow-m);
		transition: all 0.15s ease;
	}

	.subagent-card-content :global(.btn-destructive:hover) {
		filter: brightness(1.1);
	}

	.subagent-card-content :global(.btn-destructive:focus-visible) {
		outline: 2px solid var(--destructive);
		outline-offset: 2px;
	}

	/* Footer */
	.subagent-card-content :global(.footer-section) {
		background: color-mix(in oklch, var(--muted) 40%, transparent);
		border-top: 1px solid var(--border);
	}

	/* Tab navigation */
	.subagent-card-content :global(.tab-nav) {
		background: color-mix(in oklch, var(--muted) 40%, transparent);
	}

	.subagent-card-content :global(.tab-button) {
		transition: all 0.15s ease;
	}

	.subagent-card-content :global(.tab-button:hover) {
		background: color-mix(in oklch, var(--accent) 50%, transparent);
	}

	.subagent-card-content :global(.tab-button.active) {
		color: var(--primary);
	}

	/* Validation indicators - enhanced visibility */
	.subagent-card-content :global(.validation-dot-valid) {
		background: var(--success);
		box-shadow: 0 0 6px var(--success);
		border: 1px solid color-mix(in oklch, var(--success) 50%, transparent);
	}

	.subagent-card-content :global(.validation-dot-invalid) {
		background: var(--warning);
		box-shadow: 0 0 6px var(--warning);
		border: 1px solid color-mix(in oklch, var(--warning) 50%, transparent);
	}

	/* Form inputs - improved readability and focus states */
	.subagent-card-content :global(.form-input) {
		background: var(--input);
		border: 1px solid var(--border);
		color: var(--foreground);
		transition: all 0.15s ease;
		font-size: 0.875rem;
	}

	.subagent-card-content :global(.form-input::placeholder) {
		color: var(--muted-foreground);
	}

	.subagent-card-content :global(.form-input:hover) {
		border-color: color-mix(in oklch, var(--border) 100%, var(--primary) 30%);
	}

	.subagent-card-content :global(.form-input:focus) {
		outline: none;
		background: var(--card);
		border-color: var(--ring);
		box-shadow: 0 0 0 3px color-mix(in oklch, var(--ring) 20%, transparent);
	}

	/* Model selection buttons */
	.subagent-card-content :global(.model-option) {
		background: color-mix(in oklch, var(--muted) 40%, transparent);
		border: 1px solid var(--border);
		transition: all 0.15s ease;
		box-shadow: var(--shadow-s);
	}

	.subagent-card-content :global(.model-option:hover) {
		border-color: color-mix(in oklch, var(--primary) 50%, var(--border));
		background: var(--accent);
	}

	.subagent-card-content :global(.model-option.selected) {
		background: color-mix(in oklch, var(--primary) 15%, transparent);
		border-color: var(--primary);
		color: var(--primary);
	}

	/* Tool access buttons */
	.subagent-card-content :global(.tool-access-btn) {
		background: color-mix(in oklch, var(--muted) 40%, transparent);
		border: 1px solid var(--border);
		transition: all 0.15s ease;
		box-shadow: var(--shadow-s);
	}

	.subagent-card-content :global(.tool-access-btn:hover) {
		border-color: color-mix(in oklch, var(--primary) 50%, var(--border));
	}

	.subagent-card-content :global(.tool-access-btn.selected) {
		background: var(--primary);
		color: var(--primary-foreground);
		border-color: var(--primary);
	}

	/* Tool selection chips */
	.subagent-card-content :global(.tool-chip) {
		background: color-mix(in oklch, var(--muted) 40%, transparent);
		border: 1px solid var(--border);
		color: var(--foreground);
		transition: all 0.15s ease;
	}

	.subagent-card-content :global(.tool-chip:hover) {
		border-color: color-mix(in oklch, var(--primary) 50%, var(--border));
	}

	.subagent-card-content :global(.tool-chip.selected) {
		background: color-mix(in oklch, var(--primary) 15%, transparent);
		border-color: var(--primary);
		color: var(--primary);
	}

	/* Info/warning boxes */
	.subagent-card-content :global(.info-box) {
		background: color-mix(in oklch, var(--muted) 40%, transparent);
		border: 1px solid var(--border);
		color: var(--foreground);
	}

	.subagent-card-content :global(.warning-box) {
		background: color-mix(in oklch, var(--warning) 12%, transparent);
		border: 1px solid color-mix(in oklch, var(--warning) 25%, transparent);
		color: var(--warning);
	}

	/* Category checkbox styling */
	.subagent-card-content :global(.category-checkbox) {
		background: var(--muted);
		border: 1px solid var(--border);
		transition: all 0.15s ease;
	}

	.subagent-card-content :global(.category-checkbox.partial) {
		background: color-mix(in oklch, var(--primary) 50%, var(--muted));
	}

	.subagent-card-content :global(.category-checkbox.checked) {
		background: var(--primary);
	}
</style>
