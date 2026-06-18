<script lang="ts">
	/**
	 * ProfileForm — Form tab content for create/edit profile
	 * Renders the active tab's content (general, tools, agents, prompt, advanced).
	 * The parent handles FeaturePageLayout, tab bar, and footer.
	 */
	import type { Profile } from '$lib/api/client';
	import { layout } from '$lib/stores/layout.svelte';
	import { Users, Settings, Wrench, Bot, FileText, SlidersHorizontal, Check, Plus, Trash2 } from 'lucide-svelte';
	import ProfileToolConfig from './ProfileToolConfig.svelte';
	import ProfileSystemPrompt from './ProfileSystemPrompt.svelte';
	import {
		getModels,
		CUSTOM_VALUE,
		getBaseModel,
		has1MContext,
		withContextSuffix,
		modelSupports1M
	} from '$lib/constants/models';
	import { onMount } from 'svelte';
	import { getComputerUseStatus } from '$lib/api/computerUse';

	// Host capability for the Computer Use toggle. Native-only: unavailable in
	// Docker/headless, which we surface so the toggle can be greyed out.
	let computerUse = $state<{ available: boolean; reason: string }>({ available: true, reason: 'ok' });
	onMount(async () => {
		try {
			const s = await getComputerUseStatus();
			computerUse = { available: s.available, reason: s.reason };
		} catch {
			// Leave optimistic default; backend still no-ops if unavailable.
		}
	});

	const COMPUTER_USE_REASONS: Record<string, string> = {
		docker: 'Unavailable: AI Shuffle is running in Docker, which cannot control the host desktop. Run it natively to use this.',
		'no-display': 'Unavailable: no desktop display detected on this host.',
		'no-monitors': 'Unavailable: no monitors detected on this host.',
		'missing-deps': 'Unavailable: the mss/pynput packages are not installed on the server.',
		'env-disabled': 'Unavailable: disabled by the COMPUTER_USE_DISABLED environment variable.',
		'screenshot-probe-failed': 'Unavailable: the screen-capture probe failed on this host.'
	};

	interface ToolCategory {
		id: string;
		name: string;
		tools: { name: string; description: string; mcp_server?: string }[];
	}

	interface AITool {
		id: string;
		name: string;
		description: string;
		category: string;
		available: boolean;
		providers: string[];
		active_provider: string | null;
	}

	interface AIToolCategory {
		id: string;
		name: string;
		tools: AITool[];
	}

	interface Subagent {
		id: string;
		name: string;
		description: string;
		model?: string;
		is_builtin: boolean;
	}

	interface GroupState {
		profiles?: {
			groups?: { name: string }[];
			assignments?: Record<string, string>;
		};
	}

	interface ProfileFormData {
		id: string;
		name: string;
		description: string;
		model: string;
		permission_mode: string;
		max_turns: number | null;
		allowed_tools: string[];
		disallowed_tools: string[];
		enabled_agents: string[];
		include_partial_messages: boolean;
		thinking_mode: 'default' | 'disabled' | 'adaptive' | 'think' | 'think_harder' | 'ultra_think';
		effort: 'low' | 'medium' | 'high' | 'max' | 'xhigh' | null;
		system_prompt_type: string;
		system_prompt_preset: string;
		system_prompt_append: string;
		system_prompt_content: string;
		system_prompt_inject_env: boolean;
		enabled_ai_tools: string[];
		setting_sources: string[];
		cwd: string;
		add_dirs: string;
		user: string;
		max_buffer_size: number | null;
		env_vars: { key: string; value: string }[];
		enable_file_checkpointing: boolean;
		app_config_tools: boolean;
		strict_mcp_config: boolean;
		computer_use_enabled: boolean;
	}

	interface EnvVarPreset {
		key: string;
		placeholder: string;
	}

	interface Props {
		activeTab: string;
		editingProfile: Profile | null;
		profileForm: ProfileFormData;
		toolSelectionMode: 'all' | 'none' | 'allow' | 'disallow';
		customModelMode: boolean;
		groups: GroupState;
		allSubagents: Subagent[];
		builtinAgentIds: string[];
		customAgents: Subagent[];
		sdkToolCategories: ToolCategory[];
		aiToolCategories: AIToolCategory[];
		aiToolsLoading: boolean;
		enabledSdkToolCount: number;
		aiToolCount: number;
		effectiveToolCount: number;
		expandedSections: Record<string, boolean>;
		envVarPresets: EnvVarPreset[];
		onFormChange: (field: string, value: any) => void;
		onToolSelectionModeChange: (mode: 'all' | 'none' | 'allow' | 'disallow') => void;
		onCustomModelModeChange: (custom: boolean, model: string) => void;
		onToggleTool: (toolName: string) => void;
		onToggleCategory: (category: { name: string; tools: { name: string }[] }) => void;
		onToggleAITool: (toolId: string) => void;
		onToggleAIToolCategory: (category: AIToolCategory) => void;
		onToggleSection: (id: string) => void;
		onToggleAgent: (agentId: string) => void;
		onToggleSettingSource: (source: string) => void;
		onAssignToGroup: (profileId: string, groupName: string) => void;
		onRemoveFromGroup: (profileId: string) => void;
		onCreateGroup: (category: string, name: string) => void;
		onDeleteGroup: (category: string, name: string) => void;
		getCategorySelectionState: (category: { name: string; tools: { name: string }[] }) => 'all' | 'some' | 'none';
		getAICategorySelectionState: (category: AIToolCategory) => 'all' | 'some' | 'none';
		getModelDisplay: (model?: string) => string;
	}

	let {
		activeTab,
		editingProfile,
		profileForm,
		toolSelectionMode,
		customModelMode,
		groups,
		allSubagents,
		builtinAgentIds,
		customAgents,
		sdkToolCategories,
		aiToolCategories,
		aiToolsLoading,
		enabledSdkToolCount,
		aiToolCount,
		effectiveToolCount,
		expandedSections,
		envVarPresets,
		onFormChange,
		onToolSelectionModeChange,
		onCustomModelModeChange,
		onToggleTool,
		onToggleCategory,
		onToggleAITool,
		onToggleAIToolCategory,
		onToggleSection,
		onToggleAgent,
		onToggleSettingSource,
		onAssignToGroup,
		onRemoveFromGroup,
		onCreateGroup,
		onDeleteGroup,
		getCategorySelectionState,
		getAICategorySelectionState,
		getModelDisplay
	}: Props = $props();

	// Group creation local state
	let creatingGroup = $state(false);
	let newGroupName = $state('');

	function startGroupCreate() {
		creatingGroup = true;
		newGroupName = '';
	}

	function confirmGroupCreate(profileId: string) {
		const name = newGroupName.trim();
		if (name) {
			onCreateGroup('profiles', name);
			onAssignToGroup(profileId, name);
		}
		creatingGroup = false;
		newGroupName = '';
	}

	function cancelGroupCreate() {
		creatingGroup = false;
		newGroupName = '';
	}
