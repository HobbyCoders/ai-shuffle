<script lang="ts">
	/**
	 * ProjectAgentsTab - Agent management tab for project detail view
	 *
	 * Features:
	 * - View agents in .claude/agents/
	 * - Create/edit/delete file-based agents
	 * - 3-tab editor (Identity, Config, Prompt)
	 */
	import { onMount } from 'svelte';
	import {
		projectAgents,
		allProjectAgents,
		selectedProjectAgent,
		projectAgentsLoading,
		projectAgentsSaving,
		projectAgentsError,
		projectAgentCount
	} from '$lib/stores/projectAgents';
	import type { CreateProjectAgentRequest, UpdateProjectAgentRequest } from '$lib/api/projectAgents';
	import { Search, Bot, Plus, Trash2, X, Loader2, Save, ChevronLeft } from 'lucide-svelte';
	import './card-design-system.css';

	interface Props {
		projectId: string;
	}

	let { projectId }: Props = $props();

	// View state
	type ViewMode = 'list' | 'editor';
	let viewMode = $state<ViewMode>('list');
	let isCreating = $state(false);

	// Editor state
	type EditorTab = 'identity' | 'config' | 'prompt';
	let editorTab = $state<EditorTab>('identity');

	// Form state
	let formName = $state('');
	let formDescription = $state('');
	let formModel = $state<string | null>(null);
	let formTools = $state<string[]>([]);
	let formPrompt = $state('');

	// Search state
	let searchQuery = $state('');

	// Delete confirmation
	let deletingAgent = $state<string | null>(null);

	// Load agents when project changes
	$effect(() => {
		if (projectId) {
			projectAgents.loadAgents(projectId);
		}
	});

	// Filter agents
	const filteredAgents = $derived(() => {
		if (!searchQuery.trim()) return $allProjectAgents;
		const q = searchQuery.toLowerCase();
		return $allProjectAgents.filter(
			(a) =>
				a.name.toLowerCase().includes(q) ||
				a.description.toLowerCase().includes(q)
		);
	});

	// Available models
	const models = [
		{ value: null, label: 'Inherit (default)' },
		{ value: 'haiku', label: 'Haiku' },
		{ value: 'sonnet', label: 'Sonnet' },
		{ value: 'opus', label: 'Opus' }
	];

	// Reset form
	function resetForm() {
		formName = '';
		formDescription = '';
		formModel = null;
		formTools = [];
		formPrompt = '';
		editorTab = 'identity';
	}

	// Open editor for new agent
	function handleCreate() {
		resetForm();
		isCreating = true;
		viewMode = 'editor';
	}

	// Open editor for existing agent
	async function handleEdit(agentName: string) {
		isCreating = false;
		await projectAgents.selectAgent(projectId, agentName);

		if ($selectedProjectAgent) {
			formName = $selectedProjectAgent.name;
			formDescription = $selectedProjectAgent.description;
			formModel = $selectedProjectAgent.model;
			formTools = $selectedProjectAgent.tools || [];
			formPrompt = $selectedProjectAgent.prompt;
		}

		viewMode = 'editor';
		editorTab = 'identity';
	}

	// Back to list
	function handleBack() {
		viewMode = 'list';
		resetForm();
		projectAgents.clearSelectedAgent();
	}

	// Save agent
	async function handleSave() {
		if (!formName.trim() || !formDescription.trim() || !formPrompt.trim()) return;

		try {
			if (isCreating) {
				const data: CreateProjectAgentRequest = {
					name: formName.trim(),
					description: formDescription.trim(),
					prompt: formPrompt,
					model: formModel || undefined,
					tools: formTools.length > 0 ? formTools : undefined
				};
				await projectAgents.createAgent(projectId, data);
			} else {
				const data: UpdateProjectAgentRequest = {
					description: formDescription.trim(),
					prompt: formPrompt,
					model: formModel || undefined,
					tools: formTools.length > 0 ? formTools : undefined
				};
				await projectAgents.updateAgent(projectId, formName, data);
			}
			handleBack();
		} catch (e) {
			console.error('Failed to save agent:', e);
		}
	}

	// Delete agent
	async function handleDelete(agentName: string) {
		try {
			await projectAgents.deleteAgent(projectId, agentName);
			deletingAgent = null;
		} catch (e) {
			console.error('Failed to delete agent:', e);
		}
	}

	// Toggle tool selection
	function toggleTool(tool: string) {
		if (formTools.includes(tool)) {
			formTools = formTools.filter((t) => t !== tool);
		} else {
			formTools = [...formTools, tool];
		}
	}

	// Common tools
	const commonTools = [
		'Read', 'Write', 'Edit', 'Glob', 'Grep', 'Bash',
		'WebFetch', 'WebSearch', 'Task', 'TodoWrite'
	];
</script>

