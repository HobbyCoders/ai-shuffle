<script lang="ts" module>
	/** The editor's folding sections, in display order. */
	export const AGENT_SECTIONS = ['prompt', 'tools', 'subagents', 'models', 'advanced'] as const;
	export type AgentSection = (typeof AGENT_SECTIONS)[number];
</script>

<script lang="ts">
	/**
	 * ProfileForm: the body of an open agent in the Agents ledger. A short
	 * essentials block (name, group, description, avatar, assistant, streaming)
	 * sits above five folding sections. Each closed section shows a live summary
	 * line, so an agent can be read without opening anything. ProfileCard owns
	 * the data, autosave and which sections are open.
	 */
	import type { Profile } from '$lib/api/client';
	import type { ToolSurface, ToolSurfaceGroup } from '$lib/api/toolSurface';
	import { Check, ChevronRight, Plus, Trash2, X } from 'lucide-svelte';
	import ProfileToolConfig from './ProfileToolConfig.svelte';
	import ProfileSystemPrompt from './ProfileSystemPrompt.svelte';
	import AgentIdentitySettings from '$lib/components/assistant/AgentIdentitySettings.svelte';
	import ModelPicker from '$lib/components/settings/ModelPicker.svelte';
	import {
		getProviders,
		getProviderDisplay,
		NATIVE_PROVIDER_KEY,
		VOICE_PROVIDER_KEY,
		subagentPickerValue
	} from '$lib/constants/models';
	import './agent-editor.css';

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
		/** Setting sources (fluid scope filter). null = all scopes (default). */
		setting_sources: string[] | null;
	}

	type ToolMode = 'all' | 'none' | 'allow' | 'disallow';

	interface Props {
		editingProfile: Profile | null;
		profileForm: ProfileFormData;
		toolSelectionMode: ToolMode;
		/** Tools the agent actually ends up with, counted against the live surface. */
		toolCount: number;
		groups: GroupState;
		allSubagents: Subagent[];
		/** Built-in system-prompt templates (GET /agents/templates), for new agents. */
		templates: AgentTemplate[];
		/** Live tool surface (GET /tools/surface) driving the picker. */
		toolSurface: ToolSurface;
		toolsLoading: boolean;
		toolsRefreshing: boolean;
		isAdmin: boolean;
		/** Sections currently unfolded. */
		openSections: string[];
		onToggleSection: (id: AgentSection) => void;
		onFormChange: (field: string, value: any) => void;
		onToolSelectionModeChange: (mode: ToolMode) => void;
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
		editingProfile,
		profileForm,
		toolSelectionMode,
		toolCount,
		groups,
		allSubagents,
		templates,
		toolSurface,
		toolsLoading,
		toolsRefreshing,
		isAdmin,
		openSections,
		onToggleSection,
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

	const SECTION_LABELS: Record<AgentSection, string> = {
		prompt: 'Prompt',
		tools: 'Tools',
		subagents: 'Subagents',
		models: 'Models',
		advanced: 'Advanced'
	};

	// ── Group ────────────────────────────────────────────────────────────
	let creatingGroup = $state(false);
	let newGroupName = $state('');
	const currentGroup = $derived(
		editingProfile ? (groups?.profiles?.assignments?.[editingProfile.id] ?? '') : ''
	);

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

	function deleteCurrentGroup() {
		if (!editingProfile || !currentGroup) return;
		onRemoveFromGroup(editingProfile.id);
		onDeleteGroup('profiles', currentGroup);
	}

	// ── Subagents ────────────────────────────────────────────────────────
	// Every DB agent except the one being edited. The mode controls both the
	// context index and Task's runtime roster; provider assignments narrow it
	// again for each caller.
	const invocableAgents = $derived(allSubagents.filter((a) => a.id !== editingProfile?.id));
	const invokeAll = $derived(profileForm.agent_invoke_mode !== 'custom');
	const selectedAgentCount = $derived(
		profileForm.enabled_agents.filter((id) => invocableAgents.some((a) => a.id === id)).length
	);

	function providerAssignmentCount(agent: Subagent): number {
		const config = agent.config || {};
		// The voice entry is a leg choice, not a provider row — it does not
		// count as one of the provider assignments a calling model sees.
		const keys = new Set(
			Object.entries(config.provider_models || {})
				.filter(([key, value]) => key && key !== VOICE_PROVIDER_KEY && typeof value === 'string' && value.trim())
				.map(([key]) => key)
		);
		if (config.model) keys.add(config.provider || NATIVE_PROVIDER_KEY);
		return keys.size;
	}

	// ── Models ───────────────────────────────────────────────────────────
	const configuredProviders = $derived(getProviders());

	function setProviderModel(key: string, model: string) {
		onFormChange('provider_models', { ...profileForm.provider_models, [key]: model });
	}

	// ── Advanced: setting sources ────────────────────────────────────────
	// Stored as null when every scope is on (the default; the config key is
	// omitted); an explicit list otherwise, including [] for a fully
	// filesystem-isolated agent.
	const ALL_SETTING_SOURCES = ['user', 'project', 'local'];
	const SETTING_SOURCE_ROWS = [
		{ id: 'user', label: 'User', hint: 'The ~/.fluid tree: instructions, skills, agents, plugins.' },
		{ id: 'project', label: 'Project', hint: 'AGENTS.md and .fluid trees from the git root down.' },
		{ id: 'local', label: 'Local', hint: 'Gitignored overrides in .fluid/settings.local.json.' }
	];
	const settingSourcesSelected = $derived(profileForm.setting_sources ?? ALL_SETTING_SOURCES);

	function toggleSettingSource(id: string) {
		const next = settingSourcesSelected.includes(id)
			? settingSourcesSelected.filter((s) => s !== id)
			: [...settingSourcesSelected, id];
		// All three on = the default; store null so the config key is omitted.
		onFormChange('setting_sources', next.length === ALL_SETTING_SOURCES.length ? null : next);
	}

	// ── Live summaries for the closed section lines ──────────────────────
	const TOOL_MODE_LABELS: Record<ToolMode, string> = {
		all: 'All tools',
		none: 'No tools',
		allow: 'Only selected',
		disallow: 'All except selected'
	};

	const summaries = $derived.by((): Record<AgentSection, { lead: string; rest?: string }> => {
		const prompt = profileForm.system_prompt_content;
		const firstLine = prompt.split('\n').find((l) => l.trim())?.trim() ?? '';

		const models: string[] = [];
		if (profileForm.provider_models[NATIVE_PROVIDER_KEY]?.trim()) models.push('Claude');
		for (const p of configuredProviders) {
			if (profileForm.provider_models[p.id]?.trim()) models.push(getProviderDisplay(p).name);
		}
		if (profileForm.provider_models[VOICE_PROVIDER_KEY]?.trim()) models.push('Voice');

		const sources =
			profileForm.setting_sources === null
				? 'All setting sources'
				: profileForm.setting_sources.length === 0
					? 'No setting sources'
					: profileForm.setting_sources.map((s) => s[0].toUpperCase() + s.slice(1)).join(', ');

		return {
			prompt: prompt.length
				? { lead: `${prompt.length.toLocaleString()} / 10,000`, rest: firstLine }
				: { lead: 'Empty', rest: 'Runtime context still added' },
			tools: {
				lead: `${toolCount} tool${toolCount === 1 ? '' : 's'}`,
				rest: TOOL_MODE_LABELS[toolSelectionMode]
			},
			subagents:
				invocableAgents.length === 0
					? { lead: 'No other agents' }
					: invokeAll
						? { lead: `All ${invocableAgents.length} agents` }
						: { lead: `${selectedAgentCount} of ${invocableAgents.length}`, rest: 'Selected only' },
			models: models.length
				? { lead: models.join(', ') }
				: { lead: 'None set', rest: 'Not offered as a subagent' },
			advanced: {
				lead: profileForm.max_turns ? `${profileForm.max_turns} calls per turn` : 'Unlimited calls',
				rest: [sources, profileForm.cwd.trim()].filter(Boolean).join(' · ')
			}
		};
	});

	const nameMissing = $derived(!profileForm.name.trim());
</script>

<div class="agx">
	<!-- Essentials -->
	<div class="agx-ess">
		<div class="fx">
			<label class="fx-l" for="agx-name">Name</label>
			<input
				id="agx-name"
				class="inp"
				value={profileForm.name}
				oninput={(e) => onFormChange('name', (e.target as HTMLInputElement).value)}
				placeholder="My Custom Agent"
				aria-invalid={nameMissing && !!editingProfile}
				aria-describedby={nameMissing ? 'agx-name-msg' : undefined}
			/>
			{#if nameMissing}
				<span id="agx-name-msg" class="fx-msg" class:err={!!editingProfile}>Name required to save</span>
			{/if}
		</div>

		{#if editingProfile}
			{@const profileId = editingProfile.id}
			<div class="fx">
				<span class="fx-l" id="agx-group-l">Group</span>
				{#if creatingGroup}
					<div class="fx-line">
						<!-- svelte-ignore a11y_autofocus -->
						<input
							type="text"
							class="inp grow"
							placeholder="New group name"
							aria-label="New group name"
							bind:value={newGroupName}
							onkeydown={(e) => {
								if (e.key === 'Enter') { e.preventDefault(); confirmGroupCreate(profileId); }
								if (e.key === 'Escape') { e.preventDefault(); cancelGroupCreate(); }
							}}
							autofocus
						/>
						<button type="button" class="agx-ib" title="Create group" aria-label="Create group" onclick={() => confirmGroupCreate(profileId)}><Check size={14} /></button>
						<button type="button" class="agx-ib" title="Cancel" aria-label="Cancel new group" onclick={cancelGroupCreate}><X size={14} /></button>
					</div>
				{:else}
					<div class="fx-line">
						<select
							class="sel grow"
							aria-labelledby="agx-group-l"
							value={currentGroup}
							onchange={(e) => {
								const v = (e.target as HTMLSelectElement).value;
								if (v) onAssignToGroup(profileId, v);
								else onRemoveFromGroup(profileId);
							}}
						>
							<option value="">No group</option>
							{#each groups?.profiles?.groups ?? [] as group}
								<option value={group.name}>{group.name}</option>
							{/each}
						</select>
						<button type="button" class="agx-ib" title="New group" aria-label="New group" onclick={() => { creatingGroup = true; newGroupName = ''; }}><Plus size={14} /></button>
						{#if currentGroup}
							<button type="button" class="agx-ib danger" title="Delete group" aria-label="Delete group" onclick={deleteCurrentGroup}><Trash2 size={14} /></button>
						{/if}
					</div>
				{/if}
			</div>
		{/if}

		<label class="fx wide">
			<span class="fx-l">Description</span>
			<textarea
				class="fld"
				rows={2}
				value={profileForm.description}
				oninput={(e) => onFormChange('description', (e.target as HTMLTextAreaElement).value)}
				placeholder="A coding assistant for frontend work"
			></textarea>
		</label>

		{#if editingProfile && isAdmin}
			<div class="fx">
				{#key editingProfile.id}<AgentIdentitySettings agentId={editingProfile.id} builtin={editingProfile.is_builtin} />{/key}
			</div>
		{/if}

		<div class="fx fx-switch">
			<span class="fx-text">
				<span class="fx-t">Stream partial messages</span>
				<span class="fx-msg">Show text as it generates.</span>
			</span>
			<button
				type="button"
				class="switch"
				class:on={profileForm.include_partial_messages}
				role="switch"
				aria-checked={profileForm.include_partial_messages}
				aria-label="Stream partial messages"
				onclick={() => onFormChange('include_partial_messages', !profileForm.include_partial_messages)}
			><i></i></button>
		</div>
	</div>

	<!-- Folding sections -->
	{#each AGENT_SECTIONS as id (id)}
		{@const open = openSections.includes(id)}
		{@const sum = summaries[id]}
		<section class="dx" class:open id={`agx-sec-${id}`}>
			<button
				type="button"
				class="dx-head"
				aria-expanded={open}
				aria-controls={`agx-body-${id}`}
				onclick={() => onToggleSection(id)}
			>
				<ChevronRight size={15} class="dx-chev" />
				<span class="dx-label">{SECTION_LABELS[id]}</span>
				<span class="dx-sum">
					<span class="dx-lead" class:mono={id === 'prompt' && profileForm.system_prompt_content.length > 0}>{sum.lead}</span>
					{#if sum.rest}<span class="dx-rest">{sum.rest}</span>{/if}
				</span>
			</button>

			{#if open}
				<div class="dx-body" id={`agx-body-${id}`}>
					{#if id === 'prompt'}
						{#if !editingProfile && templates.length > 0}
							<div class="set-group">
								<div class="set-row">
									<div class="lab"><h4>Start from a template</h4><p>Replaces the prompt below.</p></div>
									<div class="ctl">
										<select
											class="sel"
											aria-label="Prompt template"
											value=""
											onchange={(e) => {
												const target = e.target as HTMLSelectElement;
												if (target.value) onApplyTemplate(target.value);
												target.value = '';
											}}
										>
											<option value="">Choose a template…</option>
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
					{:else if id === 'tools'}
						<!-- Computer use is a GLOBAL toggle (Settings > Security), not agent
						     configuration: the picker's computer-use group follows it. -->
						<ProfileToolConfig
							{toolSelectionMode}
							allowedTools={profileForm.allowed_tools}
							disallowedTools={profileForm.disallowed_tools}
							surface={toolSurface}
							loading={toolsLoading}
							refreshing={toolsRefreshing}
							{isAdmin}
							{onToolSelectionModeChange}
							{onToggleTool}
							{onToggleGroup}
							onRefresh={onRefreshTools}
						/>
					{:else if id === 'subagents'}
						<div class="set-group">
							<div class="set-row keep">
								<div class="lab">
									<h4>Roster</h4>
									<p>Agents this one can hand work to. Each caller's provider still applies.</p>
								</div>
								<div class="ctl">
									<div class="seg" role="radiogroup" aria-label="Subagent roster">
										<button type="button" role="radio" aria-checked={invokeAll} class:on={invokeAll} onclick={() => onFormChange('agent_invoke_mode', 'all')}>All agents</button>
										<button type="button" role="radio" aria-checked={!invokeAll} class:on={!invokeAll} onclick={() => onFormChange('agent_invoke_mode', 'custom')}>Selected only</button>
									</div>
								</div>
							</div>
						</div>
						{#if invocableAgents.length === 0}
							<p class="agx-empty">No other agents yet.</p>
						{:else}
							{#if !invokeAll}
								<div class="agx-bulk">
									<button type="button" class="agx-lnk" onclick={() => onFormChange('enabled_agents', invocableAgents.map((a) => a.id))}>Select all</button>
									<button type="button" class="agx-lnk muted" onclick={() => onFormChange('enabled_agents', [])}>Clear</button>
								</div>
							{/if}
							<div class="agx-grid" class:dim={invokeAll}>
								{#each invocableAgents as agent (agent.id)}
									{@const isSelected = invokeAll || profileForm.enabled_agents.includes(agent.id)}
									{@const assignments = providerAssignmentCount(agent)}
									<div class="agx-cell">
										<span class="agx-cell-t">
											<span class="agx-cell-n">{agent.name}</span>
											<span class="agx-cell-d">{assignments} provider{assignments === 1 ? '' : 's'}{agent.description ? ` · ${agent.description}` : ''}</span>
										</span>
										<button
											type="button"
											class="switch"
											class:on={isSelected}
											role="switch"
											aria-checked={isSelected}
											aria-label={`Toggle ${agent.name}`}
											disabled={invokeAll}
											onclick={() => onToggleAgent(agent.id)}
										><i></i></button>
									</div>
								{/each}
							</div>
						{/if}
					{:else if id === 'models'}
						<p class="agx-intro">The model this agent runs on when a caller on that provider dispatches it. Leave a row empty to hide it there.</p>
						<div class="set-group">
							<div class="set-row">
								<div class="lab"><h4>Claude</h4><p>Callers on native Claude.</p></div>
								<div class="ctl">
									<ModelPicker
										value={subagentPickerValue(NATIVE_PROVIDER_KEY, profileForm.provider_models[NATIVE_PROVIDER_KEY])}
										providerScope={NATIVE_PROVIDER_KEY}
										allowEmpty
										allowCustom={false}
										onSelect={(value) => setProviderModel(NATIVE_PROVIDER_KEY, value)}
									/>
								</div>
							</div>
							{#each configuredProviders as provider (provider.id)}
								{@const display = getProviderDisplay(provider)}
								<div class="set-row">
									<div class="lab">
										<h4>{display.name}{#if display.subtext}<span class="hint">{display.subtext}</span>{/if}</h4>
										<p>{provider.name}</p>
									</div>
									<div class="ctl">
										<ModelPicker
											value={subagentPickerValue(provider.id, profileForm.provider_models[provider.id])}
											allowEmpty
											allowCustom={false}
											onSelect={(value) => setProviderModel(provider.id, value)}
										/>
									</div>
								</div>
							{/each}
							<!-- Voice is not a provider: a voice call is funded by an OpenAI
							     Realtime row, but the workers it dispatches can run on any leg.
							     This picks that leg and is what makes the agent dispatchable from
							     a call at all. -->
							<div class="set-row">
								<div class="lab"><h4>Voice</h4><p>When the voice agent hands it work.</p></div>
								<div class="ctl">
									<ModelPicker
										value={profileForm.provider_models[VOICE_PROVIDER_KEY] || ''}
										allowEmpty
										emptyLabel="Not available in voice calls"
										allowCustom={false}
										onSelect={(value) => setProviderModel(VOICE_PROVIDER_KEY, value)}
									/>
								</div>
							</div>
						</div>
					{:else if id === 'advanced'}
						<div class="set-group">
							<div class="set-row">
								<div class="lab"><h4>Max model calls per turn</h4><p>Empty means unlimited.</p></div>
								<div class="ctl">
									<input
										class="inp num"
										type="number"
										min="1"
										aria-label="Max model calls per turn"
										value={profileForm.max_turns}
										oninput={(e) => { const v = (e.target as HTMLInputElement).value; onFormChange('max_turns', v ? Number(v) : null); }}
										placeholder="Unlimited"
									/>
								</div>
							</div>
							<div class="set-row stack">
								<div class="lab"><h4>Working directory</h4></div>
								<div class="ctl"><input class="inp mono" aria-label="Working directory" value={profileForm.cwd} oninput={(e) => onFormChange('cwd', (e.target as HTMLInputElement).value)} placeholder="/home/user/projects/my-api" /></div>
							</div>
							<div class="set-row stack">
								<div class="lab"><h4>Additional directories</h4><p>Comma-separated.</p></div>
								<div class="ctl"><input class="inp mono" aria-label="Additional directories" value={profileForm.add_dirs} oninput={(e) => onFormChange('add_dirs', (e.target as HTMLInputElement).value)} placeholder="/home/user/notes, /home/user/docs" /></div>
							</div>
						</div>
						<div class="set-group">
							<div class="set-grouphead">
								<h3>Setting sources</h3>
								<p>Which file scopes load into context. Turning one off removes what it adds.</p>
							</div>
							{#each SETTING_SOURCE_ROWS as row}
								{@const on = settingSourcesSelected.includes(row.id)}
								<div class="set-row keep">
									<div class="lab"><h4>{row.label}</h4><p>{row.hint}</p></div>
									<div class="ctl">
										<button type="button" class="switch" class:on role="switch" aria-checked={on} aria-label={`${row.label} setting source`} onclick={() => toggleSettingSource(row.id)}><i></i></button>
									</div>
								</div>
							{/each}
						</div>
					{/if}
				</div>
			{/if}
		</section>
	{/each}
</div>

<style>
	.agx {
		container: agx / inline-size;
		display: flex;
		flex-direction: column;
		min-width: 0;
	}

	/* ── Essentials ── */
	.agx-ess {
		display: grid;
		grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
		gap: 14px 20px;
		padding: 4px 0 16px;
	}
	.fx {
		display: flex;
		flex-direction: column;
		gap: 5px;
		min-width: 0;
	}
	.fx.wide {
		grid-column: 1 / -1;
	}
	.fx-l {
		font-size: 12px;
		font-weight: 500;
		color: var(--muted-foreground);
	}
	.fx .inp {
		width: 100%;
	}
	.fx-line {
		display: flex;
		align-items: center;
		gap: 6px;
	}
	.fx-line :global(.grow) {
		flex: 1;
		min-width: 0;
	}
	.fx-msg {
		font-size: 11.5px;
		color: var(--muted-foreground);
	}
	.fx-msg.err {
		color: var(--destructive);
	}
	.fx-switch {
		flex-direction: row;
		align-items: center;
		justify-content: space-between;
		gap: 12px;
		align-self: start;
	}
	.fx-text {
		display: flex;
		flex-direction: column;
		gap: 2px;
		min-width: 0;
	}
	.fx-t {
		font-size: 13px;
		font-weight: 550;
		color: var(--foreground);
	}
	.agx-ib {
		display: grid;
		place-items: center;
		flex-shrink: 0;
		width: 34px;
		height: 34px;
		border: 1px solid var(--border);
		border-radius: 7px;
		background: transparent;
		color: var(--muted-foreground);
		cursor: pointer;
	}
	.agx-ib:hover {
		background: var(--hover-overlay, var(--muted));
		color: var(--foreground);
	}
	.agx-ib.danger:hover {
		color: var(--destructive);
		background: color-mix(in oklch, var(--destructive) 12%, transparent);
	}

	/* ── Folding sections ── */
	.dx {
		border-top: 1px solid var(--border);
	}
	.dx-head {
		display: flex;
		align-items: baseline;
		gap: 10px;
		width: 100%;
		min-height: 42px;
		padding: 11px 4px;
		border: none;
		background: none;
		font: inherit;
		color: var(--foreground);
		text-align: left;
		cursor: pointer;
	}
	.dx-head:hover .dx-label {
		color: var(--primary);
	}
	.dx-head :global(.dx-chev) {
		flex-shrink: 0;
		align-self: center;
		color: var(--muted-foreground);
		transition: transform 0.16s;
	}
	.dx.open .dx-head :global(.dx-chev) {
		transform: rotate(90deg);
	}
	.dx-label {
		flex-shrink: 0;
		width: 88px;
		font-size: 13px;
		font-weight: 600;
	}
	.dx-sum {
		flex: 1;
		min-width: 0;
		display: flex;
		align-items: baseline;
		gap: 10px;
		font-size: 12.5px;
		color: var(--muted-foreground);
		overflow: hidden;
	}
	.dx-lead {
		flex-shrink: 0;
		color: var(--foreground);
		font-weight: 500;
	}
	.dx-lead.mono {
		font-family: var(--font-mono, monospace);
		font-size: 11.5px;
	}
	.dx-rest {
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.dx-body {
		padding: 2px 4px 18px 29px;
	}
	.dx-head:focus-visible,
	.agx-ib:focus-visible,
	.agx-lnk:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 1px;
		border-radius: 6px;
	}

	/* ── Subagents ── */
	.agx-bulk {
		display: flex;
		gap: 14px;
		margin: 2px 0 8px;
	}
	.agx-lnk {
		padding: 0;
		border: none;
		background: none;
		font: inherit;
		font-size: 12.5px;
		font-weight: 600;
		color: var(--primary);
		cursor: pointer;
	}
	.agx-lnk.muted {
		color: var(--muted-foreground);
		font-weight: 500;
	}
	.agx-lnk:hover {
		text-decoration: underline;
	}
	.agx-grid {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
		gap: 4px 20px;
	}
	.agx-cell {
		display: flex;
		align-items: center;
		gap: 12px;
		padding: 7px 0;
		border-bottom: 1px solid color-mix(in oklch, var(--border) 70%, transparent);
	}
	.agx-cell-t {
		flex: 1;
		min-width: 0;
		display: flex;
		flex-direction: column;
		gap: 1px;
	}
	.agx-cell-n {
		font-size: 13px;
		font-weight: 550;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.agx-cell-d {
		font-size: 11.5px;
		color: var(--muted-foreground);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.agx-grid.dim .agx-cell-n {
		opacity: 0.7;
	}
	.agx-grid.dim .switch:disabled {
		opacity: 0.55;
	}
	.agx-intro,
	.agx-empty {
		margin: 0 0 8px;
		font-size: 12px;
		line-height: 1.5;
		color: var(--muted-foreground);
	}
	.inp.num {
		width: 120px;
	}

	@container agx (max-width: 560px) {
		.agx-ess {
			grid-template-columns: minmax(0, 1fr);
		}
		.dx-label {
			width: auto;
		}
		.dx-rest {
			display: none;
		}
		.dx-body {
			padding-left: 4px;
		}
	}
	@media (prefers-reduced-motion: reduce) {
		.dx-head :global(.dx-chev) {
			transition: none;
		}
	}
</style>
