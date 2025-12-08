<script lang="ts">
	/**
	 * SubagentManager - Global subagent management panel for main UI
	 *
	 * Displays all subagents in a sidebar panel with full CRUD operations.
	 * Responsive design for mobile (full screen) and desktop (sidebar).
	 */
	import { api } from '$lib/api/client';
	import { groups } from '$lib/stores/groups';

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
		onClose: () => void;
		onUpdate?: () => void;
	}

	let { onClose, onUpdate }: Props = $props();

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

	// Expanded view
	let expandedId = $state<string | null>(null);

	// Import/Export state
	let importInput: HTMLInputElement;
	let importing = $state(false);

	// Available tools from API
	let availableTools = $state<ToolsResponse>({ categories: [], all_tools: [] });
	let toolCategoriesExpanded = $state<Record<string, boolean>>({});

	// Tool selection mode for editor: 'all' = inherit all, 'select' = only selected tools
	let toolSelectionMode = $state<'all' | 'select'>('all');

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
			// Deselect all in category
			formTools = formTools.filter(t => !categoryToolNames.includes(t));
		} else {
			// Select all in category
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

	$effect(() => {
		loadSubagents();
		loadTools();
	});

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
		// Set tool selection mode based on whether tools are restricted
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
			// Only include tools if in 'select' mode and tools are selected
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

	function handleKeydown(e: KeyboardEvent) {
		if (e.key === 'Escape') {
			if (showEditor) {
				showEditor = false;
			} else if (deletingId) {
				deletingId = null;
			} else {
				onClose();
			}
		}
	}
</script>

<svelte:window on:keydown={handleKeydown} />

<!-- Modal backdrop -->
<div
	class="fixed inset-0 bg-black/50 z-50 flex items-center justify-center p-4"
	onclick={() => onClose()}
	role="dialog"
	aria-modal="true"
>
	<!-- Modal content -->
	<!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
	<div class="bg-card rounded-xl w-full max-w-lg max-h-[80vh] overflow-y-auto shadow-lg border border-border" onclick={(e) => e.stopPropagation()}>
		<!-- Header -->
		<div class="p-4 border-b border-border flex items-center justify-between">
			<h2 class="text-lg font-semibold text-foreground">Subagents</h2>
			<button
				class="text-muted-foreground hover:text-foreground"
				onclick={onClose}
			>
				<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
				</svg>
			</button>
		</div>

		<!-- Content -->
		<div class="p-4">
		{#if error && !showEditor}
			<div class="p-3 bg-red-500/10 border border-red-500/20 rounded-md text-red-500 text-sm mb-4">
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

			<p class="text-sm text-muted-foreground text-center py-4 mb-4">No subagents configured</p>
			<div class="flex gap-2">
				<button onclick={handleCreate} class="flex-1 py-2 border border-dashed border-border rounded-lg text-muted-foreground hover:text-foreground hover:border-gray-500">
					+ New Subagent
				</button>
				<button onclick={triggerImport} disabled={importing} class="px-4 py-2 border border-dashed border-border rounded-lg text-muted-foreground hover:text-foreground hover:border-gray-500 flex items-center gap-2" title="Import subagent from file">
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
		{:else}
			<!-- Hidden file input for import -->
			<input type="file" accept=".json" bind:this={importInput} onchange={handleImport} class="hidden" />

			<div class="space-y-2 mb-4">
				{#each subagents as agent (agent.id)}
					<div class="flex items-center justify-between p-3 bg-accent rounded-lg">
						<div class="flex-1 min-w-0">
							<div class="flex items-center gap-2">
								<p class="text-sm text-foreground font-medium">{agent.name}</p>
								<span class="text-[10px] px-1.5 py-0.5 bg-primary/10 text-primary rounded">
									{getModelDisplay(agent.model)}
								</span>
								{#if $groups.subagents.assignments[agent.id]}
									<span class="text-[10px] px-1.5 py-0.5 bg-primary/10 text-primary rounded">{$groups.subagents.assignments[agent.id]}</span>
								{/if}
							</div>
							<p class="text-xs text-muted-foreground">{agent.id}</p>
						</div>
						<div class="flex gap-1">
							<!-- Group button -->
							<div class="relative group/dropdown">
								<button class="p-1.5 text-muted-foreground hover:text-foreground" title="Assign to group">
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
							<button onclick={() => exportSubagent(agent.id)} class="p-1.5 text-muted-foreground hover:text-foreground" title="Export subagent">
								<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" />
								</svg>
							</button>
							<!-- Edit button -->
							<button onclick={() => handleEdit(agent)} class="p-1.5 text-muted-foreground hover:text-foreground" title="Edit subagent">
								<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" />
								</svg>
							</button>
							<!-- Delete button -->
							<button onclick={() => handleDeleteClick(agent.id)} class="p-1.5 text-muted-foreground hover:text-destructive" title="Delete subagent">
								<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
								</svg>
							</button>
						</div>
					</div>
				{/each}
			</div>
			<div class="flex gap-2">
				<button onclick={handleCreate} class="flex-1 py-2 border border-dashed border-border rounded-lg text-muted-foreground hover:text-foreground hover:border-gray-500">
					+ New Subagent
				</button>
				<button onclick={triggerImport} disabled={importing} class="px-4 py-2 border border-dashed border-border rounded-lg text-muted-foreground hover:text-foreground hover:border-gray-500 flex items-center gap-2" title="Import subagent from file">
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
		</div>
	</div>
</div>

<!-- Editor Modal -->
{#if showEditor}
	<div
		class="fixed inset-0 bg-black/50 z-[60] flex items-center justify-center p-4"
		onclick={() => showEditor = false}
		role="dialog"
		aria-modal="true"
	>
		<!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
		<div class="bg-card rounded-xl w-full max-w-lg max-h-[80vh] overflow-y-auto shadow-lg border border-border" onclick={(e) => e.stopPropagation()}>
			<!-- Header -->
			<div class="p-4 border-b border-border flex items-center justify-between">
				<h2 class="text-lg font-semibold text-foreground">
					{editingSubagent ? 'Edit Subagent' : 'New Subagent'}
				</h2>
				<button
					class="text-muted-foreground hover:text-foreground"
					onclick={() => showEditor = false}
				>
					<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
					</svg>
				</button>
			</div>

			<!-- Content -->
			<div class="p-4 space-y-4">
				{#if error}
					<div class="p-3 bg-red-500/10 border border-red-500/20 rounded-lg text-red-500 text-sm">
						{error}
					</div>
				{/if}

				<!-- ID and Name row (only show ID for new) -->
				{#if !editingSubagent}
					<div class="grid grid-cols-2 gap-4">
						<div>
							<label class="block text-xs text-muted-foreground mb-1">ID</label>
							<input
								bind:value={formId}
								placeholder="my-agent-id"
								class="w-full bg-muted border border-border rounded-lg px-3 py-2 text-sm text-foreground"
							/>
						</div>
						<div>
							<label class="block text-xs text-muted-foreground mb-1">Name</label>
							<input
								bind:value={formName}
								placeholder="Research Assistant"
								class="w-full bg-muted border border-border rounded-lg px-3 py-2 text-sm text-foreground"
							/>
						</div>
					</div>
				{:else}
					<div>
						<label class="block text-xs text-muted-foreground mb-1">Name</label>
						<input
							bind:value={formName}
							placeholder="Research Assistant"
							class="w-full bg-muted border border-border rounded-lg px-3 py-2 text-sm text-foreground"
						/>
					</div>
				{/if}

				<!-- Description -->
				<div>
					<label class="block text-xs text-muted-foreground mb-1">Description</label>
					<input
						bind:value={formDescription}
						placeholder="When to use this agent..."
						class="w-full bg-muted border border-border rounded-lg px-3 py-2 text-sm text-foreground"
					/>
				</div>

				<!-- Model -->
				<div>
					<label class="block text-xs text-muted-foreground mb-1">Model</label>
					<select
						bind:value={formModel}
						class="w-full bg-muted border border-border rounded-lg px-3 py-2 text-sm text-foreground"
					>
						{#each MODEL_OPTIONS as opt}
							<option value={opt.value}>{opt.label}</option>
						{/each}
					</select>
				</div>

				<!-- Tool Configuration Accordion -->
				<div class="border border-border rounded-lg overflow-hidden">
					<button
						type="button"
						onclick={() => toolCategoriesExpanded['_toolConfig'] = !toolCategoriesExpanded['_toolConfig']}
						class="w-full px-3 py-2 bg-accent flex items-center justify-between text-sm text-foreground hover:bg-muted"
					>
						<span>Tool Configuration</span>
						<svg class="w-4 h-4 transition-transform {toolCategoriesExpanded['_toolConfig'] ? 'rotate-180' : ''}" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
						</svg>
					</button>
					{#if toolCategoriesExpanded['_toolConfig']}
						<div class="p-3 space-y-3 bg-card">
							<!-- Tool selection mode -->
							<div>
								<label class="block text-xs text-muted-foreground mb-2">Tool Access Mode</label>
								<div class="flex gap-2">
									<button
										type="button"
										onclick={() => { toolSelectionMode = 'all'; formTools = []; }}
										class="px-3 py-1.5 text-xs rounded-lg transition-colors {toolSelectionMode === 'all' ? 'bg-violet-600 text-white' : 'bg-muted text-foreground hover:bg-muted/80'}"
									>
										All Tools
									</button>
									<button
										type="button"
										onclick={() => toolSelectionMode = 'select'}
										class="px-3 py-1.5 text-xs rounded-lg transition-colors {toolSelectionMode === 'select' ? 'bg-violet-600 text-white' : 'bg-muted text-foreground hover:bg-muted/80'}"
									>
										Select Tools
									</button>
								</div>
								<p class="text-xs text-muted-foreground mt-1">
									{#if toolSelectionMode === 'all'}
										Subagent can use all tools available to the profile.
									{:else}
										Subagent can ONLY use the selected tools below.
									{/if}
								</p>
							</div>

							<!-- Tool categories (only shown in select mode) -->
							{#if toolSelectionMode === 'select'}
								<div class="border-t border-border pt-3 space-y-2">
									{#each availableTools.categories as category}
										{#if category.tools.length > 0}
											<div class="border border-border rounded-lg overflow-hidden">
												<!-- Category header -->
												<div class="w-full px-3 py-2 bg-muted/50 flex items-center gap-2 text-sm text-foreground">
													<!-- Custom checkbox with indeterminate state -->
													<button
														type="button"
														onclick={() => toggleFormCategory(category)}
														class="w-4 h-4 rounded flex items-center justify-center transition-colors {isCategoryFullySelected(category) ? 'bg-violet-600' : isCategoryPartiallySelected(category) ? 'bg-violet-600' : 'bg-muted border border-border'}"
													>
														{#if isCategoryFullySelected(category)}
															<svg class="w-3 h-3 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
																<path stroke-linecap="round" stroke-linejoin="round" stroke-width="3" d="M5 13l4 4L19 7" />
															</svg>
														{:else if isCategoryPartiallySelected(category)}
															<svg class="w-3 h-3 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
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
													<div class="p-2 space-y-1 bg-card">
														{#each category.tools as tool}
															<label class="flex items-center gap-2 px-2 py-1 rounded hover:bg-muted/50 cursor-pointer">
																<input
																	type="checkbox"
																	checked={formTools.includes(tool.name)}
																	onchange={() => toggleFormTool(tool.name)}
																	class="w-4 h-4 rounded bg-muted border-0 text-violet-600 focus:ring-ring"
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
				<div class="border border-border rounded-lg overflow-hidden">
					<button
						type="button"
						onclick={() => toolCategoriesExpanded['_systemPrompt'] = !toolCategoriesExpanded['_systemPrompt']}
						class="w-full px-3 py-2 bg-accent flex items-center justify-between text-sm text-foreground hover:bg-muted"
					>
						<span>System Prompt</span>
						<svg class="w-4 h-4 transition-transform {toolCategoriesExpanded['_systemPrompt'] ? 'rotate-180' : ''}" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
						</svg>
					</button>
					{#if toolCategoriesExpanded['_systemPrompt']}
						<div class="p-3 bg-card">
							<textarea
								bind:value={formPrompt}
								placeholder="You are a specialized agent..."
								rows={6}
								class="w-full bg-muted border-0 rounded-lg px-3 py-2 text-sm text-foreground resize-none"
							></textarea>
						</div>
					{/if}
				</div>

				<!-- Action Buttons -->
				<div class="flex gap-2 pt-4">
					<button
						onclick={() => showEditor = false}
						class="flex-1 px-4 py-2 bg-muted text-foreground rounded-lg hover:bg-accent"
					>
						Cancel
					</button>
					<button
						onclick={handleSave}
						disabled={!isValid() || saving}
						class="flex-1 px-4 py-2 bg-primary text-foreground rounded-lg hover:opacity-90 disabled:opacity-50 disabled:cursor-not-allowed"
					>
						{#if saving}
							<span class="inline-flex items-center gap-2">
								<svg class="w-4 h-4 animate-spin" fill="none" viewBox="0 0 24 24">
									<circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
									<path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
								</svg>
								Saving...
							</span>
						{:else}
							Save
						{/if}
					</button>
				</div>
			</div>
		</div>
	</div>
{/if}

<!-- Delete Confirmation -->
{#if deletingId}
	<div
		class="fixed inset-0 bg-black/50 z-[60] flex items-center justify-center p-4"
		onclick={() => deletingId = null}
		role="dialog"
		aria-modal="true"
	>
		<!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
		<div class="bg-card rounded-xl w-full max-w-sm shadow-lg border border-border" onclick={(e) => e.stopPropagation()}>
			<div class="p-4 border-b border-border">
				<h3 class="text-lg font-semibold text-foreground">Delete Subagent</h3>
			</div>
			<div class="p-4">
				<p class="text-sm text-muted-foreground mb-4">
					Are you sure you want to delete <strong class="text-foreground">{subagents.find(s => s.id === deletingId)?.name}</strong>? This action cannot be undone.
				</p>
				<div class="flex gap-2">
					<button
						onclick={() => deletingId = null}
						disabled={deleteLoading}
						class="flex-1 px-4 py-2 bg-muted text-foreground rounded-lg hover:bg-accent"
					>
						Cancel
					</button>
					<button
						onclick={confirmDelete}
						disabled={deleteLoading}
						class="flex-1 px-4 py-2 bg-red-500 text-white rounded-lg hover:bg-red-600 disabled:opacity-50 flex items-center justify-center gap-2"
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