<div class="agents-tab">
	{#if $projectAgentsLoading && viewMode === 'list'}
		<div class="card-empty-state">
			<Loader2 class="card-empty-icon animate-spin" />
			<p class="card-empty-title">Loading agents...</p>
		</div>
	{:else if $projectAgentsError}
		<div class="card-error-banner">
			<span class="flex-1">{$projectAgentsError}</span>
			<button onclick={() => projectAgents.clearError()}>
				<X class="w-4 h-4" />
			</button>
		</div>
	{:else if viewMode === 'editor'}
		<!-- Editor View -->
		<div class="editor-view">
			<div class="editor-header">
				<button onclick={handleBack} class="card-btn-secondary">
					<ChevronLeft class="w-4 h-4" />
					Back
				</button>
				<span class="editor-title">{isCreating ? 'New Agent' : formName}</span>
				<button
					onclick={handleSave}
					disabled={$projectAgentsSaving || !formName.trim() || !formDescription.trim() || !formPrompt.trim()}
					class="card-btn-primary"
				>
					{#if $projectAgentsSaving}
						<Loader2 class="w-4 h-4 animate-spin" />
					{:else}
						<Save class="w-4 h-4" />
					{/if}
					Save
				</button>
			</div>

			<!-- Editor tabs -->
			<div class="editor-tabs">
				<button
					class="editor-tab"
					class:active={editorTab === 'identity'}
					onclick={() => (editorTab = 'identity')}
				>
					Identity
				</button>
				<button
					class="editor-tab"
					class:active={editorTab === 'config'}
					onclick={() => (editorTab = 'config')}
				>
					Config
				</button>
				<button
					class="editor-tab"
					class:active={editorTab === 'prompt'}
					onclick={() => (editorTab = 'prompt')}
				>
					Prompt
				</button>
			</div>

			<!-- Tab content -->
			<div class="editor-content">
				{#if editorTab === 'identity'}
					<div class="form-section">
						{#if isCreating}
							<div class="form-field">
								<label class="card-form-label">Name</label>
								<input
									bind:value={formName}
									class="card-form-input card-form-input--mono"
									placeholder="my-agent"
								/>
								<span class="form-hint">Used as filename: {formName || 'name'}.md</span>
							</div>
						{/if}
						<div class="form-field">
							<label class="card-form-label">Description</label>
							<textarea
								bind:value={formDescription}
								class="card-form-input card-form-textarea"
								rows="3"
								placeholder="When should this agent be invoked? What does it do?"
							></textarea>
							<span class="form-hint">Helps Claude understand when to use this agent</span>
						</div>
					</div>
				{:else if editorTab === 'config'}
					<div class="form-section">
						<div class="form-field">
							<label class="card-form-label">Model</label>
							<select bind:value={formModel} class="card-form-input">
								{#each models as model}
									<option value={model.value}>{model.label}</option>
								{/each}
							</select>
						</div>
						<div class="form-field">
							<label class="card-form-label">Tools</label>
							<div class="tools-grid">
								{#each commonTools as tool}
									<button
										type="button"
										class="tool-chip"
										class:active={formTools.includes(tool)}
										onclick={() => toggleTool(tool)}
									>
										{tool}
									</button>
								{/each}
							</div>
							<span class="form-hint">
								{formTools.length === 0 ? 'All tools (default)' : `${formTools.length} tools selected`}
							</span>
						</div>
					</div>
				{:else if editorTab === 'prompt'}
					<div class="form-section form-section--full">
						<div class="form-field form-field--full">
							<label class="card-form-label">System Prompt</label>
							<textarea
								bind:value={formPrompt}
								class="card-form-input card-form-textarea prompt-textarea"
								placeholder="You are a specialized agent that..."
							></textarea>
							<span class="form-hint">{formPrompt.length} characters</span>
						</div>
					</div>
				{/if}
			</div>
		</div>
	{:else}
		<!-- List View -->
		{#if $projectAgentCount > 3}
			<div class="tab-search">
				<div class="card-search-container">
					<Search class="card-search-icon" />
					<input
						type="text"
						bind:value={searchQuery}
						placeholder="Search agents..."
						class="card-search-input"
					/>
				</div>
			</div>
		{/if}

		<div class="list-view">
			{#if filteredAgents().length === 0}
				<div class="card-empty-state">
					<Bot class="card-empty-icon" />
					<p class="card-empty-title">No agents defined</p>
					<p class="card-empty-description">
						Create agents to add specialized capabilities to this project.
					</p>
				</div>
			{:else}
				{#each filteredAgents() as agent (agent.name)}
					{#if deletingAgent === agent.name}
						<!-- Delete confirmation -->
						<div class="card-list-item card-list-item--deleting">
							<div class="card-item-icon card-item-icon--destructive">
								<Trash2 />
							</div>
							<div class="card-item-content">
								<p class="delete-confirm-text">
									Delete <strong>{agent.name}</strong>? This cannot be undone.
								</p>
								<div class="delete-confirm-actions">
									<button onclick={() => (deletingAgent = null)} class="card-btn-secondary">
										Cancel
									</button>
									<button onclick={() => handleDelete(agent.name)} class="card-btn-destructive">
										Delete
									</button>
								</div>
							</div>
						</div>
					{:else}
						<!-- svelte-ignore a11y_no_static_element_interactions a11y_click_events_have_key_events -->
						<div
							class="card-list-item card-list-item--clickable"
							onclick={() => handleEdit(agent.name)}
							role="button"
							tabindex="0"
							onkeydown={(e) => e.key === 'Enter' && handleEdit(agent.name)}
						>
							<div class="card-item-icon">
								<Bot />
							</div>
							<div class="card-item-content">
								<div class="card-item-header">
									<span class="card-item-name">{agent.name}</span>
									{#if agent.model}
										<span class="card-badge card-badge--primary">{agent.model}</span>
									{/if}
								</div>
								<span class="card-item-description">{agent.description || 'No description'}</span>
							</div>
							<div class="card-item-actions" onclick={(e) => e.stopPropagation()}>
								<button
									onclick={() => (deletingAgent = agent.name)}
									class="card-action-btn card-action-btn--destructive"
									title="Delete"
								>
									<Trash2 />
								</button>
							</div>
						</div>
					{/if}
				{/each}
			{/if}
		</div>

		<div class="tab-footer">
			<button onclick={handleCreate} class="card-btn-primary">
				<Plus />
				New Agent
			</button>
		</div>
	{/if}
</div>

<style>
	.agents-tab {
		display: flex;
		flex-direction: column;
		height: 100%;
	}

	.tab-search {
		padding: 0 var(--space-4) var(--space-3);
	}

	.list-view {
		flex: 1;
		overflow-y: auto;
		padding: 0 var(--space-4);
		display: flex;
		flex-direction: column;
		gap: var(--space-2);
	}

	.tab-footer {
		padding: var(--space-3) var(--space-4);
		border-top: 1px solid var(--border);
	}

	/* Editor styles */
	.editor-view {
		display: flex;
		flex-direction: column;
		height: 100%;
	}

	.editor-header {
		display: flex;
		align-items: center;
		gap: var(--space-3);
		padding: var(--space-3) var(--space-4);
		border-bottom: 1px solid var(--border);
	}

	.editor-title {
		flex: 1;
		font-weight: 500;
		font-size: var(--text-base);
	}

	.editor-tabs {
		display: flex;
		gap: var(--space-1);
		padding: var(--space-2) var(--space-4);
		border-bottom: 1px solid var(--border);
		background: var(--muted);
	}

	.editor-tab {
		padding: var(--space-2) var(--space-3);
		font-size: var(--text-sm);
		border-radius: var(--radius);
		background: transparent;
		border: none;
		cursor: pointer;
		color: var(--muted-foreground);
		transition: all 0.15s ease;
	}

	.editor-tab:hover {
		color: var(--foreground);
	}

	.editor-tab.active {
		background: var(--background);
		color: var(--foreground);
		font-weight: 500;
	}

	.editor-content {
		flex: 1;
		overflow-y: auto;
		padding: var(--space-4);
	}

	.form-section {
		display: flex;
		flex-direction: column;
		gap: var(--space-4);
	}

	.form-section--full {
		height: 100%;
	}

	.form-field {
		display: flex;
		flex-direction: column;
		gap: var(--space-1);
	}

	.form-field--full {
		flex: 1;
		display: flex;
		flex-direction: column;
	}

	.form-hint {
		font-size: var(--text-xs);
		color: var(--muted-foreground);
	}

	.prompt-textarea {
		flex: 1;
		min-height: 300px;
		font-family: var(--font-mono);
		font-size: var(--text-sm);
	}

	.tools-grid {
		display: flex;
		flex-wrap: wrap;
		gap: var(--space-2);
	}

	.tool-chip {
		padding: var(--space-1) var(--space-2);
		font-size: var(--text-sm);
		border-radius: var(--radius);
		background: var(--muted);
		border: 1px solid var(--border);
		cursor: pointer;
		transition: all 0.15s ease;
	}

	.tool-chip:hover {
		border-color: var(--border-hover);
	}

	.tool-chip.active {
		background: var(--primary);
		color: var(--primary-foreground);
		border-color: var(--primary);
	}

	.card-list-item--clickable {
		cursor: pointer;
		width: 100%;
		text-align: left;
		background: transparent;
		border: none;
	}

	.card-list-item--clickable:hover {
		background: var(--muted);
	}

	.flex-1 {
		flex: 1;
	}

	.animate-spin {
		animation: spin 1s linear infinite;
	}

	@keyframes spin {
		from {
			transform: rotate(0deg);
		}
		to {
			transform: rotate(360deg);
		}
	}
</style>
