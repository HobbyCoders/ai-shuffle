<script lang="ts">
	/**
	 * ProfileForm — Form tab content for create/edit profile
	 * Renders the active tab's content (general, tools, agents, prompt, advanced)
	 * in the Studio-v2 grouped-field design language (.set-group / .set-row).
	 * The parent (ProfileCard) owns the StudioEditorShell chrome, rail, and footer.
	 */
	import type { Profile } from '$lib/api/client';
	import { Users, Settings, SlidersHorizontal, Bot, Check, Plus, Trash2, FileText, Brain } from 'lucide-svelte';
	import ProfileToolConfig from './ProfileToolConfig.svelte';
	import ProfileSystemPrompt from './ProfileSystemPrompt.svelte';
	import ModelPicker from '$lib/components/settings/ModelPicker.svelte';
	import {
		CUSTOM_VALUE,
		getBaseModel,
		has1MContext,
		withContextSuffix,
		modelSupports1M,
		encodeProviderModel,
		decodeProviderModel
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

	interface AgentTemplate {
		id: string;
		name: string;
		description: string;
		prompt: string;
		tools?: string[];
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
		permission_mode: string;
		effort: 'low' | 'medium' | 'high' | 'xhigh' | 'max' | null;
		max_turns: number | null;
		allowed_tools: string[];
		disallowed_tools: string[];
		enabled_agents: string[];
		memory: string;
		include_partial_messages: boolean;
		system_prompt_content: string;
		enabled_ai_tools: string[];
		cwd: string;
		add_dirs: string;
		env_vars: { key: string; value: string }[];
		app_config_tools: boolean;
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
		/** Built-in system-prompt templates (GET /agents/templates), for new agents. */
		templates: AgentTemplate[];
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
		/** Apply a system-prompt template by id (fills prompt + optional tools). */
		onApplyTemplate: (templateId: string) => void;
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
		templates,
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
		onApplyTemplate,
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

	// Invoke allowlist: every agent EXCEPT the one being edited (an agent can't
	// dispatch itself). Empty selection = allow all.
	const invocableAgents = $derived(
		allSubagents.filter((a) => a.id !== editingProfile?.id)
	);
	const allInvocableSelected = $derived(
		invocableAgents.length > 0 &&
			invocableAgents.every((a) => profileForm.enabled_agents.includes(a.id))
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
				<div class="lab"><h4>Description</h4><p>A short summary of what this agent is for.</p></div>
				<div class="ctl">
					<textarea class="fld" rows={2} value={profileForm.description} oninput={(e) => onFormChange('description', (e.target as HTMLTextAreaElement).value)} placeholder="A coding assistant for frontend work"></textarea>
				</div>
			</div>
			{#if editingProfile}
				<div class="set-row">
					<div class="lab"><h4>Group</h4><p>Organize related agents together.</p></div>
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
			<div class="set-grouphead"><h3><Settings />Model &amp; permissions</h3></div>
			<div class="set-row">
				<div class="lab"><h4>Model</h4><p>The model this agent runs on. Any signed-in provider's models are available.</p></div>
				<div class="ctl ctl-col">
					<div class="grp-row">
						<ModelPicker
							value={customModelMode
								? CUSTOM_VALUE
								: profileForm.provider
									? encodeProviderModel(profileForm.provider, profileForm.model)
									: getBaseModel(profileForm.model)}
							onSelect={(v) => {
								const composite = decodeProviderModel(v);
								if (v === CUSTOM_VALUE) {
									onCustomModelModeChange(true, '');
									onFormChange('provider', null);
								} else if (composite) {
									// `prov:<providerId>:<modelId>` — the shared codec splits on the
									// first colon only, so provider model ids that contain colons
									// themselves (e.g. "meta-llama/llama-3.1:free") survive intact.
									onCustomModelModeChange(false, composite.modelId);
									onFormChange('provider', composite.providerId);
								} else {
									onCustomModelModeChange(false, withContextSuffix(v, modelSupports1M(v) && has1MContext(profileForm.model)));
									onFormChange('provider', null);
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
			<div class="set-row">
				<div class="lab"><h4>Permission mode</h4><p>How the agent asks before taking actions.</p></div>
				<div class="ctl">
					<select class="sel" value={profileForm.permission_mode} onchange={(e) => onFormChange('permission_mode', (e.target as HTMLSelectElement).value)}>
						<option value="default">Default</option>
						<option value="acceptEdits">Accept Edits</option>
						<option value="plan">Plan</option>
						<option value="bypassPermissions">Bypass Permissions</option>
					</select>
				</div>
			</div>
			<div class="set-row">
				<div class="lab"><h4>Effort</h4><p>Reasoning depth vs. speed and cost. Applies to any provider that supports it; can be overridden per-chat.</p></div>
				<div class="ctl">
					<select class="sel" value={profileForm.effort || ''} onchange={(e) => { const v = (e.target as HTMLSelectElement).value; onFormChange('effort', v ? v : null); }}>
						<option value="">Provider default</option>
						<option value="low">Low</option>
						<option value="medium">Medium</option>
						<option value="high">High</option>
						<option value="xhigh">XHigh</option>
						<option value="max">Max</option>
					</select>
				</div>
			</div>
			<div class="set-row">
				<div class="lab"><h4>Max model calls per turn</h4><p>Caps how many model calls one turn may make. Empty = default.</p></div>
				<div class="ctl">
					<input class="inp" type="number" value={profileForm.max_turns} oninput={(e) => { const v = (e.target as HTMLInputElement).value; onFormChange('max_turns', v ? Number(v) : null); }} placeholder="Default (50)" />
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
				<div class="lab"><h4>App config via chat</h4><p>Allow AI to manage hooks, automations, MCP servers, and agents through conversation. Admin only.</p></div>
				<div class="ctl">
					<button type="button" class="switch" class:on={profileForm.app_config_tools} role="switch" aria-checked={profileForm.app_config_tools} aria-label="Toggle app configuration tools" onclick={() => onFormChange('app_config_tools', !profileForm.app_config_tools)}><i></i></button>
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
		<!-- AGENTS TAB — invoke allowlist -->
		<div class="set-group">
			<div class="set-grouphead grouphead-row">
				<div>
					<h3><Bot />Invocable agents</h3>
					<p>Agents this agent may dispatch via the Task tool. Leave empty to allow all agents.</p>
				</div>
				{#if invocableAgents.length > 0}
					<div class="head-acts">
						<button type="button" class="lnk" onclick={() => onFormChange('enabled_agents', invocableAgents.map((a) => a.id))}>Select all</button>
						<button type="button" class="lnk muted" onclick={() => onFormChange('enabled_agents', [])}>Clear</button>
					</div>
				{/if}
			</div>
			{#if invocableAgents.length === 0}
				<div class="set-row"><div class="lab"><p>No other agents to invoke.</p></div></div>
			{:else}
				{@const allowAll = profileForm.enabled_agents.length === 0}
				{#each invocableAgents as agent (agent.id)}
					{@const isSelected = profileForm.enabled_agents.includes(agent.id)}
					<div class="set-row">
						<div class="lab">
							<h4>{agent.name}{#if allowAll}<span class="hint"> · Allowed (all)</span>{/if}</h4>
							<p>{getModelDisplay(agent.model)}</p>
						</div>
						<div class="ctl">
							<button type="button" class="switch" class:on={isSelected} aria-checked={isSelected} role="switch" aria-label={`Toggle ${agent.name}`} onclick={() => onToggleAgent(agent.id)}><i></i></button>
						</div>
					</div>
				{/each}
			{/if}
		</div>

		{#if invocableAgents.length > 0}
			<div class="ed-note">
				<Bot />
				<span>
					{#if profileForm.enabled_agents.length === 0}
						No restriction — this agent may invoke any of the {invocableAgents.length} agents.
					{:else}
						{profileForm.enabled_agents.length} of {invocableAgents.length} agents invocable{allInvocableSelected ? ' (all)' : ''}.
					{/if}
				</span>
			</div>
		{/if}

	{:else if activeTab === 'prompt'}
		{#if !editingProfile && templates.length > 0}
			<div class="set-group">
				<div class="set-grouphead"><h3><FileText />Start from a template</h3><p>Pick a built-in template to fill the prompt below (and its suggested tools). You can edit everything afterward.</p></div>
				<div class="set-row">
					<div class="lab"><h4>Template</h4><p>Optional. Choosing one replaces the current prompt.</p></div>
					<div class="ctl">
						<select
							class="sel"
							value=""
							onchange={(e) => {
								const target = e.target as HTMLSelectElement;
								const id = target.value;
								if (id) onApplyTemplate(id);
								target.value = '';
							}}
						>
							<option value="">Select a template…</option>
							{#each templates as tpl}
								<option value={tpl.id}>{tpl.name}</option>
							{/each}
						</select>
					</div>
				</div>
			</div>
		{/if}
		<ProfileSystemPrompt
			systemPromptContent={profileForm.system_prompt_content}
			onSystemPromptContentChange={(v) => onFormChange('system_prompt_content', v)}
		/>

		<div class="set-group">
			<div class="set-grouphead"><h3><Brain />Memory</h3><p>Persistent notes the agent maintains itself via its Memory tool at runtime. This text is injected into the agent's system prompt.</p></div>
			<div class="set-row stack">
				<div class="lab lab-row">
					<h4>Agent memory</h4>
					<span class="count">{profileForm.memory.length.toLocaleString()} chars</span>
				</div>
				<div class="ctl">
					<textarea
						class="fld code memory-area"
						value={profileForm.memory}
						oninput={(e) => onFormChange('memory', (e.target as HTMLTextAreaElement).value)}
						placeholder="Empty by default. The agent writes here itself using its Memory tool; you can also seed or edit it manually."
					></textarea>
				</div>
			</div>
		</div>

	{:else if activeTab === 'advanced'}
		<!-- ADVANCED TAB -->
		<div class="set-group">
			<div class="set-grouphead"><h3>Environment</h3><p>Configure the working environment for this agent.</p></div>
			<div class="set-row stack">
				<div class="lab"><h4>Working directory</h4><p>Where the agent runs by default.</p></div>
				<div class="ctl"><input class="inp mono" value={profileForm.cwd} oninput={(e) => onFormChange('cwd', (e.target as HTMLInputElement).value)} placeholder="/home/user/projects/my-api" /></div>
			</div>
			<div class="set-row stack">
				<div class="lab"><h4>Additional directories</h4><p>Extra directories the agent may access, comma-separated.</p></div>
				<div class="ctl"><input class="inp mono" value={profileForm.add_dirs} oninput={(e) => onFormChange('add_dirs', (e.target as HTMLInputElement).value)} placeholder="/home/user/notes, /home/user/docs" /></div>
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

	/* Memory section (prompt tab) */
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
	textarea.memory-area {
		min-height: 180px;
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
