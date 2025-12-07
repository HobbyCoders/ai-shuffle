<script lang="ts">
	/**
	 * SubagentManager - Global subagent management panel for main UI
	 *
	 * Displays all subagents in a sidebar panel with full CRUD operations.
	 * Responsive design for mobile (full screen) and desktop (sidebar).
	 */
	import { api } from '$lib/api/client';

	// Available tools
	const AVAILABLE_TOOLS = [
		'Read', 'Write', 'Edit', 'Grep', 'Glob', 'Bash',
		'WebFetch', 'WebSearch', 'Task', 'NotebookEdit'
	];

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
	}

	let { onClose }: Props = $props();

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
		showEditor = true;
	}

	// Toggle tool selection
	function toggleTool(tool: string) {
		if (formTools.includes(tool)) {
			formTools = formTools.filter(t => t !== tool);
		} else {
			formTools = [...formTools, tool];
		}
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
			if (formTools.length > 0) body.tools = formTools;
			if (formModel) body.model = formModel;

			if (editingSubagent) {
				await api.put(`/subagents/${editingSubagent.id}`, body);
			} else {
				body.id = formId;
				await api.post('/subagents', body);
			}

			showEditor = false;
			await loadSubagents();
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
	onclick={(e) => e.target === e.currentTarget && onClose()}
	role="dialog"
	aria-modal="true"
>
	<!-- Modal content -->
	<div class="bg-card rounded-xl w-full max-w-2xl max-h-[85vh] overflow-hidden flex flex-col shadow-xl border border-border">
		<!-- Header -->
		<div class="flex items-center justify-between px-4 py-3 border-b border-border bg-muted/30">
			<div class="flex items-center gap-2">
				<svg class="w-5 h-5 text-primary" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0zm6 3a2 2 0 11-4 0 2 2 0 014 0zM7 10a2 2 0 11-4 0 2 2 0 014 0z" />
				</svg>
				<h2 class="text-lg font-semibold text-foreground">Subagents</h2>
				<span class="text-sm text-muted-foreground">({subagents.length})</span>
			</div>
			<div class="flex items-center gap-2">
				<!-- Hidden file input for import -->
				<input type="file" accept=".json" bind:this={importInput} onchange={handleImport} class="hidden" />

				<button
					onclick={triggerImport}
					disabled={importing}
					class="flex items-center gap-1 px-2.5 py-1.5 text-sm font-medium text-foreground bg-muted border border-border rounded-md hover:bg-accent transition-colors"
					title="Import subagent from file"
				>
					{#if importing}
						<svg class="w-4 h-4 animate-spin" fill="none" viewBox="0 0 24 24">
							<circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
							<path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
						</svg>
					{:else}
						<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-8l-4-4m0 0L8 8m4-4v12" />
						</svg>
					{/if}
					<span>Import</span>
				</button>
				<button
					onclick={handleCreate}
					class="flex items-center gap-1 px-2.5 py-1.5 text-sm font-medium text-primary-foreground bg-primary rounded-md hover:bg-primary/90 transition-colors"
				>
					<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4" />
					</svg>
					<span>Add</span>
				</button>
				<button
					onclick={onClose}
					class="p-1.5 hover:bg-muted rounded-md transition-colors"
					aria-label="Close"
				>
					<svg class="w-5 h-5 text-muted-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
					</svg>
				</button>
			</div>
		</div>

		<!-- Content -->
		<div class="flex-1 overflow-y-auto p-4">
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
			<div class="text-center py-12">
				<div class="w-16 h-16 mx-auto mb-4 rounded-full bg-muted flex items-center justify-center">
					<svg class="w-8 h-8 text-muted-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0zm6 3a2 2 0 11-4 0 2 2 0 014 0zM7 10a2 2 0 11-4 0 2 2 0 014 0z" />
					</svg>
				</div>
				<p class="text-muted-foreground mb-2">No subagents configured</p>
				<p class="text-sm text-muted-foreground mb-4">Subagents are specialized helpers that Claude can invoke for specific tasks.</p>
				<button onclick={handleCreate} class="text-primary hover:underline">
					Create your first subagent
				</button>
			</div>
		{:else}
			<div class="space-y-3">
				{#each subagents as agent (agent.id)}
					<div class="border border-border rounded-lg overflow-hidden bg-card hover:border-primary/50 transition-colors">
						<!-- Agent header - always visible -->
						<button
							class="w-full px-4 py-3 text-left"
							onclick={() => expandedId = expandedId === agent.id ? null : agent.id}
						>
							<div class="flex items-start justify-between gap-2">
								<div class="flex-1 min-w-0">
									<div class="flex items-center gap-2 flex-wrap">
										<h3 class="font-medium text-foreground">{agent.name}</h3>
										<span class="px-1.5 py-0.5 text-xs font-medium bg-primary/10 text-primary rounded">
											{getModelDisplay(agent.model)}
										</span>
									</div>
									<p class="text-sm text-muted-foreground mt-0.5 line-clamp-2">
										{agent.description}
									</p>
								</div>
								<svg
									class="w-4 h-4 text-muted-foreground transition-transform {expandedId === agent.id ? 'rotate-180' : ''}"
									fill="none"
									stroke="currentColor"
									viewBox="0 0 24 24"
								>
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
								</svg>
							</div>
						</button>

						<!-- Expanded content -->
						{#if expandedId === agent.id}
							<div class="px-4 pb-3 space-y-3 border-t border-border pt-3">
								<!-- Tools -->
								{#if agent.tools && agent.tools.length > 0}
									<div>
										<p class="text-xs font-medium text-muted-foreground mb-1">Tools</p>
										<div class="flex flex-wrap gap-1">
											{#each agent.tools as tool}
												<span class="px-1.5 py-0.5 text-xs bg-muted text-muted-foreground rounded">
													{tool}
												</span>
											{/each}
										</div>
									</div>
								{:else}
									<p class="text-xs text-muted-foreground">All tools available</p>
								{/if}

								<!-- Prompt preview -->
								<div>
									<p class="text-xs font-medium text-muted-foreground mb-1">System Prompt</p>
									<pre class="text-xs text-muted-foreground bg-muted/50 p-2 rounded max-h-24 overflow-y-auto whitespace-pre-wrap font-mono">{agent.prompt.slice(0, 300)}{agent.prompt.length > 300 ? '...' : ''}</pre>
								</div>

								<!-- Actions -->
								<div class="flex items-center gap-2 pt-1">
									<button
										onclick={() => exportSubagent(agent.id)}
										class="px-3 py-1.5 text-sm font-medium text-foreground bg-muted rounded-md hover:bg-muted/80 transition-colors flex items-center gap-1"
										title="Export subagent"
									>
										<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" />
										</svg>
										Export
									</button>
									<button
										onclick={() => handleEdit(agent)}
										class="flex-1 px-3 py-1.5 text-sm font-medium text-foreground bg-muted rounded-md hover:bg-muted/80 transition-colors"
									>
										Edit
									</button>
									<button
										onclick={() => handleDeleteClick(agent.id)}
										class="px-3 py-1.5 text-sm font-medium text-red-500 bg-red-500/10 rounded-md hover:bg-red-500/20 transition-colors"
									>
										Delete
									</button>
								</div>
							</div>
						{/if}
					</div>
				{/each}
			</div>
		{/if}
	</div>
	</div>
</div>

<!-- Editor Modal -->
{#if showEditor}
	<div
		class="fixed inset-0 bg-black/50 z-[60] flex items-center justify-center p-4"
		onclick={(e) => e.target === e.currentTarget && (showEditor = false)}
		role="dialog"
		aria-modal="true"
	>
		<div class="bg-card border border-border rounded-lg shadow-xl w-full max-w-2xl max-h-[90vh] overflow-hidden flex flex-col">
			<!-- Header -->
			<div class="flex items-center justify-between px-4 py-3 border-b border-border bg-muted/30">
				<h2 class="text-lg font-semibold text-foreground">
					{editingSubagent ? 'Edit Subagent' : 'Create Subagent'}
				</h2>
				<button
					onclick={() => showEditor = false}
					class="p-1 hover:bg-muted rounded-md transition-colors"
				>
					<svg class="w-5 h-5 text-muted-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
					</svg>
				</button>
			</div>

			<!-- Content -->
			<div class="flex-1 overflow-y-auto p-4 space-y-4">
				{#if error}
					<div class="p-3 bg-red-500/10 border border-red-500/20 rounded-md text-red-500 text-sm">
						{error}
					</div>
				{/if}

				<!-- ID (only for new) -->
				{#if !editingSubagent}
					<div class="space-y-1.5">
						<label for="agent-id" class="block text-sm font-medium text-foreground">ID</label>
						<input
							id="agent-id"
							type="text"
							bind:value={formId}
							placeholder="my-agent-id"
							class="w-full px-3 py-2 bg-background border border-border rounded-md text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/50"
						/>
						<p class="text-xs text-muted-foreground">Lowercase letters, numbers, and hyphens only</p>
					</div>
				{/if}

				<!-- Name -->
				<div class="space-y-1.5">
					<label for="agent-name" class="block text-sm font-medium text-foreground">Name</label>
					<input
						id="agent-name"
						type="text"
						bind:value={formName}
						placeholder="Research Assistant"
						class="w-full px-3 py-2 bg-background border border-border rounded-md text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/50"
					/>
				</div>

				<!-- Description -->
				<div class="space-y-1.5">
					<label for="agent-description" class="block text-sm font-medium text-foreground">Description</label>
					<input
						id="agent-description"
						type="text"
						bind:value={formDescription}
						placeholder="When to use this agent..."
						class="w-full px-3 py-2 bg-background border border-border rounded-md text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/50"
					/>
					<p class="text-xs text-muted-foreground">Describes when Claude should invoke this subagent</p>
				</div>

				<!-- Model -->
				<div class="space-y-1.5">
					<label for="agent-model" class="block text-sm font-medium text-foreground">Model</label>
					<select
						id="agent-model"
						bind:value={formModel}
						class="w-full px-3 py-2 bg-background border border-border rounded-md text-foreground focus:outline-none focus:ring-2 focus:ring-primary/50"
					>
						{#each MODEL_OPTIONS as opt}
							<option value={opt.value}>{opt.label}</option>
						{/each}
					</select>
				</div>

				<!-- Tools -->
				<div class="space-y-2">
					<div class="flex items-center justify-between">
						<label class="block text-sm font-medium text-foreground">Allowed Tools</label>
						<div class="flex gap-2 text-xs">
							<button type="button" onclick={() => formTools = [...AVAILABLE_TOOLS]} class="text-primary hover:underline">All</button>
							<span class="text-muted-foreground">|</span>
							<button type="button" onclick={() => formTools = []} class="text-primary hover:underline">Clear</button>
						</div>
					</div>
					<div class="flex flex-wrap gap-2">
						{#each AVAILABLE_TOOLS as tool}
							<button
								type="button"
								onclick={() => toggleTool(tool)}
								class="px-2.5 py-1 text-sm rounded-md border transition-colors {formTools.includes(tool)
									? 'bg-primary text-primary-foreground border-primary'
									: 'bg-background text-foreground border-border hover:bg-muted'}"
							>
								{tool}
							</button>
						{/each}
					</div>
					<p class="text-xs text-muted-foreground">
						{formTools.length === 0 ? 'All tools available (inherits from profile)' : `${formTools.length} tool(s) selected`}
					</p>
				</div>

				<!-- Prompt -->
				<div class="space-y-1.5">
					<label for="agent-prompt" class="block text-sm font-medium text-foreground">System Prompt</label>
					<textarea
						id="agent-prompt"
						bind:value={formPrompt}
						placeholder="You are a specialized agent..."
						rows={8}
						class="w-full px-3 py-2 bg-background border border-border rounded-md text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/50 font-mono text-sm resize-y"
					></textarea>
				</div>
			</div>

			<!-- Footer -->
			<div class="flex flex-col-reverse sm:flex-row items-stretch sm:items-center justify-end gap-2 px-4 py-3 border-t border-border bg-muted/30">
				<button
					type="button"
					onclick={() => showEditor = false}
					class="px-4 py-2 text-sm font-medium text-foreground bg-background border border-border rounded-md hover:bg-muted transition-colors"
				>
					Cancel
				</button>
				<button
					type="button"
					onclick={handleSave}
					disabled={!isValid() || saving}
					class="px-4 py-2 text-sm font-medium text-primary-foreground bg-primary rounded-md hover:bg-primary/90 transition-colors disabled:opacity-50 disabled:cursor-not-allowed flex items-center justify-center gap-2"
				>
					{#if saving}
						<svg class="w-4 h-4 animate-spin" fill="none" viewBox="0 0 24 24">
							<circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
							<path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
						</svg>
					{/if}
					{editingSubagent ? 'Save Changes' : 'Create Subagent'}
				</button>
			</div>
		</div>
	</div>
{/if}

<!-- Delete Confirmation -->
{#if deletingId}
	<div
		class="fixed inset-0 bg-black/50 z-[60] flex items-center justify-center p-4"
		onclick={(e) => e.target === e.currentTarget && (deletingId = null)}
		role="dialog"
		aria-modal="true"
	>
		<div class="bg-card border border-border rounded-lg shadow-xl w-full max-w-sm p-4">
			<h3 class="text-lg font-semibold text-foreground mb-2">Delete Subagent</h3>
			<p class="text-muted-foreground mb-4">
				Are you sure you want to delete <strong>{subagents.find(s => s.id === deletingId)?.name}</strong>? This action cannot be undone.
			</p>
			<div class="flex justify-end gap-2">
				<button
					onclick={() => deletingId = null}
					disabled={deleteLoading}
					class="px-3 py-1.5 text-sm font-medium text-foreground bg-background border border-border rounded-md hover:bg-muted transition-colors"
				>
					Cancel
				</button>
				<button
					onclick={confirmDelete}
					disabled={deleteLoading}
					class="px-3 py-1.5 text-sm font-medium text-white bg-red-500 rounded-md hover:bg-red-600 transition-colors disabled:opacity-50 flex items-center gap-2"
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
{/if}
