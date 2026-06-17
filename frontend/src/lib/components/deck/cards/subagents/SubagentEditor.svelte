<script lang="ts">
	import { Bot, Plus, Trash2, X, Check, Settings, Terminal, User, Wrench, ChevronRight } from 'lucide-svelte';
	import { groups } from '$lib/stores/groups';
	import type { SubagentExport } from '$lib/api/client';
	import AIGenerateDialog from '../AIGenerateDialog.svelte';
	import FeaturePageLayout from '$lib/components/layout/FeaturePageLayout.svelte';
	import {
		getModels,
		CUSTOM_VALUE,
		INHERIT_VALUE,
		getBaseModel,
		has1MContext,
		withContextSuffix,
		modelSupports1M
	} from '$lib/constants/models';
	import '../card-design-system.css';

	interface Subagent {
		id: string;
		name: string;
		description: string;
		prompt: string;
		tools?: string[];
		model?: string;
		is_builtin: boolean;
		is_modified: boolean;
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

	type EditorTab = 'identity' | 'config' | 'prompt';

	// Model options come from the shared registry. Subagents prepend the
	// "Inherit" sentinel to let an agent fall back to the session's model.

	interface Props {
		editingSubagent: Subagent | null;
		isEditMode: boolean;
		error: string | null;
		saving: boolean;
		// Form state
		formId: string;
		onFormIdChange: (value: string) => void;
		formName: string;
		onFormNameChange: (value: string) => void;
		formDescription: string;
		onFormDescriptionChange: (value: string) => void;
		formPrompt: string;
		onFormPromptChange: (value: string) => void;
		formTools: string[];
		onFormToolsChange: (value: string[]) => void;
		formModel: string;
		onFormModelChange: (value: string) => void;
		customModelMode: boolean;
		onCustomModelModeChange: (custom: boolean, model: string) => void;
		toolSelectionMode: 'all' | 'select';
		onToolSelectionModeChange: (value: 'all' | 'select') => void;
		availableTools: ToolsResponse;
		expandedSections: Record<string, boolean>;
		onToggleSection: (section: string) => void;
		// Group state
		creatingGroup: boolean;
		newGroupName: string;
		onNewGroupNameChange: (value: string) => void;
		onStartGroupCreate: () => void;
		onConfirmGroupCreate: (agentId: string) => void;
		onCancelGroupCreate: () => void;
		onAssignToGroup: (agentId: string, groupName: string) => void;
		onRemoveFromGroup: (agentId: string) => void;
		// Actions
		onBack: () => void;
		onSave: () => void;
		onClearError: () => void;
		isValid: () => boolean;
		showGenerateDialog: boolean;
		onShowGenerateDialogChange: (value: boolean) => void;
		onGeneratedSubagent: (data: SubagentExport | unknown) => void;
	}

	let {
		editingSubagent,
		isEditMode,
		error,
		saving,
		formId,
		onFormIdChange,
		formName,
		onFormNameChange,
		formDescription,
		onFormDescriptionChange,
		formPrompt,
		onFormPromptChange,
		formTools,
		onFormToolsChange,
		formModel,
		onFormModelChange,
		customModelMode,
		onCustomModelModeChange,
		toolSelectionMode,
		onToolSelectionModeChange,
		availableTools,
		expandedSections,
		onToggleSection,
		creatingGroup,
		newGroupName,
		onNewGroupNameChange,
		onStartGroupCreate,
		onConfirmGroupCreate,
		onCancelGroupCreate,
		onAssignToGroup,
		onRemoveFromGroup,
		onBack,
		onSave,
		onClearError,
		isValid,
		showGenerateDialog,
		onShowGenerateDialogChange,
		onGeneratedSubagent,
	}: Props = $props();

	let activeTab = $state<EditorTab>('identity');

	const tabs: { id: EditorTab; label: string; icon: typeof User }[] = [
		{ id: 'identity', label: 'Identity', icon: User },
		{ id: 'config', label: 'Tools', icon: Wrench },
		{ id: 'prompt', label: 'Prompt', icon: Terminal }
	];

	function toggleTool(toolName: string) {
		if (formTools.includes(toolName)) {
			onFormToolsChange(formTools.filter(t => t !== toolName));
		} else {
			onFormToolsChange([...formTools, toolName]);
		}
	}

	function selectAllInCategory(category: ToolCategory) {
		const toolNames = category.tools.map(t => t.name);
		const allSelected = toolNames.every(name => formTools.includes(name));

		if (allSelected) {
			onFormToolsChange(formTools.filter(t => !toolNames.includes(t)));
		} else {
			const newTools = toolNames.filter(name => !formTools.includes(name));
			onFormToolsChange([...formTools, ...newTools]);
		}
	}

	function getCategorySelectionState(category: ToolCategory): 'none' | 'partial' | 'all' {
		const toolNames = category.tools.map(t => t.name);
		const selectedCount = toolNames.filter(name => formTools.includes(name)).length;
		if (selectedCount === 0) return 'none';
		if (selectedCount === toolNames.length) return 'all';
		return 'partial';
	}
</script>

<FeaturePageLayout
	icon={Bot}
	title="Subagents"
	breadcrumbs={[
		{ label: 'Subagents', onclick: onBack },
		{ label: isEditMode ? (editingSubagent?.name || 'Edit') : 'New Subagent' }
	]}
>
	{#snippet footer()}
		<button type="button" class="pf-btn-save" onclick={onSave} disabled={!isValid() || saving}>
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
			<button onclick={onClearError}>
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
						value={formId}
						oninput={(e) => onFormIdChange(e.currentTarget.value)}
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
					value={formName}
					oninput={(e) => onFormNameChange(e.currentTarget.value)}
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
					value={formDescription}
					oninput={(e) => onFormDescriptionChange(e.currentTarget.value)}
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
				<div class="agent-model-row">
					<select
						id="agent-model"
						class="card-form-input"
						value={customModelMode ? CUSTOM_VALUE : getBaseModel(formModel)}
						onchange={(e) => {
							const v = (e.target as HTMLSelectElement).value;
							if (v === CUSTOM_VALUE) {
								onCustomModelModeChange(true, '');
							} else {
								onCustomModelModeChange(false, withContextSuffix(v, modelSupports1M(v) && has1MContext(formModel)));
							}
						}}
					>
						<option value={INHERIT_VALUE}>Inherit</option>
						{#each getModels() as model (model.value)}
							<option value={model.value}>{model.label}</option>
						{/each}
						<option value={CUSTOM_VALUE}>Custom...</option>
					</select>
					{#if !customModelMode && modelSupports1M(formModel)}
						<label class="agent-1m-toggle">
							<input
								type="checkbox"
								class="checkbox-input"
								checked={has1MContext(formModel)}
								onchange={(e) => onFormModelChange(withContextSuffix(formModel, (e.target as HTMLInputElement).checked))}
							/>
							<span class="checkbox-label">1M context</span>
						</label>
					{/if}
				</div>
				{#if customModelMode}
					<input
						type="text"
						value={formModel}
						oninput={(e) => onFormModelChange((e.target as HTMLInputElement).value)}
						class="card-form-input card-form-input--mono"
						placeholder="e.g. claude-opus-4-6"
						style="margin-top: 6px;"
					/>
				{/if}
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
									if (v) onAssignToGroup(editingSubagent.id, v);
									else onRemoveFromGroup(editingSubagent.id);
								}
							}}
						>
							<option value="">No group</option>
							{#each $groups.subagents.groups as group}
								<option value={group.name}>{group.name}</option>
							{/each}
						</select>
						{#if creatingGroup}
							<button type="button" class="pf-btn-new-group" onclick={() => editingSubagent && onConfirmGroupCreate(editingSubagent.id)} title="Create">
								<Check size={14} />
							</button>
						{:else}
							<button type="button" class="pf-btn-new-group" onclick={onStartGroupCreate} title="New group">
								<Plus size={14} />
							</button>
							{#if ($groups.subagents.assignments[editingSubagent.id] ?? "")}
								<button type="button" class="pf-btn-new-group pf-btn-new-group--delete" onclick={() => { const g = $groups.subagents.assignments[editingSubagent?.id ?? ""]; if (g) { onRemoveFromGroup(editingSubagent!.id); groups.deleteGroup("subagents", g); } }} title="Delete group">
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
							value={newGroupName}
							oninput={(e) => onNewGroupNameChange(e.currentTarget.value)}
							onkeydown={(e) => {
								if (e.key === 'Enter') { e.preventDefault(); editingSubagent && onConfirmGroupCreate(editingSubagent.id); }
								if (e.key === 'Escape') { e.preventDefault(); onCancelGroupCreate(); }
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
				<select class="pf-select" value={toolSelectionMode} onchange={(e) => { const v = (e.target as HTMLSelectElement).value as 'all' | 'select'; onToolSelectionModeChange(v); if (v === 'all') onFormToolsChange([]); }}>
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
						<button type="button" class="pf-category-card" onclick={() => onToggleSection('sdk')}>
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
				value={formPrompt}
				oninput={(e) => onFormPromptChange(e.currentTarget.value)}
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
		onClose={() => onShowGenerateDialogChange(false)}
		onGenerated={onGeneratedSubagent}
	/>
</FeaturePageLayout>

<style>
	.flex-1 {
		flex: 1;
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

	/* Model select + 1M context toggle, side by side */
	.agent-model-row {
		display: flex;
		align-items: center;
		gap: 12px;
	}

	.agent-model-row .card-form-input {
		flex: 1;
		min-width: 0;
	}

	/* 1M context toggle (inline, right of model select) */
	.agent-1m-toggle {
		display: flex;
		align-items: center;
		gap: 8px;
		flex-shrink: 0;
		white-space: nowrap;
		cursor: pointer;
	}

	.checkbox-input {
		width: 16px;
		height: 16px;
		accent-color: var(--primary);
		color-scheme: dark;
	}

	.checkbox-label {
		font-size: var(--text-md, 0.875rem);
		color: var(--text-primary);
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
