<script lang="ts">
	/**
	 * ProfileForm — Form tab content for create/edit profile
	 * Renders the active tab's content (general, tools, agents, prompt, advanced)
	 * in the Studio-v2 grouped-field design language (.set-group / .set-row).
	 * The parent (ProfileCard) owns the StudioEditorShell chrome, rail, and footer.
	 */
	import type { Profile } from '$lib/api/client';
	import type { ToolSurface, ToolSurfaceGroup } from '$lib/api/toolSurface';
	import { Users, Settings, SlidersHorizontal, Bot, Check, Plus, Trash2, FileText, Monitor, OctagonX } from 'lucide-svelte';
	import ProfileToolConfig from './ProfileToolConfig.svelte';
	import ProfileSystemPrompt from './ProfileSystemPrompt.svelte';
	import ModelPicker from '$lib/components/settings/ModelPicker.svelte';
	import {
		encodeProviderModel,
		decodeProviderModel,
		getProviders,
		getProviderDisplay,
		NATIVE_PROVIDER_KEY
	} from '$lib/constants/models';
	import { onMount } from 'svelte';
	import { getComputerUseStatus } from '$lib/api/computerUse';
	import { computerUse } from '$lib/stores/computerUse.svelte';

	// Host capability for the Computer Use toggle. Native-only: unavailable in
	// Docker/headless, which we surface so the toggle can be greyed out.
	let computerUseHost = $state<{ available: boolean; reason: string }>({ available: true, reason: 'ok' });
	onMount(async () => {
		try {
			const s = await getComputerUseStatus();
			computerUseHost = { available: s.available, reason: s.reason };
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

	interface Subagent {
		id: string;
		name: string;
		description: string;
		config?: {
			model?: string;
			provider?: string | null;
			provider_models?: Record<string, string>;
		};
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
		/** Per-provider model used when this agent is dispatched as a subagent. */
		provider_models: Record<string, string>;
		max_turns: number | null;
		allowed_tools: string[];
		disallowed_tools: string[];
		enabled_agents: string[];
		/** 'all' = may invoke any agent; 'custom' = enabled_agents is the exact allowlist (empty = none). */
		agent_invoke_mode: 'all' | 'custom';
		include_partial_messages: boolean;
		system_prompt_content: string;
		cwd: string;
		add_dirs: string;
		env_vars: { key: string; value: string }[];
		computer_use_enabled: boolean;
		/** Setting sources (fluid scope filter). null = all scopes (default). */
		setting_sources: string[] | null;
	}

	interface Props {
		activeTab: string;
		editingProfile: Profile | null;
		profileForm: ProfileFormData;
		toolSelectionMode: 'all' | 'none' | 'allow' | 'disallow';
		groups: GroupState;
		allSubagents: Subagent[];
		builtinAgentIds: string[];
		customAgents: Subagent[];
		/** Built-in system-prompt templates (GET /agents/templates), for new agents. */
		templates: AgentTemplate[];
		/** Live tool surface (GET /tools/surface) driving the picker. */
		toolSurface: ToolSurface;
		toolsLoading: boolean;
		toolsRefreshing: boolean;
		isAdmin: boolean;
		onFormChange: (field: string, value: any) => void;
		onToolSelectionModeChange: (mode: 'all' | 'none' | 'allow' | 'disallow') => void;
		onToggleTool: (group: ToolSurfaceGroup, toolName: string) => void;
		onToggleGroup: (group: ToolSurfaceGroup) => void;
		onRefreshTools: () => void;
		onToggleAgent: (agentId: string) => void;
		/** Apply a system-prompt template by id (fills prompt + optional tools). */
		onApplyTemplate: (templateId: string) => void;
		onAssignToGroup: (profileId: string, groupName: string) => void;
		onRemoveFromGroup: (profileId: string) => void;
		onCreateGroup: (category: string, name: string) => void;
		onDeleteGroup: (category: string, name: string) => void;
	}

	let {
		activeTab,
		editingProfile,
		profileForm,
		toolSelectionMode,
		groups,
		allSubagents,
		builtinAgentIds,
		customAgents,
		templates,
		toolSurface,
		toolsLoading,
		toolsRefreshing,
		isAdmin,
		onFormChange,
		onToolSelectionModeChange,
		onToggleTool,
		onToggleGroup,
		onRefreshTools,
		onToggleAgent,
		onApplyTemplate,
		onAssignToGroup,
		onRemoveFromGroup,
		onCreateGroup,
		onDeleteGroup
	}: Props = $props();

	// Live control status for the emergency stop in the Computer use section.
	// That control is the app's ONLY stop surface — the composer no longer
	// carries one — so the shared poller runs whenever this agent has the
	// capability on and the Tools tab is open, and nowhere else.
	let stopBusy = $state(false);
	$effect(() => {
		if (activeTab !== 'tools' || !profileForm.computer_use_enabled) return;
		computerUse.start();
		return () => computerUse.stop();
	});

	async function toggleComputerUseStop() {
		if (stopBusy) return;
		stopBusy = true;
		try {
			await computerUse.toggleStopped();
		} finally {
			stopBusy = false;
		}
	}

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

	// Every DB agent except the one being edited. The mode controls both the
	// context index and Task's runtime roster; provider assignments narrow it
	// again for each caller.
	const invocableAgents = $derived(
		allSubagents.filter((a) => a.id !== editingProfile?.id)
	);
	const invokeAll = $derived(profileForm.agent_invoke_mode !== 'custom');

	// Setting sources (fluid scope filter). Stored as null when every scope is
	// on (the default — the config key is omitted); an explicit list otherwise,
	// including [] for a fully filesystem-isolated agent.
	const ALL_SETTING_SOURCES = ['user', 'project', 'local'];
	const SETTING_SOURCE_ROWS = [
		{ id: 'user', label: 'User (global)', hint: 'The ~/.fluid tree: global instructions, skills, agents, commands, and installed plugins.' },
		{ id: 'project', label: 'Project', hint: 'The project walk: AGENTS.md files plus .fluid / .agents trees from the git root down to the working directory.' },
		{ id: 'local', label: 'Local', hint: 'Gitignored per-checkout overrides: .fluid/settings.local.json.' }
	];
	const settingSourcesSelected = $derived(profileForm.setting_sources ?? ALL_SETTING_SOURCES);

	function toggleSettingSource(id: string) {
		const next = settingSourcesSelected.includes(id)
			? settingSourcesSelected.filter((s) => s !== id)
			: [...settingSourcesSelected, id];
		// All three on = the default; store null so the config key is omitted.
		onFormChange('setting_sources', next.length === ALL_SETTING_SOURCES.length ? null : next);
	}
	const allInvocableSelected = $derived(
		invocableAgents.length > 0 &&
			invocableAgents.every((a) => profileForm.enabled_agents.includes(a.id))
	);
	const configuredProviders = $derived(getProviders());

	function setProviderModel(key: string, model: string) {
		onFormChange('provider_models', {
			...profileForm.provider_models,
			[key]: model
		});
	}

	function providerAssignmentCount(agent: Subagent): number {
		const config = agent.config || {};
		const keys = new Set(
			Object.entries(config.provider_models || {})
				.filter(([key, value]) => key && typeof value === 'string' && value.trim())
				.map(([key]) => key)
		);
		if (config.model) keys.add(config.provider || NATIVE_PROVIDER_KEY);
		return keys.size;
	}
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
			<div class="set-grouphead"><h3><SlidersHorizontal />Behavior</h3></div>
			<div class="set-row">
				<div class="lab"><h4>Include partial messages</h4><p>Stream partial text during AI generation for real-time feedback.</p></div>
				<div class="ctl">
					<button type="button" class="switch" class:on={profileForm.include_partial_messages} role="switch" aria-checked={profileForm.include_partial_messages} aria-label="Toggle include partial messages" onclick={() => onFormChange('include_partial_messages', !profileForm.include_partial_messages)}><i></i></button>
				</div>
			</div>
		</div>

	{:else if activeTab === 'tools'}
		<!-- Computer use keeps its own section: it carries host hardware settings
		     the picker has no place for, and the picker's computer-use group is
		     greyed out until this switch is on. -->
		<div class="set-group">
			<div class="set-grouphead"><h3><Monitor />Computer use</h3><p>Host desktop control. Off by default; the tools below only appear once it is on.</p></div>
			<div class="set-row" class:disabled={!computerUseHost.available}>
				<div class="lab">
					<h4>Enable computer use</h4>
					<p>
						Let the agent control this machine's screen — screenshots, mouse, and keyboard,
						like Claude Desktop. Shares one cursor with you. Native (non-Docker) hosts only.
						{#if !computerUseHost.available}
							<br /><strong>{COMPUTER_USE_REASONS[computerUseHost.reason] ?? `Unavailable (${computerUseHost.reason}).`}</strong>
						{/if}
					</p>
				</div>
				<div class="ctl">
					<button type="button" class="switch" class:on={profileForm.computer_use_enabled} role="switch" aria-checked={profileForm.computer_use_enabled} aria-label="Toggle computer use" disabled={!computerUseHost.available} onclick={() => onFormChange('computer_use_enabled', !profileForm.computer_use_enabled)}><i></i></button>
				</div>
			</div>
			{#if profileForm.computer_use_enabled}
				<!-- The app's only emergency stop. It halts synthetic input for the
				     whole process, not just this agent, and takes effect at once. -->
				<div class="set-row">
					<div class="lab">
						<h4>
							<OctagonX />Emergency stop
							<span class="hint">{computerUse.stopped ? 'Stopped' : computerUse.active ? `Controlling ${computerUse.displayLabel ?? 'your screen'}` : 'Idle'}</span>
						</h4>
						<p>
							Halts every screen, mouse and keyboard action immediately, across all
							sessions on this host. Resume when you want the agent to carry on.
						</p>
					</div>
					<div class="ctl">
						<button
							type="button"
							class="ed-btn"
							class:danger={!computerUse.stopped}
							disabled={stopBusy}
							onclick={toggleComputerUseStop}
						>
							{computerUse.stopped ? 'Resume control' : 'Stop control'}
						</button>
					</div>
				</div>
			{/if}
		</div>

		<ProfileToolConfig
			{toolSelectionMode}
			allowedTools={profileForm.allowed_tools}
			disallowedTools={profileForm.disallowed_tools}
			surface={toolSurface}
			loading={toolsLoading}
			refreshing={toolsRefreshing}
			computerUseEnabled={profileForm.computer_use_enabled}
			{isAdmin}
			{onToolSelectionModeChange}
			{onToggleTool}
			{onToggleGroup}
			onRefresh={onRefreshTools}
		/>

	{:else if activeTab === 'agents'}
		<div class="set-group">
			<div class="set-grouphead">
				<h3><Settings />Provider models</h3>
				<p>Choose the model used when this agent runs as a subagent on each provider. An unavailable provider is never shown to a calling model.</p>
			</div>
			<div class="set-row">
				<div class="lab"><h4>Claude</h4><p>Native Claude sessions.</p></div>
				<div class="ctl">
					<ModelPicker
						value={profileForm.provider_models[NATIVE_PROVIDER_KEY] || ''}
						providerScope={NATIVE_PROVIDER_KEY}
						allowEmpty
						onSelect={(value) => setProviderModel(NATIVE_PROVIDER_KEY, value)}
					/>
				</div>
			</div>
			{#each configuredProviders as provider (provider.id)}
				{@const display = getProviderDisplay(provider)}
				<div class="set-row">
					<div class="lab">
						<h4>{display.name}{#if display.subtext}<span class="hint"> · {display.subtext}</span>{/if}</h4>
						<p>{provider.name}</p>
					</div>
					<div class="ctl">
						<ModelPicker
							value={profileForm.provider_models[provider.id]
								? encodeProviderModel(provider.id, profileForm.provider_models[provider.id])
								: ''}
							providerScope={provider.id}
							providerModelsOnly
							allowEmpty
							onSelect={(value) => {
								const decoded = decodeProviderModel(value);
								setProviderModel(provider.id, decoded?.modelId || '');
							}}
						/>
					</div>
				</div>
			{/each}
		</div>

		<div class="set-group">
			<div class="set-grouphead grouphead-row">
				<div>
					<h3><Bot />Available subagents</h3>
					<p>Choose which DB agents are listed and dispatchable. The current provider further limits this list to agents with a model assigned above.</p>
				</div>
				{#if invocableAgents.length > 0 && !invokeAll}
					<div class="head-acts">
						<button type="button" class="lnk" onclick={() => onFormChange('enabled_agents', invocableAgents.map((a) => a.id))}>Select all</button>
						<button type="button" class="lnk muted" onclick={() => onFormChange('enabled_agents', [])}>Clear</button>
					</div>
				{/if}
			</div>
			<div class="set-row">
				<div class="lab"><h4>Roster</h4><p>Offer every eligible agent, or only the ones selected below.</p></div>
				<div class="ctl">
					<div class="seg">
						<button type="button" class:on={invokeAll} onclick={() => onFormChange('agent_invoke_mode', 'all')}>All agents</button>
						<button type="button" class:on={!invokeAll} onclick={() => onFormChange('agent_invoke_mode', 'custom')}>Selected only</button>
					</div>
				</div>
			</div>
			{#if invocableAgents.length === 0}
				<div class="set-row"><div class="lab"><p>No other DB agents to list.</p></div></div>
			{:else}
				{#each invocableAgents as agent (agent.id)}
					{@const isSelected = !invokeAll && profileForm.enabled_agents.includes(agent.id)}
					<div class="set-row" class:disabled={invokeAll}>
						<div class="lab">
							<h4>{agent.name}{#if invokeAll}<span class="hint"> · Available when provider-compatible</span>{/if}</h4>
							<p>{agent.description || 'No description'} · {providerAssignmentCount(agent)} provider assignment{providerAssignmentCount(agent) === 1 ? '' : 's'}</p>
						</div>
						<div class="ctl">
							<button type="button" class="switch" class:on={isSelected} aria-checked={isSelected} role="switch" aria-label={`Toggle ${agent.name}`} disabled={invokeAll} onclick={() => onToggleAgent(agent.id)}><i></i></button>
						</div>
					</div>
				{/each}
			{/if}
		</div>

		{#if invocableAgents.length > 0}
			<div class="ed-note">
				<Bot />
				<span>
					{#if invokeAll}
						All {invocableAgents.length} DB agents may be offered when provider-compatible.
					{:else if profileForm.enabled_agents.length === 0}
						No DB subagents are configured for this caller.
					{:else}
						{profileForm.enabled_agents.length} of {invocableAgents.length} DB agents selected{allInvocableSelected ? ' (all)' : ''}; provider assignments still apply.
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


	{:else if activeTab === 'advanced'}
		<!-- ADVANCED TAB -->
		<div class="set-group">
			<div class="set-grouphead"><h3>Limits</h3><p>Guardrails applied to every session this agent runs.</p></div>
			<div class="set-row">
				<div class="lab"><h4>Max model calls per turn</h4><p>Optional user-set cap. Empty = unlimited.</p></div>
				<div class="ctl">
					<input class="inp" type="number" value={profileForm.max_turns} oninput={(e) => { const v = (e.target as HTMLInputElement).value; onFormChange('max_turns', v ? Number(v) : null); }} placeholder="Unlimited" />
				</div>
			</div>
		</div>

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
			<div class="set-grouphead"><h3>Setting sources</h3><p>Which filesystem scopes load into the agent's context: instruction files, skills, agents, and plugins. All on by default; unchecking a scope removes what it contributes.</p></div>
			{#each SETTING_SOURCE_ROWS as row}
				<div class="set-row">
					<div class="lab"><h4>{row.label}</h4><p>{row.hint}</p></div>
					<div class="ctl">
						<input
							type="checkbox"
							checked={settingSourcesSelected.includes(row.id)}
							onchange={() => toggleSettingSource(row.id)}
						/>
					</div>
				</div>
			{/each}
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

</style>
