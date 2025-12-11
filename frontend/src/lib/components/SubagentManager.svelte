<script lang="ts">
	/**
	 * SubagentManager - Global subagent management panel using UniversalModal
	 *
	 * Displays all subagents with full CRUD operations.
	 * Uses the universal modal design with responsive fullscreen on mobile.
	 */
	import { api } from '$lib/api/client';
	import { groups } from '$lib/stores/groups';
	import UniversalModal from './UniversalModal.svelte';

	// Tool interfaces
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

	const MODEL_OPTIONS = [
		{ value: '', label: 'Inherit' },
		{ value: 'haiku', label: 'Haiku' },
		{ value: 'sonnet', label: 'Sonnet' },
		{ value: 'sonnet-1m', label: 'Sonnet 1M' },
		{ value: 'opus', label: 'Opus' }
	];

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

	interface Props {
		open: boolean;
		onClose: () => void;
		onUpdate?: () => void;
	}

	let { open, onClose, onUpdate }: Props = $props();

	// State
	let subagents = $state<Subagent[]>([]);
	let loading = $state(true);
	let error = $state<string | null>(null);

	// Editor state
	let showEditor = $state(false);
	let editingSubagent = $state<Subagent | null>(null);

	// Form state
	let formId = $state('');
	let formName = $state('');
	let formDescription = $state('');
	let formPrompt = $state('');
	let formTools = $state<string[]>([]);
	let formModel = $state('');
	let saving = $state(false);

	// Delete state
	let deletingId = $state<string | null>(null);
	let deleteLoading = $state(false);

	// Import/Export state
	let importInput: HTMLInputElement;
	let importing = $state(false);

	// Available tools from API
	let availableTools = $state<ToolsResponse>({ categories: [], all_tools: [] });
	let toolCategoriesExpanded = $state<Record<string, boolean>>({});

	// Tool selection mode for editor: 'all' = inherit all, 'select' = only selected tools
	let toolSelectionMode = $state<'all' | 'select'>('all');

	// Load data when modal opens
	$effect(() => {
		if (open) {
			loadSubagents();
			loadTools();
		}
	});

	// Load available tools
	async function loadTools() {
		try {
			availableTools = await api.get<ToolsResponse>('/tools');
		} catch (e) {
			console.error('Failed to load tools:', e);
		}
	}

	// Toggle tool selection
	function toggleFormTool(toolName: string) {
		if (formTools.includes(toolName)) {
			formTools = formTools.filter(t => t !== toolName);
		} else {
			formTools = [...formTools, toolName];
		}
	}

	// Check if all tools in a category are selected
	function isCategoryFullySelected(category: ToolCategory): boolean {
		const categoryToolNames = category.tools.map(t => t.name);
		return categoryToolNames.length > 0 && categoryToolNames.every(name => formTools.includes(name));
	}

	// Check if some (but not all) tools in a category are selected
	function isCategoryPartiallySelected(category: ToolCategory): boolean {
		const categoryToolNames = category.tools.map(t => t.name);
		const selectedCount = categoryToolNames.filter(name => formTools.includes(name)).length;
		return selectedCount > 0 && selectedCount < categoryToolNames.length;
	}

	// Toggle all tools in a category
	function toggleFormCategory(category: ToolCategory) {
		const categoryToolNames = category.tools.map(t => t.name);
		const allSelected = categoryToolNames.every(name => formTools.includes(name));

		if (allSelected) {
			formTools = formTools.filter(t => !categoryToolNames.includes(t));
		} else {
			const newTools = categoryToolNames.filter(name => !formTools.includes(name));
			formTools = [...formTools, ...newTools];
		}
	}

	// Toggle tool category expansion
	function toggleToolCategoryExpansion(categoryId: string) {
		toolCategoriesExpanded[categoryId] = !toolCategoriesExpanded[categoryId];
		toolCategoriesExpanded = toolCategoriesExpanded;
	}

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
		showEditor = true;
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
		toolSelectionMode = (agent.tools && agent.tools.length > 0) ? 'select' : 'all';
		showEditor = true;
	}

	// Form validation
	const isValid = $derived(() => {
		if (!editingSubagent && !formId.match(/^[a-z0-9-]+$/)) return false;
		if (formId.length === 0) return false;
		if (formName.length === 0) return false;
		if (formDescription.length === 0) return false;
		if (formPrompt.length === 0) return false;
		return true;
	});

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
			if (formModel) body.model = formModel;

			if (editingSubagent) {
				await api.put(`/subagents/${editingSubagent.id}`, body);
			} else {
				body.id = formId;
				await api.post('/subagents', body);
			}

			showEditor = false;
			await loadSubagents();
			onUpdate?.();
		} catch (e: unknown) {
			const err = e as { detail?: string; message?: string };
			error = err.detail || err.message || 'Failed to save subagent';
		} finally {
			saving = false;
		}
	}

	// Delete subagent
	function handleDeleteClick(id: string) {
		deletingId = id;
	}

	async function confirmDelete() {
		if (!deletingId) return;
		deleteLoading = true;
		try {
			await api.delete(`/subagents/${deletingId}`);
			deletingId = null;
			await loadSubagents();
			onUpdate?.();
		} catch (e: unknown) {
			const err = e as { detail?: string; message?: string };
			error = err.detail || err.message || 'Failed to delete subagent';
		} finally {
			deleteLoading = false;
		}
	}

	function getModelDisplay(model?: string): string {
		switch (model) {
			case 'haiku': return 'Haiku';
			case 'sonnet': return 'Sonnet';
			case 'sonnet-1m': return 'Sonnet 1M';
			case 'opus': return 'Opus';
			default: return 'Inherit';
		}
	}

	// Export a single subagent
	async function exportSubagent(agentId: string) {
		try {
			const data = await api.get<any>(`/export-import/subagents/${agentId}`);
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

	// Trigger import file dialog
	function triggerImport() {
		importInput?.click();
	}

	// Handle import file selection
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

			const result = await api.post<any>('/export-import/import/json?overwrite_existing=false', data);

			if (result.subagents_imported > 0) {
				await loadSubagents();
				onUpdate?.();
			} else if (result.subagents_skipped > 0) {
				error = 'Subagent already exists. Use a different ID or delete the existing one first.';
			} else if (result.errors?.length > 0) {
				error = 'Import failed: ' + result.errors.join(', ');
			}
		} catch (e: unknown) {
			const err = e as { detail?: string; message?: string };
			error = 'Import failed: ' + (err.detail || err.message || 'Invalid JSON file');
		}

		importing = false;
	}

	function handleClose() {
		if (showEditor) {
			showEditor = false;
		} else if (deletingId) {
			deletingId = null;
		} else {
			onClose();
		}
	}

	// Subagent icon path
	const subagentIcon = 'M9.75 17L9 20l-1 1h8l-1-1-.75-3M3 13h18M5 17h14a2 2 0 002-2V5a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z';
