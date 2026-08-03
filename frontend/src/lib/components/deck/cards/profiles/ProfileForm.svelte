<script lang="ts">
	/**
	 * ProfileForm — Form tab content for create/edit profile
	 * Renders the active tab's content (general, tools, agents, prompt, advanced)
	 * in the Studio-v2 grouped-field design language (.set-group / .set-row).
	 * The parent (ProfileCard) owns the StudioEditorShell chrome, rail, and footer.
	 */
	import type { Profile } from '$lib/api/client';
	import { layout } from '$lib/stores/layout.svelte';
	import { Users, Settings, SlidersHorizontal, Bot, Check, Plus, Trash2 } from 'lucide-svelte';
	import ProfileToolConfig from './ProfileToolConfig.svelte';
	import ProfileSystemPrompt from './ProfileSystemPrompt.svelte';
	import ModelPicker from '$lib/components/settings/ModelPicker.svelte';
	import {
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
		/** `model_providers.id` this profile's model belongs to, or `null` for native Anthropic. */
		provider: string | null;
		/** Optional cross-provider subagent leg: `model_providers.id`, or `null` to inherit. */
		subagent_provider: string | null;
		/** Subagent model id paired with `subagent_provider`, or `null` to inherit. */
		subagent_model: string | null;
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

	const settingSources = [
		{ id: 'user', label: 'User (~/.claude)' },
		{ id: 'project', label: 'Project (.claude)' },
		{ id: 'local', label: 'Local' }
	];

	// Count of enabled custom (non-builtin) agents, for the summary note.
	const enabledCustomCount = $derived(
		profileForm.enabled_agents.filter(
			(id) => !builtinAgentIds.includes(id) && customAgents.some((a) => a.id === id)
		).length
	);
</script>

<div class="card-system pf-body">
	{#if activeTab === 'general'}
		<!-- GENERAL TAB -->
		<div class="set-group">
			<div class="set-grouphead"><h3><Users />Identity</h3></div>
			<div class="set-row">
				<div class="lab"><h4>Name <span class="req">*</span></h4><p>Display name shown across the app.</p></div>
				<div class="ctl">
					<input class="inp" value={profileForm.name} oninput={(e) => onFormChange('name', (e.target as HTMLInputElement).value)} placeholder="My Custom Agent" />
				</div>
			</div>
			<div class="set-row stack">
				<div class="lab"><h4>Description</h4><p>A short summary of what this profile is for.</p></div>
				<div class="ctl">
					<textarea class="fld" rows={2} value={profileForm.description} oninput={(e) => onFormChange('description', (e.target as HTMLTextAreaElement).value)} placeholder="A coding assistant for frontend work"></textarea>
				</div>
			</div>
			{#if editingProfile}
				<div class="set-row">
					<div class="lab"><h4>Group</h4><p>Organize related profiles together.</p></div>
					<div class="ctl ctl-col">
						<div class="grp-row">
							<select
								class="sel"
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
								<button type="button" class="icon-btn" onclick={() => editingProfile && confirmGroupCreate(editingProfile.id)} title="Create">
									<Check size={14} />
								</button>
							{:else}
								<button type="button" class="icon-btn" onclick={startGroupCreate} title="New group">
									<Plus size={14} />
								</button>
								{#if editingProfile && (groups?.profiles?.assignments?.[editingProfile.id] ?? '')}
									<button type="button" class="icon-btn danger" onclick={() => { if (!editingProfile) return; const g = groups?.profiles?.assignments?.[editingProfile.id]; if (g) { onRemoveFromGroup(editingProfile.id); onDeleteGroup('profiles', g); } }} title="Delete group">
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

		<div class="set-group">
			<div class="set-grouphead"><h3><Settings />Model &amp; reasoning</h3></div>
			<div class="set-row">
				<div class="lab"><h4>Model</h4><p>The Claude model this profile runs on.</p></div>
				<div class="ctl ctl-col">
					<div class="grp-row">
						<ModelPicker
							value={customModelMode
								? CUSTOM_VALUE
								: profileForm.provider
									? `prov:${profileForm.provider}:${profileForm.model}`
									: getBaseModel(profileForm.model)}
							onSelect={(v) => {
								if (v === CUSTOM_VALUE) {
									onCustomModelModeChange(true, '');
									onFormChange('provider', null);
									// Leaving a provider clears any subagent leg — a hidden
									// stale cross-provider leg must never be saved.
									onFormChange('subagent_provider', null);
									onFormChange('subagent_model', null);
								} else if (v.startsWith('prov:')) {
									// Composite value: `prov:<providerId>:<modelId>`. Split on the
									// first colon only — provider model ids (e.g. OpenRouter's
									// "meta-llama/llama-3.1:free") can contain colons themselves.
									const rest = v.slice('prov:'.length);
									const sep = rest.indexOf(':');
									const providerId = rest.slice(0, sep);
									const modelId = rest.slice(sep + 1);
									onCustomModelModeChange(false, modelId);
									onFormChange('provider', providerId);
								} else {
									onCustomModelModeChange(false, withContextSuffix(v, modelSupports1M(v) && has1MContext(profileForm.model)));
									onFormChange('provider', null);
									// Native model → no provider → clear the subagent leg.
									onFormChange('subagent_provider', null);
									onFormChange('subagent_model', null);
								}
							}}
						/>
						{#if !customModelMode && modelSupports1M(profileForm.model)}
							<label class="chk">
								<input type="checkbox" checked={has1MContext(profileForm.model)} onchange={(e) => onFormChange('model', withContextSuffix(profileForm.model, (e.target as HTMLInputElement).checked))} />
								<span>1M context</span>
							</label>
						{/if}
					</div>
					{#if customModelMode}
						<input type="text" value={profileForm.model} oninput={(e) => onFormChange('model', (e.target as HTMLInputElement).value)} class="inp mono" placeholder="e.g. claude-sonnet-4-5" />
					{/if}
				</div>
			</div>
			{#if profileForm.provider}
				<!-- A2 UI gate: the cross-provider subagent leg is offered ONLY when
				     the profile itself runs on a provider model. Native-Claude
				     profiles never see this row. -->
				<div class="set-row">
					<div class="lab"><h4>Subagent model</h4><p>Optional. Runs this profile's subagents on a different provider's model. Inherit uses the provider default.</p></div>
					<div class="ctl ctl-col">
						<ModelPicker
							allowInherit
							providerModelsOnly
							servableOnly
							value={profileForm.subagent_provider ? `prov:${profileForm.subagent_provider}:${profileForm.subagent_model}` : ''}
							onSelect={(v) => {
								if (v.startsWith('prov:')) {
									// Same first-colon decode as the main picker.
									const rest = v.slice('prov:'.length);
									const sep = rest.indexOf(':');
									onFormChange('subagent_provider', rest.slice(0, sep));
									onFormChange('subagent_model', rest.slice(sep + 1));
								} else {
									// Inherit ('') clears both. Native/custom entries are
									// suppressed via providerModelsOnly, so any non-composite
									// value here means "inherit".
									onFormChange('subagent_provider', null);
									onFormChange('subagent_model', null);
								}
							}}
						/>
					</div>
				</div>
			{/if}
			<div class="set-row">
				<div class="lab"><h4>Permission mode</h4><p>How the agent asks before taking actions.</p></div>
				<div class="ctl">
					<select class="sel" value={profileForm.permission_mode} onchange={(e) => onFormChange('permission_mode', (e.target as HTMLSelectElement).value)}>
						<option value="default">Default</option>
						<option value="auto">Auto</option>
						<option value="acceptEdits">Accept Edits</option>
						<option value="plan">Plan</option>
						<option value="bypassPermissions">Bypass Permissions</option>
					</select>
				</div>
			</div>
			<div class="set-row">
				<div class="lab"><h4>Thinking mode</h4><p>Extended reasoning budget for the model.</p></div>
				<div class="ctl">
					<select class="sel" value={profileForm.thinking_mode} onchange={(e) => onFormChange('thinking_mode', (e.target as HTMLSelectElement).value)}>
						<option value="default">Default</option>
						<option value="disabled">Off</option>
						<option value="adaptive">Adaptive</option>
						<option value="think">8K Budget</option>
						<option value="think_harder">24K Budget</option>
						<option value="ultra_think">64K Budget</option>
					</select>
				</div>
			</div>
			<div class="set-row">
				<div class="lab"><h4>Effort level</h4><p>Trade speed against depth of work.</p></div>
				<div class="ctl">
					<select class="sel" value={profileForm.effort || ''} onchange={(e) => { const v = (e.target as HTMLSelectElement).value; onFormChange('effort', v ? v : null); }}>
						<option value="">Default</option>
						<option value="low">Low</option>
						<option value="medium">Medium</option>
						<option value="high">High</option>
						<option value="xhigh">XHigh</option>
						<option value="max">Max</option>
					</select>
				</div>
			</div>
			<div class="set-row">
				<div class="lab"><h4>Max turns</h4><p>Cap on agent turns per run. Empty = unlimited.</p></div>
				<div class="ctl">
					<input class="inp" type="number" value={profileForm.max_turns} oninput={(e) => { const v = (e.target as HTMLInputElement).value; onFormChange('max_turns', v ? Number(v) : null); }} placeholder="Unlimited" />
				</div>
			</div>
		</div>

		<div class="set-group">
			<div class="set-grouphead"><h3><SlidersHorizontal />Behavior</h3></div>
			<div class="set-row">
				<div class="lab"><h4>Include partial messages</h4><p>Stream partial text during AI generation for real-time feedback.</p></div>
				<div class="ctl">
					<button type="button" class="switch" class:on={profileForm.include_partial_messages} role="switch" aria-checked={profileForm.include_partial_messages} aria-label="Toggle include partial messages" onclick={() => onFormChange('include_partial_messages', !profileForm.include_partial_messages)}><i></i></button>
				</div>
			</div>
			<div class="set-row">
				<div class="lab"><h4>File checkpointing</h4><p>Create restore points before file modifications.</p></div>
				<div class="ctl">
					<button type="button" class="switch" class:on={profileForm.enable_file_checkpointing} role="switch" aria-checked={profileForm.enable_file_checkpointing} aria-label="Toggle file checkpointing" onclick={() => onFormChange('enable_file_checkpointing', !profileForm.enable_file_checkpointing)}><i></i></button>
				</div>
			</div>
			<div class="set-row">
				<div class="lab"><h4>App config via chat</h4><p>Allow AI to manage hooks, automations, MCP servers, and profiles through conversation. Admin only.</p></div>
				<div class="ctl">
					<button type="button" class="switch" class:on={profileForm.app_config_tools} role="switch" aria-checked={profileForm.app_config_tools} aria-label="Toggle app configuration tools" onclick={() => onFormChange('app_config_tools', !profileForm.app_config_tools)}><i></i></button>
				</div>
			</div>
			<div class="set-row">
				<div class="lab"><h4>Strict MCP config</h4><p>Fail-fast if any MCP server is misconfigured. Recommended for production profiles.</p></div>
				<div class="ctl">
					<button type="button" class="switch" class:on={profileForm.strict_mcp_config} role="switch" aria-checked={profileForm.strict_mcp_config} aria-label="Toggle strict MCP config" onclick={() => onFormChange('strict_mcp_config', !profileForm.strict_mcp_config)}><i></i></button>
				</div>
			</div>
			<div class="set-row" class:disabled={!computerUse.available}>
				<div class="lab">
					<h4>Computer use</h4>
					<p>
						Let the agent control this machine's screen — screenshots, mouse, and keyboard,
						like Claude Desktop. Shares one cursor with you. Native (non-Docker) hosts only.
						{#if !computerUse.available}
							<br /><strong>{COMPUTER_USE_REASONS[computerUse.reason] ?? `Unavailable (${computerUse.reason}).`}</strong>
						{/if}
					</p>
				</div>
				<div class="ctl">
					<button type="button" class="switch" class:on={profileForm.computer_use_enabled} role="switch" aria-checked={profileForm.computer_use_enabled} aria-label="Toggle computer use" disabled={!computerUse.available} onclick={() => onFormChange('computer_use_enabled', !profileForm.computer_use_enabled)}><i></i></button>
				</div>
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
		<div class="set-group">
			<div class="set-grouphead grouphead-row">
				<div>
					<h3><Bot />Sub-agents</h3>
					<p>Built-in agents are always available. Choose which custom agents to enable.</p>
				</div>
				{#if customAgents.length > 0}
					<div class="head-acts">
						<button type="button" class="lnk" onclick={() => onFormChange('enabled_agents', [...builtinAgentIds, ...customAgents.map((a) => a.id)])}>Select all</button>
						<button type="button" class="lnk muted" onclick={() => onFormChange('enabled_agents', [...builtinAgentIds])}>Clear custom</button>
					</div>
				{/if}
			</div>
			{#if allSubagents.length === 0}
				<div class="set-row"><div class="lab"><p>No agents configured.</p></div></div>
			{:else}
				{#each allSubagents as agent (agent.id)}
					{@const isBuiltin = agent.is_builtin}
					{@const isSelected = isBuiltin || profileForm.enabled_agents.includes(agent.id)}
					<div class="set-row">
						<div class="lab">
							<h4>{agent.name}{#if isBuiltin}<span class="hint"> · Always on</span>{/if}</h4>
							<p>{getModelDisplay(agent.model)}</p>
						</div>
						<div class="ctl">
							<button type="button" class="switch" class:on={isSelected} disabled={isBuiltin} aria-checked={isSelected} role="switch" aria-label={`Toggle ${agent.name}`} onclick={() => onToggleAgent(agent.id)}><i></i></button>
						</div>
					</div>
				{/each}
			{/if}
		</div>

		{#if allSubagents.length > 0}
			<div class="ed-note">
				<Bot />
				<span>
					{builtinAgentIds.length} built-in{#if customAgents.length > 0} + {enabledCustomCount} of {customAgents.length} custom{/if} enabled.
					<button type="button" class="lnk-inline" onclick={() => layout.openFeaturePage('subagent')}>Configure agents →</button>
				</span>
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
		<div class="set-group">
			<div class="set-grouphead"><h3>Environment</h3><p>Configure the working environment for this profile.</p></div>
			<div class="set-row stack">
				<div class="lab"><h4>Working directory</h4><p>Where the agent runs by default.</p></div>
				<div class="ctl"><input class="inp mono" value={profileForm.cwd} oninput={(e) => onFormChange('cwd', (e.target as HTMLInputElement).value)} placeholder="/home/user/projects/my-api" /></div>
			</div>
			<div class="set-row">
				<div class="lab"><h4>Run as user</h4><p>System user the agent process runs under.</p></div>
				<div class="ctl"><input class="inp" value={profileForm.user} oninput={(e) => onFormChange('user', (e.target as HTMLInputElement).value)} placeholder="default" /></div>
			</div>
		</div>

		<div class="set-group">
			<div class="set-grouphead"><h3>Performance &amp; limits</h3></div>
			<div class="set-row">
				<div class="lab"><h4>Max output buffer</h4><p>Maximum characters of tool output to retain.</p></div>
				<div class="ctl"><input class="inp" type="number" value={profileForm.max_buffer_size} oninput={(e) => { const v = (e.target as HTMLInputElement).value; onFormChange('max_buffer_size', v ? Number(v) : null); }} placeholder="200,000 chars" /></div>
			</div>
		</div>

		<div class="set-group">
			<div class="set-grouphead"><h3>Settings sources</h3><p>Load settings from filesystem locations.</p></div>
			<div class="set-row stack">
				<div class="lab"><h4>Sources</h4></div>
				<div class="ctl">
					<div class="chips-select">
						{#each settingSources as source}
							{@const on = profileForm.setting_sources.includes(source.id)}
							<button type="button" class="chsel" class:on onclick={() => onToggleSettingSource(source.id)}>
								{#if on}<Check size={12} />{/if}
								{source.label}
							</button>
						{/each}
					</div>
				</div>
			</div>
		</div>

		<div class="set-group">
			<div class="set-grouphead grouphead-row">
				<div><h3>Environment variables</h3><p>Set environment variables for the agent's runtime.</p></div>
				<button type="button" class="lnk" onclick={() => onFormChange('env_vars', [...profileForm.env_vars, { key: '', value: '' }])}>+ Add variable</button>
			</div>
			<div class="set-row stack">
				<div class="lab"><h4>Known variables</h4><p>Pick a recognized variable to add it quickly.</p></div>
				<div class="ctl">
					<select class="sel fld" onchange={(e) => { const target = e.target as HTMLSelectElement; const key = target.value; if (key && !profileForm.env_vars.some((v) => v.key === key)) { onFormChange('env_vars', [...profileForm.env_vars, { key, value: '' }]); } target.value = ''; }}>
						<option value="">Select a known variable to add…</option>
						{#each envVarPresets as preset}
							{#if !profileForm.env_vars.some((v) => v.key === preset.key)}
								<option value={preset.key}>{preset.key}</option>
							{/if}
						{/each}
					</select>
					{#if profileForm.env_vars.length > 0}
						<div class="env-list">
							{#each profileForm.env_vars as envVar, index}
								{@const preset = envVarPresets.find((p) => p.key === envVar.key)}
								<div class="env-row">
									<input type="text" value={envVar.key} class="inp mono env-key" placeholder="VARIABLE_NAME" oninput={(e) => { const target = e.target as HTMLInputElement; let val = target.value.replace(/,/g, ''); const updated = [...profileForm.env_vars]; updated[index] = { ...updated[index], key: val }; onFormChange('env_vars', updated); }} />
									<input type="text" value={envVar.value} class="inp mono env-val" placeholder={preset?.placeholder || 'value'} oninput={(e) => { const target = e.target as HTMLInputElement; let val = target.value.replace(/,/g, ''); const updated = [...profileForm.env_vars]; updated[index] = { ...updated[index], value: val }; onFormChange('env_vars', updated); }} />
									<button type="button" class="icon-btn danger" title="Remove variable" onclick={() => onFormChange('env_vars', profileForm.env_vars.filter((_, i) => i !== index))}>
										<Trash2 size={14} />
									</button>
								</div>
							{/each}
						</div>
					{:else}
						<p class="empty">No environment variables configured.</p>
					{/if}
				</div>
			</div>
		</div>
	{/if}
</div>

<style>
	.pf-body {
		display: block;
	}

	/* Required asterisk */
	.req {
		color: var(--destructive);
		font-weight: 700;
	}

	/* Control stacked vertically (control + helper input below) */
	.ctl-col {
		flex-direction: column;
		align-items: stretch;
		gap: 8px;
	}
	.ctl-col :global(.inp),
	.ctl-col :global(.sel) {
		width: 100%;
	}

	/* Inline control row (select + adjacent icon button / checkbox) */
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
	/* min-width: 0 here would beat the picker's own 220px floor on specificity
	   and collapse the trigger to the current label's width. */
	.grp-row :global(.mpk) {
		flex: 1;
		min-width: 220px;
	}

	/* Square icon button next to a select (new/delete group, remove var) */
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

	/* Inline checkbox label (1M context) */
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

	/* Group header with trailing actions */
	.grouphead-row {
		display: flex;
		align-items: flex-start;
		justify-content: space-between;
		gap: 14px;
	}
	.head-acts {
		display: flex;
		gap: 14px;
		flex-shrink: 0;
		padding-top: 2px;
	}

	/* Text-link buttons */
	.lnk {
		background: none;
		border: none;
		padding: 0;
		font: inherit;
		font-size: 12.5px;
		font-weight: 600;
		color: var(--foreground);
		cursor: pointer;
	}
	.lnk:hover {
		text-decoration: underline;
	}
	.lnk.muted {
		color: var(--muted-foreground);
		font-weight: 500;
	}
	.lnk-inline {
		background: none;
		border: none;
		padding: 0;
		font: inherit;
		font-weight: 600;
		color: var(--foreground);
		cursor: pointer;
		text-decoration: underline;
	}

	/* Disabled toggle row (computer use unavailable) */
	.set-row.disabled .lab h4 {
		opacity: 0.6;
	}

	/* Environment variable rows */
	.env-list {
		display: flex;
		flex-direction: column;
		gap: 8px;
		margin-top: 10px;
	}
	.env-row {
		display: flex;
		align-items: center;
		gap: 8px;
	}
	/* Extra `.env-list` qualifier raises specificity above the shell's
	   `.set-row.stack .ctl .inp { width: 100% }` rule, which otherwise ties
	   and (by source order) forces the no-shrink key input to 100% width —
	   pushing the value input and delete button off the right edge. */
	.env-list .env-row :global(.inp.env-key) {
		min-width: 0;
		width: auto;
		flex: 1 1 0;
	}
	.env-list .env-row :global(.inp.env-val) {
		min-width: 0;
		width: 150px;
		flex: 0 0 150px;
	}
	.empty {
		margin: 10px 0 0;
		font-size: 12.5px;
		color: var(--muted-foreground);
	}

	@media (max-width: 560px) {
		.env-row {
			display: grid;
			grid-template-columns: 1fr auto;
			grid-template-rows: auto auto;
		}
		.env-list .env-row :global(.inp.env-key) {
			width: 100%;
			flex: none;
			grid-column: 1;
			grid-row: 1;
		}
		.env-list .env-row :global(.inp.env-val) {
			width: 100%;
			flex: none;
			grid-column: 1;
			grid-row: 2;
		}
		.env-row .icon-btn {
			grid-column: 2;
			grid-row: 1 / 3;
			align-self: center;
		}
	}
</style>