</script>

<div class="card-system pf-content">
	{#if activeTab === 'general'}
	<!-- GENERAL TAB -->
		<div class="pf-section">
			<div class="pf-section-header">
				<Users size={16} />
				<h3>Profile Information</h3>
			</div>
			<div class="pf-field-grid">
				<div class="pf-field">
					<label class="pf-label" for="profile-name">Name <span class="required">*</span></label>
					<input id="profile-name" value={profileForm.name} oninput={(e) => onFormChange('name', (e.target as HTMLInputElement).value)} class="pf-input" placeholder="My Custom Agent" />
				</div>
				<div class="pf-field">
					<label class="pf-label" for="profile-description">Description</label>
					<input id="profile-description" value={profileForm.description} oninput={(e) => onFormChange('description', (e.target as HTMLInputElement).value)} class="pf-input" placeholder="A coding assistant for frontend work" />
				</div>
				{#if editingProfile}
					<div class="pf-field">
						<label class="pf-label" for="profile-group">Group</label>
						<div class="pf-group-wrapper">
						<div class="pf-group-row">
							<select
								id="profile-group"
								class="pf-select"
								value={groups?.profiles?.assignments?.[editingProfile.id] ?? ''}
								onchange={(e) => {
									const v = (e.target as HTMLSelectElement).value;
									if (editingProfile) {
										if (v) onAssignToGroup(editingProfile.id, v);
										else onRemoveFromGroup(editingProfile.id);
									}
								}}
							>
								<option value="">No group</option>
								{#each groups?.profiles?.groups ?? [] as group}
									<option value={group.name}>{group.name}</option>
								{/each}
							</select>
							{#if creatingGroup}
								<button type="button" class="pf-btn-new-group" onclick={() => editingProfile && confirmGroupCreate(editingProfile.id)} title="Create">
									<Check size={14} />
								</button>
							{:else}
								<button type="button" class="pf-btn-new-group" onclick={startGroupCreate} title="New group">
									<Plus size={14} />
								</button>
								{#if editingProfile && (groups?.profiles?.assignments?.[editingProfile.id] ?? "")}
									<button type="button" class="pf-btn-new-group pf-btn-new-group--delete" onclick={() => { if (!editingProfile) return; const g = groups?.profiles?.assignments?.[editingProfile.id]; if (g) { onRemoveFromGroup(editingProfile.id); onDeleteGroup("profiles", g); } }} title="Delete group">
										<Trash2 size={14} />
									</button>
								{/if}
							{/if}
						</div>
						{#if creatingGroup}
							<!-- svelte-ignore a11y_autofocus -->
							<input
								type="text"
								class="pf-input pf-group-input"
								placeholder="Group name..."
								bind:value={newGroupName}
								onkeydown={(e) => {
									if (e.key === 'Enter') { e.preventDefault(); editingProfile && confirmGroupCreate(editingProfile.id); }
									if (e.key === 'Escape') { e.preventDefault(); cancelGroupCreate(); }
								}}
								autofocus
							/>
						{/if}
					</div>
					</div>
				{/if}
			</div>
		</div>

		<div class="pf-divider"></div>

		<div class="pf-section">
			<div class="pf-section-header">
				<Settings size={16} />
				<h3>Model & AI Settings</h3>
			</div>
			<div class="pf-field-grid">
				<div class="pf-field">
					<label class="pf-label" for="profile-model">Model</label>
					<div class="pf-model-row">
						<select id="profile-model" class="pf-select" value={customModelMode ? CUSTOM_VALUE : getBaseModel(profileForm.model)} onchange={(e) => { const v = (e.target as HTMLSelectElement).value; if (v === CUSTOM_VALUE) { onCustomModelModeChange(true, ''); } else { onCustomModelModeChange(false, withContextSuffix(v, modelSupports1M(v) && has1MContext(profileForm.model))); } }}>
							{#each getModels() as model (model.value)}
								<option value={model.value}>{model.label}</option>
							{/each}
							<option value={CUSTOM_VALUE}>Custom...</option>
						</select>
						{#if !customModelMode && modelSupports1M(profileForm.model)}
							<label class="inline-checkbox pf-1m-toggle">
								<input
									type="checkbox"
									class="checkbox-input"
									checked={has1MContext(profileForm.model)}
									onchange={(e) => onFormChange('model', withContextSuffix(profileForm.model, (e.target as HTMLInputElement).checked))}
								/>
								<span class="checkbox-label">1M context</span>
							</label>
						{/if}
					</div>
					{#if customModelMode}
						<input type="text" value={profileForm.model} oninput={(e) => onFormChange('model', (e.target as HTMLInputElement).value)} class="pf-input pf-input--mono" placeholder="e.g. claude-opus-4-7" style="margin-top: 6px;" />
					{/if}
				</div>
				<div class="pf-field">
					<label class="pf-label" for="profile-permission">Permission Mode</label>
					<select id="profile-permission" class="pf-select" value={profileForm.permission_mode} onchange={(e) => onFormChange('permission_mode', (e.target as HTMLSelectElement).value)}>
						<option value="default">Default</option>
						<option value="auto">Auto</option>
						<option value="acceptEdits">Accept Edits</option>
						<option value="plan">Plan</option>
						<option value="bypassPermissions">Bypass Permissions</option>
					</select>
				</div>
				<div class="pf-field">
					<label class="pf-label" for="profile-thinking">Thinking Mode</label>
					<select id="profile-thinking" class="pf-select" value={profileForm.thinking_mode} onchange={(e) => onFormChange('thinking_mode', (e.target as HTMLSelectElement).value)}>
						<option value="default">Default</option>
						<option value="disabled">Off</option>
						<option value="adaptive">Adaptive</option>
						<option value="think">8K Budget</option>
						<option value="think_harder">24K Budget</option>
						<option value="ultra_think">64K Budget</option>
					</select>
				</div>
				<div class="pf-field">
					<label class="pf-label" for="profile-effort">Effort Level</label>
					<select id="profile-effort" class="pf-select" value={profileForm.effort || ''} onchange={(e) => { const v = (e.target as HTMLSelectElement).value; onFormChange('effort', v ? v : null); }}>
						<option value="">Default</option>
						<option value="low">Low</option>
						<option value="medium">Medium</option>
						<option value="high">High</option>
						<option value="xhigh">XHigh</option>
						<option value="max">Max</option>
					</select>
				</div>
				<div class="pf-field">
					<label class="pf-label" for="profile-max-turns">Max Turns</label>
					<input id="profile-max-turns" type="number" value={profileForm.max_turns} oninput={(e) => { const v = (e.target as HTMLInputElement).value; onFormChange('max_turns', v ? Number(v) : null); }} class="pf-input" placeholder="Unlimited" />
				</div>
				</div>
		</div>

		<div class="pf-divider"></div>

		<div class="pf-section">
			<div class="pf-section-header">
				<SlidersHorizontal size={16} />
				<h3>Behavior</h3>
			</div>
			<div class="pf-toggle-row">
				<div class="pf-toggle-info">
					<span class="pf-toggle-title">Include Partial Messages</span>
					<span class="pf-toggle-desc">Stream partial text during AI generation for real-time feedback</span>
				</div>
				<button
					type="button"
					class="pf-toggle"
					class:active={profileForm.include_partial_messages}
					role="switch"
					aria-checked={profileForm.include_partial_messages}
					aria-label="Toggle include partial messages"
					onclick={() => onFormChange('include_partial_messages', !profileForm.include_partial_messages)}
				>
					<span class="pf-toggle-knob"></span>
				</button>
			</div>
			<div class="pf-toggle-row">
				<div class="pf-toggle-info">
					<span class="pf-toggle-title">File Checkpointing</span>
					<span class="pf-toggle-desc">Create restore points before file modifications.</span>
				</div>
				<button
					type="button"
					class="pf-toggle"
					class:active={profileForm.enable_file_checkpointing}
					role="switch"
					aria-checked={profileForm.enable_file_checkpointing}
					aria-label="Toggle file checkpointing"
					onclick={() => onFormChange('enable_file_checkpointing', !profileForm.enable_file_checkpointing)}
				>
					<span class="pf-toggle-knob"></span>
				</button>
			</div>
			<div class="pf-toggle-row">
				<div class="pf-toggle-info">
					<span class="pf-toggle-title">App Config via Chat</span>
					<span class="pf-toggle-desc">Allow AI to manage hooks, automations, MCP servers, and profiles through conversation. Admin only.</span>
				</div>
				<button
					type="button"
					class="pf-toggle"
					class:active={profileForm.app_config_tools}
					role="switch"
					aria-checked={profileForm.app_config_tools}
					aria-label="Toggle app configuration tools"
					onclick={() => onFormChange('app_config_tools', !profileForm.app_config_tools)}
				>
					<span class="pf-toggle-knob"></span>
				</button>
			</div>
			<!-- Slice F (SDK 0.1.81): strict MCP config — fail-fast on broken servers. -->
			<div class="pf-toggle-row">
				<div class="pf-toggle-info">
					<span class="pf-toggle-title">Strict MCP config</span>
					<span class="pf-toggle-desc">Fail-fast if any MCP server is misconfigured. Recommended for production profiles.</span>
				</div>
				<button
					type="button"
					class="pf-toggle"
					class:active={profileForm.strict_mcp_config}
					role="switch"
					aria-checked={profileForm.strict_mcp_config}
					aria-label="Toggle strict MCP config"
					onclick={() => onFormChange('strict_mcp_config', !profileForm.strict_mcp_config)}
				>
					<span class="pf-toggle-knob"></span>
				</button>
			</div>
			<!-- Computer Use: control the host desktop (screenshots, mouse, keyboard). -->
			<div class="pf-toggle-row" class:pf-toggle-row--disabled={!computerUse.available}>
				<div class="pf-toggle-info">
					<span class="pf-toggle-title">Computer Use</span>
					<span class="pf-toggle-desc">
						Let the agent control this machine's screen — screenshots, mouse, and keyboard,
						like Claude Desktop. Shares one cursor with you. Native (non-Docker) hosts only.
						{#if !computerUse.available}
							<br /><strong>{COMPUTER_USE_REASONS[computerUse.reason] ?? `Unavailable (${computerUse.reason}).`}</strong>
						{/if}
					</span>
				</div>
				<button
					type="button"
					class="pf-toggle"
					class:active={profileForm.computer_use_enabled}
					role="switch"
					aria-checked={profileForm.computer_use_enabled}
					aria-label="Toggle computer use"
					disabled={!computerUse.available}
					onclick={() => onFormChange('computer_use_enabled', !profileForm.computer_use_enabled)}
				>
					<span class="pf-toggle-knob"></span>
				</button>
			</div>
		</div>

	{:else if activeTab === 'tools'}
		<ProfileToolConfig
			{toolSelectionMode}
			allowedTools={profileForm.allowed_tools}
			disallowedTools={profileForm.disallowed_tools}
			enabledAITools={profileForm.enabled_ai_tools}
			{sdkToolCategories}
			{aiToolCategories}
			{aiToolsLoading}
			{enabledSdkToolCount}
			{aiToolCount}
			{effectiveToolCount}
			{expandedSections}
			{onToolSelectionModeChange}
			{onToggleTool}
			{onToggleCategory}
			{onToggleAITool}
			{onToggleAIToolCategory}
			{onToggleSection}
			{getCategorySelectionState}
			{getAICategorySelectionState}
		/>

	{:else if activeTab === 'agents'}
		<!-- AGENTS TAB -->
		<div class="pf-section">
			<div class="pf-section-header">
				<Bot size={16} />
				<h3>Sub-Agents</h3>
				{#if customAgents.length > 0}
					<div class="pf-section-actions">
						<button type="button" class="text-btn primary" onclick={() => onFormChange('enabled_agents', [...builtinAgentIds, ...customAgents.map((a) => a.id)])}>Select All</button>
						<button type="button" class="text-btn muted" onclick={() => onFormChange('enabled_agents', [...builtinAgentIds])}>Clear Custom</button>
					</div>
				{/if}
			</div>
			<p class="pf-desc">Built-in agents are always available. Choose which custom agents to enable.</p>
		</div>

		{#if allSubagents.length === 0}
			<div class="card-empty-state"><p class="card-empty-description">No agents configured</p></div>
		{:else}
			<div class="fp-list" style="padding: 0 20px;">
				{#each allSubagents as agent, index (agent.id)}
					{@const isBuiltin = agent.is_builtin}
					{@const isSelected = isBuiltin || profileForm.enabled_agents.includes(agent.id)}
					{#if index > 0}<div class="fp-list-divider"></div>{/if}
					<label class="fp-list-item pf-agent-row" class:pf-agent-row--selected={isSelected} class:pf-agent-row--builtin={isBuiltin}>
						<input type="checkbox" checked={isSelected} disabled={isBuiltin} onchange={() => onToggleAgent(agent.id)} class="pf-agent-checkbox" />
						<div class="fp-item-indicator" style={isSelected ? 'background: var(--primary)' : 'background: var(--text-tertiary); opacity: 0.3'}></div>
						<div class="fp-item-content">
							<div class="fp-item-header">
								<span class="fp-item-name">{agent.name}</span>
								{#if isBuiltin}
									<span class="fp-item-badge">Always on</span>
								{/if}
							</div>
							<div class="fp-item-meta">
								<span class="fp-meta-model">{getModelDisplay(agent.model)}</span>
							</div>
						</div>
					</label>
				{/each}
			</div>

			<!-- Agents Summary Bar -->
			<div class="pf-summary-bar">
				<div class="pf-summary-left">
					<Bot size={14} />
						<span>{builtinAgentIds.length} built-in{#if customAgents.length > 0} + {profileForm.enabled_agents.filter(id => !builtinAgentIds.includes(id) && customAgents.some(a => a.id === id)).length} of {customAgents.length} custom{/if}</span>
				</div>
				<button type="button" class="pf-summary-link" onclick={() => layout.openFeaturePage('subagent')}>Configure agents &rarr;</button>
			</div>
		{/if}

	{:else if activeTab === 'prompt'}
		<ProfileSystemPrompt
			systemPromptType={profileForm.system_prompt_type}
			systemPromptAppend={profileForm.system_prompt_append}
			systemPromptContent={profileForm.system_prompt_content}
			systemPromptInjectEnv={profileForm.system_prompt_inject_env}
			onSystemPromptTypeChange={(v) => onFormChange('system_prompt_type', v)}
			onSystemPromptAppendChange={(v) => onFormChange('system_prompt_append', v)}
			onSystemPromptContentChange={(v) => onFormChange('system_prompt_content', v)}
			onSystemPromptInjectEnvToggle={() => onFormChange('system_prompt_inject_env', !profileForm.system_prompt_inject_env)}
		/>

	{:else if activeTab === 'advanced'}
		<!-- ADVANCED TAB -->
		<div class="pf-section">
			<div class="pf-section-header">
				<SlidersHorizontal size={16} />
				<h3>Environment</h3>
			</div>
			<p class="pf-desc">Configure the working environment for this profile.</p>
			<div class="pf-field-grid">
				<div class="pf-field">
					<label class="pf-label" for="profile-cwd">Working Directory</label>
					<input id="profile-cwd" value={profileForm.cwd} oninput={(e) => onFormChange('cwd', (e.target as HTMLInputElement).value)} class="pf-input pf-input--mono" placeholder="/home/user/projects/my-api" />
				</div>
				<div class="pf-field">
					<label class="pf-label" for="profile-user">Run as User</label>
					<input id="profile-user" value={profileForm.user} oninput={(e) => onFormChange('user', (e.target as HTMLInputElement).value)} class="pf-input" placeholder="default" />
				</div>
			</div>
		</div>

		<div class="pf-section">
			<div class="pf-section-header">
				<Settings size={16} />
				<h3>Performance & Limits</h3>
			</div>
			<div class="pf-field-grid">
				<div class="pf-field">
					<label class="pf-label" for="profile-max-buffer">Max Output Buffer</label>
					<input id="profile-max-buffer" type="number" value={profileForm.max_buffer_size} oninput={(e) => { const v = (e.target as HTMLInputElement).value; onFormChange('max_buffer_size', v ? Number(v) : null); }} class="pf-input" placeholder="200,000 chars" />
				</div>
			</div>
		</div>

		<div class="pf-section">
			<div class="pf-section-header">
				<FileText size={16} />
				<h3>Settings Sources</h3>
			</div>
			<p class="pf-desc">Load settings from filesystem locations</p>
			<div class="inline-checkboxes">
				{#each [{ id: 'user', label: 'User (~/.claude)' }, { id: 'project', label: 'Project (.claude)' }, { id: 'local', label: 'Local' }] as source}
					<label class="inline-checkbox">
						<input type="checkbox" checked={profileForm.setting_sources.includes(source.id)} onchange={() => onToggleSettingSource(source.id)} class="checkbox-input" />
						<span class="checkbox-label">{source.label}</span>
					</label>
				{/each}
			</div>
		</div>

		<div class="pf-section">
			<div class="pf-section-header">
				<SlidersHorizontal size={16} />
				<h3>Environment Variables</h3>
				<button type="button" class="pf-add-var-btn" onclick={() => { onFormChange('env_vars', [...profileForm.env_vars, { key: '', value: '' }]); }}>
					+ Add Variable
				</button>
			</div>
			<select class="pf-select" onchange={(e) => { const target = e.target as HTMLSelectElement; const key = target.value; if (key && !profileForm.env_vars.some(v => v.key === key)) { onFormChange('env_vars', [...profileForm.env_vars, { key, value: '' }]); } target.value = ''; }}>
				<option value="">Select a known variable to add...</option>
				{#each envVarPresets as preset}
					{#if !profileForm.env_vars.some(v => v.key === preset.key)}
						<option value={preset.key}>{preset.key}</option>
					{/if}
				{/each}
			</select>
			{#if profileForm.env_vars.length > 0}
				<div class="pf-env-list">
					{#each profileForm.env_vars as envVar, index}
						{@const preset = envVarPresets.find(p => p.key === envVar.key)}
						<div class="pf-env-row">
							<input type="text" value={envVar.key} class="pf-input pf-input--mono pf-env-key" placeholder="VARIABLE_NAME" oninput={(e) => { const target = e.target as HTMLInputElement; let val = target.value.replace(/,/g, ''); const updated = [...profileForm.env_vars]; updated[index] = { ...updated[index], key: val }; onFormChange('env_vars', updated); }} />
							<input type="text" value={envVar.value} class="pf-input pf-input--mono pf-env-value" placeholder={preset?.placeholder || 'value'} oninput={(e) => { const target = e.target as HTMLInputElement; let val = target.value.replace(/,/g, ''); const updated = [...profileForm.env_vars]; updated[index] = { ...updated[index], value: val }; onFormChange('env_vars', updated); }} />
							<button type="button" class="pf-env-remove" title="Remove variable" onclick={() => { onFormChange('env_vars', profileForm.env_vars.filter((_, i) => i !== index)); }}>
								<Trash2 size={14} />
							</button>
						</div>
					{/each}
				</div>
			{:else}
				<p class="pf-desc">No environment variables configured.</p>
			{/if}
		</div>
	{/if}
</div>

<style>
	/* Content Area */
	.pf-content {
		padding: 24px 20px;
		display: flex;
		flex-direction: column;
		gap: 28px;
		flex: 1;
	}

	/* Sections */
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

	.pf-section-actions {
		margin-left: auto;
		display: flex;
		gap: 12px;
	}

	.pf-divider {
		height: 1px;
		background: var(--border-subtle);
		width: 100%;
	}

	.pf-desc {
		font-size: var(--text-sm, 0.75rem);
		color: var(--text-secondary);
		margin: 0;
	}

	/* Field Grid */
	.pf-field-grid {
		display: grid;
		grid-template-columns: repeat(2, 1fr);
		gap: 16px;
	}

	@media (max-width: 480px) {
		.pf-field-grid {
			grid-template-columns: 1fr;
		}
	}

	.pf-field {
		display: flex;
		flex-direction: column;
		gap: 6px;
	}

	.pf-label {
		font-size: var(--text-sm, 0.75rem);
		font-weight: 500;
		color: var(--text-secondary);
	}

	/* Inputs */
	.pf-input {
		width: 100%;
		padding: 10px 14px;
		background: var(--surface-1);
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-md, 10px);
		color: var(--text-primary);
		font-size: var(--text-md, 0.875rem);
		font-family: inherit;
		transition: border-color 200ms, box-shadow 200ms;
		outline: none;
	}

	.pf-input:focus {
		border-color: var(--primary);
		box-shadow: 0 0 0 3px color-mix(in oklch, var(--primary) 15%, transparent);
	}

	.pf-input::placeholder {
		color: var(--text-tertiary);
	}

	.pf-input--mono {
		font-family: var(--card-font-mono, 'JetBrains Mono', monospace);
		font-size: var(--text-sm, 0.75rem);
	}

	/* Selects */
	.pf-select {
		width: 100%;
		padding: 10px 14px;
		padding-right: 36px;
		background: var(--surface-1);
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-md, 10px);
		color: var(--text-primary);
		font-size: var(--text-md, 0.875rem);
		font-family: inherit;
		cursor: pointer;
		outline: none;
		appearance: none;
		background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='16' height='16' viewBox='0 0 24 24' fill='none' stroke='%239090A8' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpolyline points='6 9 12 15 18 9'%3E%3C/polyline%3E%3C/svg%3E");
		background-repeat: no-repeat;
		background-position: right 12px center;
		background-size: 16px;
		transition: border-color 200ms, box-shadow 200ms;
	}

	.pf-select:focus {
		border-color: var(--primary);
		box-shadow: 0 0 0 3px color-mix(in oklch, var(--primary) 15%, transparent);
	}

	/* Toggle Switch */
	.pf-toggle-row {
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding: 14px 16px;
		background: var(--surface-1);
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-md, 10px);
	}

	.pf-toggle-info {
		display: flex;
		flex-direction: column;
		gap: 2px;
		flex: 1;
		min-width: 0;
	}

	.pf-toggle-title {
		font-size: var(--text-md, 0.875rem);
		font-weight: 500;
		color: var(--text-primary);
	}

	.pf-toggle-desc {
		font-size: var(--text-xs, 0.6875rem);
		color: var(--text-secondary);
	}

	.pf-toggle {
		position: relative;
		width: 44px;
		height: 24px;
		background: var(--muted);
		border: 1px solid var(--border-subtle);
		border-radius: 12px;
		cursor: pointer;
		transition: all 200ms ease;
		flex-shrink: 0;
		margin-left: 16px;
	}

	.pf-toggle.active {
		background: var(--primary);
		border-color: var(--primary);
	}

	.pf-toggle-knob {
		position: absolute;
		top: 2px;
		left: 2px;
		width: 18px;
		height: 18px;
		background: var(--primary-foreground);
		border-radius: 50%;
		transition: transform 200ms ease;
	}

	.pf-toggle.active .pf-toggle-knob {
		transform: translateX(20px);
	}

	.pf-toggle:disabled {
		cursor: not-allowed;
		opacity: 0.4;
	}

	.pf-toggle-row--disabled .pf-toggle-title {
		opacity: 0.6;
	}

	/* Summary Bar */
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

	.pf-summary-link {
		font-size: var(--text-xs, 0.6875rem);
		font-weight: 500;
		color: var(--text-secondary);
		background: none;
		border: none;
		padding: 0;
		cursor: pointer;
		transition: color 120ms;
	}

	.pf-summary-link:hover {
		color: var(--primary);
	}

	/* Agent Cards */
	.pf-agent-row {
		cursor: pointer;
	}

	.pf-agent-row--builtin {
		cursor: default;
		opacity: 0.85;
	}

	.pf-agent-checkbox {
		width: 18px;
		height: 18px;
		flex-shrink: 0;
		accent-color: var(--primary);
	}

	.pf-agent-checkbox:disabled {
		opacity: 0.5;
		cursor: default;
	}

	/* Group */
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

	/* Environment Variables */
	.pf-env-list {
		display: flex;
		flex-direction: column;
		gap: 6px;
	}

	.pf-env-row {
		display: flex;
		align-items: center;
		gap: 8px;
	}

	.pf-env-key {
		width: 200px;
		flex-shrink: 0;
	}

	.pf-env-value {
		flex: 1;
	}

	.pf-env-remove {
		width: 32px;
		height: 32px;
		display: flex;
		align-items: center;
		justify-content: center;
		background: color-mix(in oklch, var(--destructive) 10%, transparent);
		border: none;
		border-radius: 6px;
		color: var(--destructive);
		cursor: pointer;
		transition: all 150ms;
		flex-shrink: 0;
	}

	.pf-env-remove:hover {
		background: color-mix(in oklch, var(--destructive) 20%, transparent);
	}

	.pf-add-var-btn {
		margin-left: auto;
		background: none;
		border: none;
		color: var(--primary);
		font-size: var(--text-sm, 0.75rem);
		font-weight: 500;
		cursor: pointer;
	}

	.pf-add-var-btn:hover {
		text-decoration: underline;
	}

	@media (max-width: 480px) {
		.pf-env-row {
			display: grid;
			grid-template-columns: 1fr auto;
			grid-template-rows: auto auto;
		}

		.pf-env-key {
			width: 100%;
			grid-column: 1;
			grid-row: 1;
		}

		.pf-env-value {
			grid-column: 1;
			grid-row: 2;
		}

		.pf-env-remove {
			grid-column: 2;
			grid-row: 1 / 3;
			align-self: center;
		}
	}

	.text-btn {
		background: transparent;
		border: none;
		font-size: var(--text-xs, 0.75rem);
		cursor: pointer;
		transition: color 100ms;
	}

	.text-btn.muted {
		color: var(--text-secondary);
	}

	.text-btn.muted:hover {
		color: var(--text-primary);
	}

	.text-btn.primary {
		color: var(--primary);
	}

	.text-btn.primary:hover {
		text-decoration: underline;
	}

	.required {
		color: var(--destructive);
	}

	/* Model select + 1M context toggle, side by side */
	.pf-model-row {
		display: flex;
		align-items: center;
		gap: 12px;
	}

	.pf-model-row .pf-select {
		flex: 1;
		min-width: 0;
	}

	/* 1M context toggle (inline, right of model select) */
	.pf-1m-toggle {
		flex-shrink: 0;
		white-space: nowrap;
	}

	/* Inline checkboxes */
	.inline-checkboxes {
		display: flex;
		flex-wrap: wrap;
		gap: 16px;
	}

	.inline-checkbox {
		display: flex;
		align-items: center;
		gap: 8px;
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
</style>
