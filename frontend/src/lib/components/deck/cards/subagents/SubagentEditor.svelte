<script lang="ts">
	import { Plus, Trash2, X, Check, Settings, Terminal, User, Wrench, ChevronDown } from 'lucide-svelte';
	import { groups } from '$lib/stores/groups.svelte';
	import type { SubagentExport } from '$lib/api/client';
	import AIGenerateDialog from '../AIGenerateDialog.svelte';
	import StudioEditorShell from '$lib/components/layout/StudioEditorShell.svelte';
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

<StudioEditorShell
	title={isEditMode ? (editingSubagent?.name || 'Edit subagent') : 'New Subagent'}
	subtitle={isEditMode ? 'Subagent' : 'Create a new subagent'}
	backLabel="Subagents"
	{onBack}
	sections={tabs.map((t) => ({ id: t.id, label: t.label, icon: t.icon }))}
	active={activeTab}
	onSelect={(id) => (activeTab = id as EditorTab)}
	railLabel="Subagent sections"
>
	{#snippet actions()}
		<button type="button" class="ed-btn primary" onclick={onSave} disabled={!isValid() || saving}>
			{#if saving}
				<div class="card-spinner card-spinner--small"></div>
				<span>Saving…</span>
			{:else}
				<Check size={14} />
				<span>{isEditMode ? 'Save' : 'Create'}</span>
			{/if}
		</button>
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
		<div class="set-group">
			<div class="set-grouphead"><h3><User />Identity</h3></div>
			{#if !isEditMode}
				<div class="set-row">
					<div class="lab"><h4>ID <span class="req">*</span></h4><p>Lowercase letters, numbers, and hyphens only.</p></div>
					<div class="ctl"><input type="text" value={formId} oninput={(e) => onFormIdChange(e.currentTarget.value)} placeholder="my-agent-id" class="inp mono" /></div>
				</div>
			{/if}
			<div class="set-row">
				<div class="lab"><h4>Display name <span class="req">*</span></h4><p>Shown in the agent picker.</p></div>
				<div class="ctl"><input type="text" value={formName} oninput={(e) => onFormNameChange(e.currentTarget.value)} placeholder="Research Assistant" class="inp" /></div>
			</div>
			<div class="set-row stack">
				<div class="lab"><h4>Description <span class="req">*</span></h4><p>Describes when Claude should invoke this subagent.</p></div>
				<div class="ctl">
					<textarea value={formDescription} oninput={(e) => onFormDescriptionChange(e.currentTarget.value)} placeholder="Use this agent when you need to research topics…" rows={3} class="fld"></textarea>
				</div>
			</div>
			<div class="set-row">
				<div class="lab"><h4>Model</h4><p>Defaults to the session model when set to Inherit.</p></div>
				<div class="ctl ctl-col">
					<div class="grp-row">
						<select
							class="sel"
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
							<option value={CUSTOM_VALUE}>Custom…</option>
						</select>
						{#if !customModelMode && modelSupports1M(formModel)}
							<label class="chk">
								<input type="checkbox" checked={has1MContext(formModel)} onchange={(e) => onFormModelChange(withContextSuffix(formModel, (e.target as HTMLInputElement).checked))} />
								<span>1M context</span>
							</label>
						{/if}
					</div>
					{#if customModelMode}
						<input type="text" value={formModel} oninput={(e) => onFormModelChange((e.target as HTMLInputElement).value)} class="inp mono" placeholder="e.g. claude-opus-4-6" />
					{/if}
				</div>
			</div>
			{#if isEditMode && editingSubagent}
				<div class="set-row">
					<div class="lab"><h4>Group</h4><p>Organize related subagents together.</p></div>
					<div class="ctl ctl-col">
						<div class="grp-row">
							<select
								class="sel"
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
								<button type="button" class="icon-btn" onclick={() => editingSubagent && onConfirmGroupCreate(editingSubagent.id)} title="Create">
									<Check size={14} />
								</button>
							{:else}
								<button type="button" class="icon-btn" onclick={onStartGroupCreate} title="New group">
									<Plus size={14} />
								</button>
								{#if ($groups.subagents.assignments[editingSubagent.id] ?? '')}
									<button type="button" class="icon-btn danger" onclick={() => { const g = $groups.subagents.assignments[editingSubagent?.id ?? '']; if (g) { onRemoveFromGroup(editingSubagent!.id); groups.deleteGroup('subagents', g); } }} title="Delete group">
										<Trash2 size={14} />
									</button>
								{/if}
							{/if}
						</div>
						{#if creatingGroup}
							<!-- svelte-ignore a11y_autofocus -->
							<input
								type="text"
								class="inp"
								placeholder="Group name…"
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
		<div class="set-group">
			<div class="set-grouphead"><h3><Wrench />Tool access</h3><p>Control which tools this subagent can use.</p></div>
			<div class="set-row">
				<div class="lab"><h4>Mode</h4><p>Inherit all tools, or curate a custom selection.</p></div>
				<div class="ctl">
					<select class="sel" value={toolSelectionMode} onchange={(e) => { const v = (e.target as HTMLSelectElement).value as 'all' | 'select'; onToolSelectionModeChange(v); if (v === 'all') onFormToolsChange([]); }}>
						<option value="all">All Tools</option>
						<option value="select">Custom Selection</option>
					</select>
				</div>
			</div>
		</div>

		{#if toolSelectionMode === 'select'}
			<div class="set-group">
				<div class="set-grouphead"><h3><Settings />Tool categories</h3><p>Select which tools this subagent can access.</p></div>
				<div class="cat-list">
					<button type="button" class="cat-card" onclick={() => onToggleSection('sdk')}>
						<div class="cat-icon"><Wrench size={17} /></div>
						<div class="cat-info">
							<span class="cat-name">Built-in tools</span>
							<span class="cat-desc">{availableTools.categories.map((c) => c.name).join(', ')}</span>
						</div>
						<span class="pill-s acc">{formTools.length}/{availableTools.all_tools.length}</span>
						<ChevronDown size={16} class={expandedSections.sdk ? 'cat-chev open' : 'cat-chev'} />
					</button>
					{#if expandedSections.sdk}
						<div class="cat-body">
							{#each availableTools.categories as category}
								{#if category.tools.length > 0}
									{@const state = getCategorySelectionState(category)}
									<div class="catgroup">
										<div class="catgroup-head">
											<span class="catgroup-name">{category.name} <span class="hint">{category.tools.length}</span></span>
											<button type="button" class="lnk" onclick={() => selectAllInCategory(category)}>{state === 'all' ? 'Clear' : 'Select all'}</button>
										</div>
										<div class="chips-select">
											{#each category.tools as tool}
												{@const sel = formTools.includes(tool.name)}
												<button type="button" class="chsel" class:on={sel} title={tool.description} onclick={() => toggleTool(tool.name)}>
													{#if sel}<Check size={12} />{/if}
													{tool.name}
												</button>
											{/each}
										</div>
									</div>
								{/if}
							{/each}
							<div class="sel-count">{formTools.length} tool{formTools.length !== 1 ? 's' : ''} selected</div>
						</div>
					{/if}
				</div>
			</div>
		{:else}
			<div class="ed-note"><Wrench /><span>All {availableTools.all_tools.length} tools are available to this subagent.</span></div>
		{/if}
	{/if}

	<!-- Prompt Tab -->
	{#if activeTab === 'prompt'}
		<div class="set-group">
			<div class="set-grouphead"><h3><Terminal />System prompt</h3><p>Instructions that define the subagent's behavior and capabilities.</p></div>
			<div class="set-row stack">
				<div class="lab lab-row">
					<h4>Prompt <span class="req">*</span></h4>
					<span class="count">{formPrompt.length} characters</span>
				</div>
				<div class="ctl">
					<textarea
						value={formPrompt}
						oninput={(e) => onFormPromptChange(e.currentTarget.value)}
						placeholder="You are a specialized agent that helps users with…"
						class="fld code prompt-area"
					></textarea>
				</div>
			</div>
		</div>
	{/if}

	<!-- AI Generate Dialog -->
	<AIGenerateDialog
		type="subagent"
		isOpen={showGenerateDialog}
		onClose={() => onShowGenerateDialogChange(false)}
		onGenerated={onGeneratedSubagent}
	/>
</StudioEditorShell>

<style>
	.flex-1 {
		flex: 1;
	}

	/* Required asterisk */
	.req {
		color: var(--destructive);
		font-weight: 700;
	}

	/* Stacked label header (prompt char count) */
	.lab-row {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		gap: 12px;
	}
	.count {
		font-family: var(--font-mono, monospace);
		font-size: 11px;
		color: var(--muted-foreground);
		flex-shrink: 0;
	}
	textarea.prompt-area {
		min-height: 220px;
	}

	/* Stacked control with helper below */
	.ctl-col {
		flex-direction: column;
		align-items: stretch;
		gap: 8px;
	}
	.ctl-col :global(.inp),
	.ctl-col :global(.sel) {
		width: 100%;
	}

	/* Select + adjacent control row */
	.grp-row {
		display: flex;
		align-items: center;
		gap: 8px;
		width: 100%;
	}
	.grp-row :global(.sel) {
		flex: 1;
		min-width: 0;
	}

	/* Inline checkbox (1M context) */
	.chk {
		display: inline-flex;
		align-items: center;
		gap: 7px;
		font-size: 12.5px;
		color: var(--muted-foreground);
		white-space: nowrap;
		cursor: pointer;
		flex-shrink: 0;
	}
	.chk input {
		width: 15px;
		height: 15px;
		accent-color: var(--primary);
		color-scheme: dark;
	}

	/* Square icon button (group new/delete) */
	.icon-btn {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 36px;
		height: 36px;
		flex-shrink: 0;
		background: var(--background);
		border: 1px solid var(--border);
		border-radius: 9px;
		color: var(--muted-foreground);
		cursor: pointer;
		transition: background 0.12s, color 0.12s, border-color 0.12s;
	}
	.icon-btn:hover {
		background: var(--muted);
		color: var(--foreground);
	}
	.icon-btn.danger {
		color: var(--destructive);
	}
	.icon-btn.danger:hover {
		background: color-mix(in oklch, var(--destructive) 14%, transparent);
		border-color: color-mix(in oklch, var(--destructive) 30%, transparent);
	}

	/* Tool categories */
	.cat-list {
		display: flex;
		flex-direction: column;
		gap: 10px;
		padding: 14px 18px 18px;
	}
	.cat-card {
		display: flex;
		align-items: center;
		gap: 12px;
		width: 100%;
		padding: 12px 14px;
		background: var(--background);
		border: 1px solid var(--border);
		border-radius: 10px;
		cursor: pointer;
		text-align: left;
		color: var(--foreground);
		transition: background 0.12s, border-color 0.12s;
	}
	.cat-card:hover {
		background: var(--muted);
	}
	.cat-icon {
		width: 34px;
		height: 34px;
		display: flex;
		align-items: center;
		justify-content: center;
		background: var(--muted);
		border-radius: 8px;
		color: var(--foreground);
		flex-shrink: 0;
	}
	.cat-info {
		flex: 1;
		min-width: 0;
		display: flex;
		flex-direction: column;
		gap: 2px;
	}
	.cat-name {
		font-size: 13.5px;
		font-weight: 600;
	}
	.cat-desc {
		font-size: 11.5px;
		color: var(--muted-foreground);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.cat-card :global(.cat-chev) {
		color: var(--muted-foreground);
		flex-shrink: 0;
		transition: transform 0.18s;
	}
	.cat-card :global(.cat-chev.open) {
		transform: rotate(180deg);
	}
	.cat-body {
		display: flex;
		flex-direction: column;
		gap: 14px;
		padding: 14px;
		margin-top: -4px;
		border: 1px solid var(--border);
		border-radius: 10px;
		background: color-mix(in oklch, var(--muted) 35%, transparent);
	}
	.catgroup {
		display: flex;
		flex-direction: column;
		gap: 9px;
	}
	.catgroup-head {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 12px;
	}
	.catgroup-name {
		font-size: 12.5px;
		font-weight: 600;
		color: var(--foreground);
		display: inline-flex;
		align-items: center;
		gap: 7px;
	}
	.lnk {
		background: none;
		border: none;
		padding: 0;
		font: inherit;
		font-size: 12px;
		font-weight: 600;
		color: var(--foreground);
		cursor: pointer;
	}
	.lnk:hover {
		text-decoration: underline;
	}
	.sel-count {
		font-size: 12px;
		color: var(--muted-foreground);
		padding-top: 12px;
		border-top: 1px solid var(--border);
	}
</style>
