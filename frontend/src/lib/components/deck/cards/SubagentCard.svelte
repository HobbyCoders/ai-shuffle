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
		onMinimize: () => void;
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
		onMinimize,
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
							class="w-full pl-10 pr-4 py-2 bg-background border border-border rounded-xl text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/50"
						/>
					</div>
				{/if}
			</div>

			<!-- Content -->
			<div class="flex-1 overflow-y-auto p-4">
				{#if error}
					<div class="mb-4 p-3 bg-red-500/10 border border-red-500/20 rounded-xl text-red-500 text-sm flex items-center gap-2">
						<span class="flex-1">{error}</span>
						<button onclick={() => (error = null)} class="hover:text-red-400">
							<X class="w-4 h-4" />
						</button>
					</div>
				{/if}

				<!-- Delete confirmation banner -->
				{#if deletingId}
					<div class="mb-4 p-4 bg-red-500/10 border border-red-500/20 rounded-xl">
						<p class="text-sm text-foreground mb-3">
							Delete <strong>{subagents.find(s => s.id === deletingId)?.name}</strong>? This cannot be undone.
						</p>
						<div class="flex gap-2">
							<button
								onclick={() => (deletingId = null)}
								disabled={deleteLoading}
								class="flex-1 px-3 py-2 bg-muted text-foreground rounded-lg hover:bg-accent transition-colors text-sm"
							>
								Cancel
							</button>
							<button
								onclick={confirmDelete}
								disabled={deleteLoading}
								class="flex-1 px-3 py-2 bg-red-500 text-white rounded-lg hover:bg-red-600 transition-colors text-sm flex items-center justify-center gap-2"
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
					<div class="space-y-2">
						{#each filteredSubagents() as agent (agent.id)}
							<!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
							<div
								class="p-3 bg-background hover:bg-accent border border-border rounded-xl transition-all group cursor-pointer"
								onclick={() => handleEdit(agent)}
							>
								<div class="flex items-start gap-3">
									<div class="w-9 h-9 rounded-lg bg-primary/10 flex items-center justify-center flex-shrink-0">
										<Monitor class="w-4 h-4 text-primary" />
									</div>
									<div class="flex-1 min-w-0">
										<div class="flex items-center gap-2 flex-wrap">
											<span class="font-medium text-foreground text-sm">{agent.name}</span>
											<span class="text-[10px] px-1.5 py-0.5 rounded {getModelColor(agent.model)}">
												{getModelDisplay(agent.model)}
											</span>
											{#if agent.tools && agent.tools.length > 0}
												<span class="text-[10px] px-1.5 py-0.5 rounded bg-muted text-muted-foreground">
													{agent.tools.length} tools
												</span>
											{/if}
											{#if $groups.subagents.assignments[agent.id]}
												<span class="text-[10px] px-1.5 py-0.5 rounded bg-primary/10 text-primary">
													{$groups.subagents.assignments[agent.id]}
												</span>
											{/if}
										</div>
										<p class="text-xs text-muted-foreground font-mono mt-0.5">{agent.id}</p>
										<p class="text-xs text-muted-foreground mt-1 line-clamp-2">{agent.description}</p>
									</div>
									<div class="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
										<button
											onclick={(e) => exportSubagent(agent.id, e)}
											class="p-2 text-muted-foreground hover:text-foreground rounded-lg hover:bg-muted transition-colors"
											title="Export"
										>
											<Download class="w-4 h-4" />
										</button>
										<button
											onclick={(e) => handleDeleteClick(agent.id, e)}
											class="p-2 text-muted-foreground hover:text-red-500 rounded-lg hover:bg-red-500/10 transition-colors"
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
			<div class="px-4 py-4 border-t border-border bg-muted/30">
				<div class="flex gap-2">
					<button
						onclick={handleCreate}
						class="flex-1 flex items-center justify-center gap-2 px-4 py-2.5 bg-primary text-primary-foreground rounded-xl hover:bg-primary/90 transition-colors font-medium"
					>
						<Plus class="w-4 h-4" />
						New Subagent
					</button>
					<button
						onclick={triggerImport}
						disabled={importing}
						class="px-4 py-2.5 bg-background border border-border text-foreground rounded-xl hover:bg-muted transition-colors flex items-center gap-2"
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
			<div class="flex border-b border-border bg-muted/30">
				{#each tabs as tab}
					<button
						onclick={() => activeTab = tab.id}
						class="flex-1 flex items-center justify-center gap-2 px-4 py-2.5 text-sm font-medium transition-all relative
							{activeTab === tab.id
								? 'text-primary'
								: 'text-muted-foreground hover:text-foreground'}"
					>
						<tab.icon class="w-4 h-4" />
						<span>{tab.label}</span>

						<!-- Validation indicator -->
						{#if !tabStatus()[tab.id]}
							<span class="w-2 h-2 rounded-full bg-amber-500"></span>
						{:else}
							<span class="w-2 h-2 rounded-full bg-green-500"></span>
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
					<div class="mb-4 p-3 bg-red-500/10 border border-red-500/20 rounded-xl text-red-500 text-sm flex items-center gap-2">
						<span class="flex-1">{error}</span>
						<button onclick={() => (error = null)} class="hover:text-red-400">
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
									ID <span class="text-red-500">*</span>
								</label>
								<input
									id="agent-id"
									type="text"
									bind:value={formId}
									placeholder="my-agent-id"
									class="w-full px-4 py-2.5 bg-background border border-border rounded-xl text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary transition-all"
								/>
								<p class="text-xs text-muted-foreground">
									Lowercase letters, numbers, and hyphens only.
								</p>
							</div>
						{/if}

						<div class="space-y-2">
							<label for="agent-name" class="block text-sm font-medium text-foreground">
								Display Name <span class="text-red-500">*</span>
							</label>
							<input
								id="agent-name"
								type="text"
								bind:value={formName}
								placeholder="Research Assistant"
								class="w-full px-4 py-2.5 bg-background border border-border rounded-xl text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary transition-all"
							/>
						</div>

						<div class="space-y-2">
							<label for="agent-description" class="block text-sm font-medium text-foreground">
								Description <span class="text-red-500">*</span>
							</label>
							<textarea
								id="agent-description"
								bind:value={formDescription}
								placeholder="Use this agent when you need to research topics..."
								rows={3}
								class="w-full px-4 py-2.5 bg-background border border-border rounded-xl text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary transition-all resize-none"
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
							<div class="grid grid-cols-2 gap-2">
								{#each MODEL_OPTIONS as opt}
									<button
										type="button"
										onclick={() => formModel = opt.value}
										class="flex flex-col items-center gap-1 p-3 rounded-xl border transition-all
											{formModel === opt.value
												? 'bg-primary/10 border-primary text-primary'
												: 'bg-background border-border text-foreground hover:border-primary/50'}"
									>
										<span class="text-sm font-medium">{opt.label}</span>
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
									<span class="text-xs text-muted-foreground">{formTools.length} selected</span>
								{/if}
							</div>

							<div class="flex gap-2">
								<button
									type="button"
									onclick={() => { toolSelectionMode = 'all'; formTools = []; }}
									class="flex-1 flex items-center justify-center gap-2 px-4 py-2.5 rounded-xl border transition-all
										{toolSelectionMode === 'all'
											? 'bg-primary text-primary-foreground border-primary'
											: 'bg-background border-border text-foreground hover:border-primary/50'}"
								>
									<Check class="w-4 h-4" />
									All Tools
								</button>
								<button
									type="button"
									onclick={() => toolSelectionMode = 'select'}
									class="flex-1 flex items-center justify-center gap-2 px-4 py-2.5 rounded-xl border transition-all
										{toolSelectionMode === 'select'
											? 'bg-primary text-primary-foreground border-primary'
											: 'bg-background border-border text-foreground hover:border-primary/50'}"
								>
									<Settings class="w-4 h-4" />
									Custom
								</button>
							</div>

							{#if toolSelectionMode === 'all'}
								<p class="text-xs text-muted-foreground bg-muted/50 rounded-lg p-3">
									The subagent will have access to all tools available in the profile.
								</p>
							{/if}
						</div>

						<!-- Tool Selection -->
						{#if toolSelectionMode === 'select'}
							<div class="space-y-4">
								<div class="flex gap-2 text-xs">
									<button type="button" onclick={selectAllTools} class="text-primary hover:underline">
										Select all
									</button>
									<span class="text-muted-foreground">|</span>
									<button type="button" onclick={clearAllTools} class="text-primary hover:underline">
										Clear all
									</button>
								</div>

								<div class="space-y-4 max-h-64 overflow-y-auto">
									{#each availableTools.categories as category}
										{#if category.tools.length > 0}
											<div class="space-y-2">
												<button
													type="button"
													onclick={() => selectAllInCategory(category)}
													class="flex items-center gap-2 text-xs font-medium text-muted-foreground hover:text-foreground transition-colors"
												>
													<span class="w-4 h-4 rounded flex items-center justify-center transition-colors
														{getCategorySelectionState(category) === 'all'
															? 'bg-primary'
															: getCategorySelectionState(category) === 'partial'
																? 'bg-primary/50'
																: 'bg-muted border border-border'}">
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
															class="px-3 py-1.5 text-xs rounded-lg border transition-all
																{formTools.includes(tool.name)
																	? 'bg-primary/10 border-primary text-primary'
																	: 'bg-background border-border text-foreground hover:border-primary/50'}"
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
									<p class="text-xs text-amber-500 bg-amber-500/10 rounded-lg p-3">
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
								System Prompt <span class="text-red-500">*</span>
							</label>
							<span class="text-xs text-muted-foreground">
								{formPrompt.length} characters
							</span>
						</div>
						<textarea
							id="agent-prompt"
							bind:value={formPrompt}
							placeholder="You are a specialized agent that helps users with..."
							class="flex-1 min-h-[200px] w-full px-4 py-3 bg-background border border-border rounded-xl text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary transition-all font-mono text-sm resize-none"
						></textarea>
						<p class="text-xs text-muted-foreground">
							Instructions that define the subagent's behavior and capabilities.
						</p>
					</div>
				{/if}
			</div>

			<!-- Footer -->
			<div class="px-4 py-4 border-t border-border bg-muted/30">
				<div class="flex gap-2">
					<button
						onclick={handleBack}
						class="flex-1 px-4 py-2.5 bg-muted text-foreground rounded-xl hover:bg-accent transition-colors"
					>
						Cancel
					</button>
					<button
						onclick={handleSave}
						disabled={!isValid() || saving}
						class="flex-1 px-4 py-2.5 bg-primary text-primary-foreground rounded-xl hover:bg-primary/90 transition-colors disabled:opacity-50 flex items-center justify-center gap-2"
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
		{onMinimize}
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
</style>