</script>

<UniversalModal
	{open}
	title="Subagents"
	icon={subagentIcon}
	onClose={handleClose}
	showFooter={false}
	size="lg"
>
	{#if error && !showEditor}
		<div class="p-3 bg-destructive/10 border border-destructive/20 rounded-lg text-destructive text-sm mb-4">
			{error}
			<button onclick={() => error = null} class="ml-2 underline">Dismiss</button>
		</div>
	{/if}

	{#if loading}
		<div class="flex items-center justify-center py-12">
			<svg class="w-6 h-6 animate-spin text-primary" fill="none" viewBox="0 0 24 24">
				<circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
				<path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
			</svg>
		</div>
	{:else if subagents.length === 0}
		<!-- Hidden file input for import -->
		<input type="file" accept=".json" bind:this={importInput} onchange={handleImport} class="hidden" />

		<div class="text-center py-8">
			<div class="w-16 h-16 bg-muted rounded-2xl flex items-center justify-center mx-auto mb-4">
				<svg class="w-8 h-8 text-muted-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d={subagentIcon} />
				</svg>
			</div>
			<p class="text-sm text-muted-foreground mb-4">No subagents configured</p>
			<div class="flex gap-2 justify-center">
				<button onclick={handleCreate} class="px-4 py-2 bg-primary text-primary-foreground rounded-lg text-sm font-medium hover:opacity-90">
					+ New Subagent
				</button>
				<button onclick={triggerImport} disabled={importing} class="px-4 py-2 bg-muted text-foreground rounded-lg text-sm font-medium hover:bg-accent flex items-center gap-2">
					{#if importing}
						<span class="animate-spin inline-block w-4 h-4 border-2 border-current border-t-transparent rounded-full"></span>
					{:else}
						<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-8l-4-4m0 0L8 8m4-4v12" />
						</svg>
					{/if}
					Import
				</button>
			</div>
		</div>
	{:else}
		<!-- Hidden file input for import -->
		<input type="file" accept=".json" bind:this={importInput} onchange={handleImport} class="hidden" />

		<div class="space-y-3 mb-4">
			{#each subagents as agent (agent.id)}
				<div class="p-4 bg-muted/30 rounded-xl border border-border hover:border-primary/30 transition-colors">
					<div class="flex items-start justify-between gap-4">
						<div class="flex-1 min-w-0">
							<div class="flex items-center gap-2 mb-1">
								<h4 class="text-sm font-medium text-foreground">{agent.name}</h4>
								<span class="text-[10px] px-1.5 py-0.5 bg-primary/10 text-primary rounded">
									{getModelDisplay(agent.model)}
								</span>
								{#if $groups.subagents.assignments[agent.id]}
									<span class="text-[10px] px-1.5 py-0.5 bg-primary/10 text-primary rounded">{$groups.subagents.assignments[agent.id]}</span>
								{/if}
							</div>
							<p class="text-xs text-muted-foreground">{agent.id}</p>
							<p class="text-xs text-muted-foreground/70 mt-1 line-clamp-2">{agent.description}</p>
						</div>
						<div class="flex items-center gap-1">
							<!-- Group button -->
							<div class="relative group/dropdown">
								<button class="p-2 text-muted-foreground hover:text-foreground hover:bg-accent rounded-lg transition-colors" title="Assign to group">
									<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
										<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M7 7h.01M7 3h5c.512 0 1.024.195 1.414.586l7 7a2 2 0 010 2.828l-7 7a2 2 0 01-2.828 0l-7-7A1.994 1.994 0 013 12V7a4 4 0 014-4z" />
									</svg>
								</button>
								<div class="absolute right-0 top-full mt-1 w-40 bg-card border border-border rounded-lg shadow-lg opacity-0 invisible group-hover/dropdown:opacity-100 group-hover/dropdown:visible transition-all z-50">
									<div class="py-1 max-h-48 overflow-y-auto">
										{#each $groups.subagents.groups as group}
											<button
												onclick={() => groups.assignToGroup('subagents', agent.id, group.name)}
												class="w-full px-3 py-1.5 text-left text-sm hover:bg-accent transition-colors flex items-center gap-2"
											>
												{#if $groups.subagents.assignments[agent.id] === group.name}
													<svg class="w-3 h-3 text-primary" fill="none" stroke="currentColor" viewBox="0 0 24 24">
														<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
													</svg>
												{:else}
													<span class="w-3"></span>
												{/if}
												{group.name}
											</button>
										{/each}
										{#if $groups.subagents.assignments[agent.id]}
											<button
												onclick={() => groups.removeFromGroup('subagents', agent.id)}
												class="w-full px-3 py-1.5 text-left text-sm hover:bg-accent transition-colors text-muted-foreground"
											>
												<span class="ml-5">Remove</span>
											</button>
										{/if}
										<div class="border-t border-border my-1"></div>
										<button
											onclick={() => { const name = prompt('New group name:'); if (name?.trim()) { groups.createGroup('subagents', name.trim()); groups.assignToGroup('subagents', agent.id, name.trim()); } }}
											class="w-full px-3 py-1.5 text-left text-sm hover:bg-accent transition-colors text-muted-foreground"
										>
											<span class="ml-5">+ New group</span>
										</button>
									</div>
								</div>
							</div>
							<!-- Export button -->
							<button onclick={() => exportSubagent(agent.id)} class="p-2 text-muted-foreground hover:text-foreground hover:bg-accent rounded-lg transition-colors" title="Export subagent">
								<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" />
								</svg>
							</button>
							<!-- Edit button -->
							<button onclick={() => handleEdit(agent)} class="p-2 text-muted-foreground hover:text-foreground hover:bg-accent rounded-lg transition-colors" title="Edit subagent">
								<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" />
								</svg>
							</button>
							<!-- Delete button -->
							<button onclick={() => handleDeleteClick(agent.id)} class="p-2 text-muted-foreground hover:text-destructive hover:bg-destructive/10 rounded-lg transition-colors" title="Delete subagent">
								<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
								</svg>
							</button>
						</div>
					</div>
				</div>
			{/each}
		</div>

		<div class="flex gap-2">
			<button onclick={handleCreate} class="flex-1 py-2.5 bg-primary text-primary-foreground rounded-xl text-sm font-medium hover:opacity-90">
				+ New Subagent
			</button>
			<button onclick={triggerImport} disabled={importing} class="px-4 py-2.5 bg-muted text-foreground rounded-xl text-sm font-medium hover:bg-accent flex items-center gap-2" title="Import subagent from file">
				{#if importing}
					<span class="animate-spin inline-block w-4 h-4 border-2 border-current border-t-transparent rounded-full"></span>
				{:else}
					<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-8l-4-4m0 0L8 8m4-4v12" />
					</svg>
				{/if}
				Import
			</button>
		</div>
	{/if}
</UniversalModal>

<!-- Editor Modal -->
{#if showEditor}
	<UniversalModal
		open={showEditor}
		title={editingSubagent ? 'Edit Subagent' : 'New Subagent'}
		icon={subagentIcon}
		onClose={() => showEditor = false}
		onSave={handleSave}
		saveLabel={editingSubagent ? 'Save Changes' : 'Create Subagent'}
		saveDisabled={!isValid()}
		{saving}
		size="lg"
	>
		{#if error}
			<div class="p-3 bg-destructive/10 border border-destructive/20 rounded-lg text-destructive text-sm mb-4">
				{error}
			</div>
		{/if}

		<div class="space-y-4">
			<!-- ID and Name row (only show ID for new) -->
			{#if !editingSubagent}
				<div class="grid grid-cols-2 gap-4">
					<div>
						<label class="block text-sm font-medium text-foreground mb-1.5">ID</label>
						<input
							bind:value={formId}
							placeholder="my-agent-id"
							class="w-full px-3 py-2.5 bg-input border border-border rounded-lg text-sm text-foreground"
						/>
					</div>
					<div>
						<label class="block text-sm font-medium text-foreground mb-1.5">Name</label>
						<input
							bind:value={formName}
							placeholder="Research Assistant"
							class="w-full px-3 py-2.5 bg-input border border-border rounded-lg text-sm text-foreground"
						/>
					</div>
				</div>
			{:else}
				<div>
					<label class="block text-sm font-medium text-foreground mb-1.5">Name</label>
					<input
						bind:value={formName}
						placeholder="Research Assistant"
						class="w-full px-3 py-2.5 bg-input border border-border rounded-lg text-sm text-foreground"
					/>
				</div>
			{/if}

			<!-- Description -->
			<div>
				<label class="block text-sm font-medium text-foreground mb-1.5">Description</label>
				<input
					bind:value={formDescription}
					placeholder="When to use this agent..."
					class="w-full px-3 py-2.5 bg-input border border-border rounded-lg text-sm text-foreground"
				/>
			</div>

			<!-- Model -->
			<div>
				<label class="block text-sm font-medium text-foreground mb-1.5">Model</label>
				<select
					bind:value={formModel}
					class="w-full px-3 py-2.5 bg-input border border-border rounded-lg text-sm text-foreground"
				>
					{#each MODEL_OPTIONS as opt}
						<option value={opt.value}>{opt.label}</option>
					{/each}
				</select>
			</div>

			<!-- Tool Configuration Accordion -->
			<div class="border border-border rounded-xl overflow-hidden">
				<button
					type="button"
					onclick={() => toolCategoriesExpanded['_toolConfig'] = !toolCategoriesExpanded['_toolConfig']}
					class="w-full px-4 py-3 bg-muted/50 flex items-center justify-between text-sm text-foreground hover:bg-muted"
				>
					<span class="font-medium">Tool Configuration</span>
					<svg class="w-4 h-4 transition-transform {toolCategoriesExpanded['_toolConfig'] ? 'rotate-180' : ''}" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
					</svg>
				</button>
				{#if toolCategoriesExpanded['_toolConfig']}
					<div class="p-4 space-y-4 bg-card">
						<!-- Tool selection mode -->
						<div>
							<label class="block text-xs text-muted-foreground mb-2">Tool Access Mode</label>
							<div class="flex gap-2">
								<button
									type="button"
									onclick={() => { toolSelectionMode = 'all'; formTools = []; }}
									class="px-4 py-2 text-sm rounded-lg transition-colors {toolSelectionMode === 'all' ? 'bg-primary text-primary-foreground' : 'bg-muted text-foreground hover:bg-accent'}"
								>
									All Tools
								</button>
								<button
									type="button"
									onclick={() => toolSelectionMode = 'select'}
									class="px-4 py-2 text-sm rounded-lg transition-colors {toolSelectionMode === 'select' ? 'bg-primary text-primary-foreground' : 'bg-muted text-foreground hover:bg-accent'}"
								>
									Select Tools
								</button>
							</div>
							<p class="text-xs text-muted-foreground mt-2">
								{#if toolSelectionMode === 'all'}
									Subagent can use all tools available to the profile.
								{:else}
									Subagent can ONLY use the selected tools below.
								{/if}
							</p>
						</div>

						<!-- Tool categories (only shown in select mode) -->
						{#if toolSelectionMode === 'select'}
							<div class="border-t border-border pt-4 space-y-2">
								{#each availableTools.categories as category}
									{#if category.tools.length > 0}
										<div class="border border-border rounded-lg overflow-hidden">
											<!-- Category header -->
											<div class="w-full px-3 py-2 bg-muted/30 flex items-center gap-2 text-sm text-foreground">
												<!-- Custom checkbox with indeterminate state -->
												<button
													type="button"
													onclick={() => toggleFormCategory(category)}
													class="w-5 h-5 rounded flex items-center justify-center transition-colors {isCategoryFullySelected(category) ? 'bg-primary' : isCategoryPartiallySelected(category) ? 'bg-primary' : 'bg-muted border border-border'}"
												>
													{#if isCategoryFullySelected(category)}
														<svg class="w-3 h-3 text-primary-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
															<path stroke-linecap="round" stroke-linejoin="round" stroke-width="3" d="M5 13l4 4L19 7" />
														</svg>
													{:else if isCategoryPartiallySelected(category)}
														<svg class="w-3 h-3 text-primary-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
															<path stroke-linecap="round" stroke-linejoin="round" stroke-width="3" d="M5 12h14" />
														</svg>
													{/if}
												</button>
												<button
													type="button"
													onclick={() => toggleToolCategoryExpansion(category.id)}
													class="flex-1 flex items-center gap-2 text-left hover:text-foreground/80"
												>
													<span class="flex-1">{category.name}</span>
													<span class="text-xs text-muted-foreground">({category.tools.length} tools)</span>
													<svg class="w-4 h-4 transition-transform {toolCategoriesExpanded[category.id] ? 'rotate-180' : ''}" fill="none" stroke="currentColor" viewBox="0 0 24 24">
														<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
													</svg>
												</button>
											</div>
											<!-- Individual tools -->
											{#if toolCategoriesExpanded[category.id]}
												<div class="p-3 space-y-1 bg-card">
													{#each category.tools as tool}
														<label class="flex items-center gap-2 px-2 py-1.5 rounded-lg hover:bg-muted/50 cursor-pointer">
															<input
																type="checkbox"
																checked={formTools.includes(tool.name)}
																onchange={() => toggleFormTool(tool.name)}
																class="w-4 h-4 rounded bg-muted border-0 text-primary focus:ring-primary"
															/>
															<span class="text-sm text-foreground">{tool.name}</span>
															<span class="text-xs text-muted-foreground">- {tool.description}</span>
														</label>
													{/each}
												</div>
											{/if}
										</div>
									{/if}
								{/each}
							</div>

							<!-- Summary -->
							<div class="text-xs text-muted-foreground pt-2 border-t border-border">
								{formTools.length} tool{formTools.length !== 1 ? 's' : ''} selected
								{#if formTools.length > 0}
									<span class="text-foreground">: {formTools.join(', ')}</span>
								{/if}
							</div>
						{/if}
					</div>
				{/if}
			</div>

			<!-- System Prompt Accordion -->
			<div class="border border-border rounded-xl overflow-hidden">
				<button
					type="button"
					onclick={() => toolCategoriesExpanded['_systemPrompt'] = !toolCategoriesExpanded['_systemPrompt']}
					class="w-full px-4 py-3 bg-muted/50 flex items-center justify-between text-sm text-foreground hover:bg-muted"
				>
					<span class="font-medium">System Prompt</span>
					<svg class="w-4 h-4 transition-transform {toolCategoriesExpanded['_systemPrompt'] ? 'rotate-180' : ''}" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
					</svg>
				</button>
				{#if toolCategoriesExpanded['_systemPrompt']}
					<div class="p-4 bg-card">
						<textarea
							bind:value={formPrompt}
							placeholder="You are a specialized agent..."
							rows={8}
							class="w-full px-3 py-2.5 bg-input border border-border rounded-lg text-sm text-foreground resize-y font-mono"
						></textarea>
					</div>
				{/if}
			</div>
		</div>
	</UniversalModal>
{/if}

<!-- Delete Confirmation -->
{#if deletingId}
	<div
		class="fixed inset-0 bg-black/60 z-[70] flex items-center justify-center p-4"
		onclick={() => deletingId = null}
		role="dialog"
		aria-modal="true"
	>
		<!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
		<div class="bg-card rounded-2xl w-full max-w-sm shadow-2xl border border-border" onclick={(e) => e.stopPropagation()}>
			<div class="p-6 text-center">
				<div class="w-12 h-12 bg-destructive/15 rounded-full flex items-center justify-center mx-auto mb-4">
					<svg class="w-6 h-6 text-destructive" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
					</svg>
				</div>
				<h3 class="text-lg font-semibold text-foreground mb-2">Delete Subagent</h3>
				<p class="text-sm text-muted-foreground mb-6">
					Are you sure you want to delete <strong class="text-foreground">{subagents.find(s => s.id === deletingId)?.name}</strong>? This action cannot be undone.
				</p>
				<div class="flex gap-3">
					<button
						onclick={() => deletingId = null}
						disabled={deleteLoading}
						class="flex-1 px-4 py-2.5 bg-muted text-foreground rounded-xl text-sm font-medium hover:bg-accent"
					>
						Cancel
					</button>
					<button
						onclick={confirmDelete}
						disabled={deleteLoading}
						class="flex-1 px-4 py-2.5 bg-destructive text-destructive-foreground rounded-xl text-sm font-medium hover:opacity-90 disabled:opacity-50 flex items-center justify-center gap-2"
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
		</div>
	</div>
{/if}
