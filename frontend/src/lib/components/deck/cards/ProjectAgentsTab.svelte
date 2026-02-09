<script lang="ts">
	/**
	 * ProjectAgentsTab - Agent management tab for project detail view
	 *
	 * Features:
	 * - View agents in .claude/agents/
	 * - Create/edit/delete file-based agents
	 * - 3-tab editor (Identity, Config, Prompt) matching SubagentCard UI
	 * - AI generation support
	 */
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
	import type { SubagentExport } from '$lib/api/client';
	import { Search, Bot, Plus, Trash2, X, ArrowLeft, User, Settings, Terminal, Zap } from 'lucide-svelte';
	import AIGenerateDialog from './AIGenerateDialog.svelte';
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
	let formModel = $state<string>('');
	let formTools = $state<string[]>([]);
	let formPrompt = $state('');

	// Search state
	let searchQuery = $state('');

	// Delete confirmation
	let deletingAgent = $state<string | null>(null);
	let deleteLoading = $state(false);

	// AI Generate state
	let showGenerateDialog = $state(false);

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

	// Validation per tab
	const identityValid = $derived(() => {
		if (isCreating && !formName.match(/^[a-zA-Z][a-zA-Z0-9_-]*$/)) return false;
		if (isCreating && formName.length === 0) return false;
		if (formDescription.length === 0) return false;
		return true;
	});

	const configValid = $derived(() => true);
	const promptValid = $derived(() => formPrompt.length > 0);
	const isValid = $derived(() => identityValid() && configValid() && promptValid());

	// Model options
	const MODEL_OPTIONS = [
		{ value: '', label: 'Inherit', description: 'Use profile default' },
		{ value: 'haiku', label: 'Haiku', description: 'Fast & cheap' },
		{ value: 'sonnet', label: 'Sonnet', description: 'Balanced' },
		{ value: 'opus', label: 'Opus', description: 'Most capable' }
	];

	function getModelBadgeClass(model?: string | null): string {
		if (!model || model === '') return 'card-badge--inherit';
		return `card-badge--${model}`;
	}

	function getModelDisplay(model?: string | null): string {
		const opt = MODEL_OPTIONS.find(o => o.value === model);
		return opt?.label || 'Inherit';
	}

	// Reset form
	function resetForm() {
		formName = '';
		formDescription = '';
		formModel = '';
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
			formModel = $selectedProjectAgent.model || '';
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
		if (!isValid()) return;

		try {
			if (isCreating) {
				const data: CreateProjectAgentRequest = {
					name: formName.trim(),
					description: formDescription.trim(),
					prompt: formPrompt,
					model: formModel || null,
					tools: formTools.length > 0 ? formTools : undefined
				};
				await projectAgents.createAgent(projectId, data);
			} else {
				const data: UpdateProjectAgentRequest = {
					description: formDescription.trim(),
					prompt: formPrompt,
					model: formModel || null,
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
		deleteLoading = true;
		try {
			await projectAgents.deleteAgent(projectId, agentName);
			deletingAgent = null;
		} catch (e) {
			console.error('Failed to delete agent:', e);
		} finally {
			deleteLoading = false;
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

	// Handle AI-generated subagent
	function handleGeneratedSubagent(data: SubagentExport | unknown) {
		showGenerateDialog = false;
		const sub = (data as SubagentExport).subagent;

		// Populate form with generated data
		isCreating = true;
		formName = sub.id || sub.name.toLowerCase().replace(/\s+/g, '-');
		formDescription = sub.description;
		formPrompt = sub.prompt;
		formTools = sub.tools || [];
		formModel = sub.model || '';

		viewMode = 'editor';
		editorTab = 'identity';
	}

	// Editor tabs config
	const tabs: { id: EditorTab; label: string; icon: typeof User }[] = [
		{ id: 'identity', label: 'Identity', icon: User },
		{ id: 'config', label: 'Config', icon: Settings },
		{ id: 'prompt', label: 'Prompt', icon: Terminal }
	];
</script>

<div class="card-system agents-tab">
	{#if $projectAgentsLoading && viewMode === 'list'}
		<div class="card-content">
			<div class="loading-container">
				<div class="card-spinner"></div>
			</div>
		</div>
	{:else if viewMode === 'editor'}
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
						{isCreating ? 'New Agent' : 'Edit Agent'}
					</span>
					{#if !isCreating && formName}
						<span class="card-header-subtitle">{formName}</span>
					{/if}
				</div>
			</div>
		</div>

		<!-- Tab Navigation -->
		<div class="card-tabs">
			{#each tabs as tab}
				<button
					onclick={() => editorTab = tab.id}
					class="card-tab"
					class:card-tab--active={editorTab === tab.id}
				>
					<tab.icon />
					<span class="tab-label">{tab.label}</span>
				</button>
			{/each}
		</div>

		<!-- Tab Content -->
		<div class="card-content">
			<div class="centered-content">
				{#if $projectAgentsError}
					<div class="card-error-banner">
						<span class="flex-1">{$projectAgentsError}</span>
						<button onclick={() => projectAgents.clearError()}>
							<X class="w-4 h-4" />
						</button>
					</div>
				{/if}

				<!-- Identity Tab -->
				{#if editorTab === 'identity'}
					<div class="editor-tab-content">
						{#if isCreating}
							<div class="card-form-section">
								<label for="agent-name" class="card-form-label">
									Name <span class="required">*</span>
								</label>
								<input
									id="agent-name"
									type="text"
									bind:value={formName}
									placeholder="my-agent"
									class="card-form-input card-form-input--mono"
								/>
								<p class="card-form-hint">
									Used as filename: {formName || 'name'}.md
								</p>
							</div>
						{/if}

						<div class="card-form-section">
							<label for="agent-description" class="card-form-label">
								Description <span class="required">*</span>
							</label>
							<textarea
								id="agent-description"
								bind:value={formDescription}
								placeholder="When should this agent be invoked? What does it do?"
								rows={3}
								class="card-form-input card-form-textarea"
							></textarea>
							<p class="card-form-hint">
								Helps Claude understand when to use this agent.
							</p>
						</div>
					</div>
				{/if}

				<!-- Config Tab -->
				{#if editorTab === 'config'}
					<div class="editor-tab-content">
						<!-- Model Selection -->
						<div class="card-form-section">
							<label class="card-form-label">
								Model — {#if formModel === ''}use profile default{:else if formModel === 'haiku'}fastest and most compact{:else if formModel === 'sonnet'}balanced performance{:else if formModel === 'opus'}most capable{/if}
							</label>
							<div class="card-mode-selector">
								{#each MODEL_OPTIONS as opt}
									<button
										type="button"
										class="card-mode-btn"
										class:card-mode-btn--active={formModel === opt.value}
										onclick={() => formModel = opt.value}
									>
										{opt.label}
									</button>
								{/each}
							</div>
						</div>

						<!-- Tool Selection -->
						<div class="card-form-section">
							<div class="section-header">
								<label class="card-form-label">Tools</label>
								<span class="card-badge card-badge--primary">
									{formTools.length === 0 ? 'All' : formTools.length + ' selected'}
								</span>
							</div>
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
							<p class="card-form-hint">
								{formTools.length === 0 ? 'Agent can use all available tools.' : 'Agent can ONLY use selected tools.'}
							</p>
						</div>
					</div>
				{/if}

				<!-- Prompt Tab -->
				{#if editorTab === 'prompt'}
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
							Instructions that define the agent's behavior and capabilities.
						</p>
					</div>
				{/if}
			</div>
		</div>

		<!-- Footer -->
		<div class="card-footer">
			<div class="card-footer-content">
				<button
					onclick={handleBack}
					class="card-btn-secondary"
				>
					Cancel
				</button>
				<button
					onclick={handleSave}
					disabled={!isValid() || $projectAgentsSaving}
					class="card-btn-primary"
				>
					{#if $projectAgentsSaving}
						<div class="card-spinner card-spinner--small"></div>
						Saving...
					{:else}
						{isCreating ? 'Create' : 'Save Changes'}
					{/if}
				</button>
			</div>
		</div>
	{:else}
		<!-- LIST VIEW -->
		<!-- Search bar (show if > 3 agents) -->
		{#if $projectAgentCount > 3}
			<div class="card-search-wrapper">
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

		<!-- Content -->
		<div class="card-content">
			<div class="centered-content">
				{#if $projectAgentsError}
					<div class="card-error-banner">
						<span class="flex-1">{$projectAgentsError}</span>
						<button onclick={() => projectAgents.clearError()}>
							<X class="w-4 h-4" />
						</button>
					</div>
				{/if}

				{#if filteredAgents().length === 0}
					<div class="card-empty-state">
						{#if searchQuery}
							<Search class="card-empty-icon" />
							<p class="card-empty-title">No agents match "{searchQuery}"</p>
							<button onclick={() => (searchQuery = '')} class="card-empty-action">
								Clear search
							</button>
						{:else}
							<Bot class="card-empty-icon" />
							<p class="card-empty-title">No agents defined</p>
							<p class="card-empty-description">
								Create agents to add specialized capabilities to this project.
							</p>
						{/if}
					</div>
				{:else}
					{#each filteredAgents() as agent (agent.name)}
						{#if deletingAgent === agent.name}
							<!-- Inline delete confirmation -->
							<div class="card-list-item card-list-item--deleting">
								<div class="card-item-icon card-item-icon--destructive">
									<Trash2 />
								</div>
								<div class="card-item-content">
									<p class="delete-confirm-text">
										Delete <strong>{agent.name}</strong>? This cannot be undone.
									</p>
									<div class="delete-confirm-actions">
										<button
											onclick={() => (deletingAgent = null)}
											disabled={deleteLoading}
											class="card-btn-secondary"
										>
											Cancel
										</button>
										<button
											onclick={() => handleDelete(agent.name)}
											disabled={deleteLoading}
											class="card-btn-destructive"
										>
											{#if deleteLoading}
												<div class="card-spinner card-spinner--small"></div>
											{/if}
											Delete
										</button>
									</div>
								</div>
							</div>
						{:else}
							<!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
							<div
								class="card-list-item"
								onclick={() => handleEdit(agent.name)}
							>
								<div class="card-item-icon">
									<Bot />
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
									</div>
									<p class="card-item-description">{agent.description || 'No description'}</p>
								</div>
								<div class="card-item-actions">
									<button
										onclick={(e) => { e.stopPropagation(); deletingAgent = agent.name; }}
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
		</div>

		<!-- Footer -->
		<div class="card-footer">
			<div class="card-footer-content">
				<button
					onclick={handleCreate}
					class="card-btn-primary"
				>
					<Plus />
					New Agent
				</button>
				<button
					onclick={() => showGenerateDialog = true}
					class="card-btn-secondary"
					title="Generate with AI"
				>
					<Zap />
				</button>
			</div>
		</div>
	{/if}

	<!-- AI Generate Dialog -->
	<AIGenerateDialog
		type="subagent"
		isOpen={showGenerateDialog}
		onClose={() => showGenerateDialog = false}
		onGenerated={handleGeneratedSubagent}
	/>
</div>

<style>
	.agents-tab {
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

	/* Tools grid */
	.tools-grid {
		display: flex;
		flex-wrap: wrap;
		gap: var(--space-2);
	}

	.tool-chip {
		padding: var(--space-2) var(--space-3);
		font-size: var(--text-sm);
		border-radius: var(--radius-md);
		background: var(--surface-1);
		border: 1px solid var(--border-subtle);
		cursor: pointer;
		transition: all var(--transition-fast, 100ms);
		color: var(--text-primary);
	}

	.tool-chip:hover {
		border-color: var(--border-default);
		background: var(--surface-hover);
	}

	.tool-chip.active {
		background: var(--selection-bg-emphasis);
		color: var(--selection-text);
		border-color: var(--selection-ring);
		box-shadow: var(--selection-shadow);
	}

	/* Required indicator */
	.required {
		color: var(--destructive);
	}
</style>
