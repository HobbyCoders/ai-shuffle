<script lang="ts">
	import {
		automationFallbackModel,
		savedFallbackModel,
		type Automation,
		type TriggerItem,
		type ActionStep,
		type ActionStepType,
		type OutputType
	} from '$lib/api/automations';
	import ModelPicker from '$lib/components/settings/ModelPicker.svelte';
	import type { Profile } from '$lib/api/client';
	import type { Project } from '$lib/stores/types';
	import type { Connector, ConnectorTypeInfo } from '$lib/api/connectors';
	import { slide } from 'svelte/transition';
	import {
		Plus,
		Trash2,
		ChevronDown,
		ChevronRight,
		Zap,
		CalendarClock,
		Settings,
		FileText,
		DollarSign
	} from 'lucide-svelte';
	import AutomationTriggerConfig from './AutomationTriggerConfig.svelte';
	import Banner from '$lib/components/Banner.svelte';
	import { layout } from '$lib/stores/layout.svelte';
	import { onMount } from 'svelte';
	import { getClaudeAuth, type ClaudeAuthMode } from '$lib/api/claude-auth';
	import { openSettings } from '$lib/settings/navigation.svelte';

	interface Props {
		currentView: 'create' | 'edit';
		selectedAutomationId: string | null;
		formError: string;
		formName: string;
		formDescription: string;
		formTriggers: TriggerItem[];
		formPrompt: string;
		formProfileId: string;
		formProjectId: string;
		formOutputType: OutputType;
		formOutputConfig: Record<string, any>;
		formMaxCostPerRun: string;
		formMaxCostPerDay: string;
		formMaxCostTotal: string;
		formMaxRunsPerDay: string;
		formMaxTurns: string;
		formTimeoutSeconds: string;
		formRequiresApproval: boolean;
		/** Model picker value; '' = no choice of its own. */
		formModel: string;
		formActionSteps: ActionStep[];
		/** The saved automation when editing, for its fallback / unavailable model. */
		savedAutomation?: Automation | null;
		profiles: Profile[];
		projects: Project[];
		connectors: Connector[];
		connectorTypes: ConnectorTypeInfo[];
		connectorsWithTriggers: Connector[];
		connectorsWithActions: Connector[];
		expandedTriggers: Set<number>;
		expandedActions: Set<number>;
		collapsedSections: Set<string>;
		timezone: string;
		onUpdateFormError: (val: string) => void;
		onUpdateFormName: (val: string) => void;
		onUpdateFormDescription: (val: string) => void;
		onUpdateFormTriggers: (val: TriggerItem[]) => void;
		onUpdateFormPrompt: (val: string) => void;
		onUpdateFormProfileId: (val: string) => void;
		onUpdateFormProjectId: (val: string) => void;
		onUpdateFormOutputType: (val: OutputType) => void;
		onUpdateFormOutputConfig: (val: Record<string, any>) => void;
		onUpdateFormMaxCostPerRun: (val: string) => void;
		onUpdateFormMaxCostPerDay: (val: string) => void;
		onUpdateFormMaxCostTotal: (val: string) => void;
		onUpdateFormMaxRunsPerDay: (val: string) => void;
		onUpdateFormMaxTurns: (val: string) => void;
		onUpdateFormTimeoutSeconds: (val: string) => void;
		onUpdateFormRequiresApproval: (val: boolean) => void;
		onUpdateFormModel: (val: string) => void;
		onUpdateFormActionSteps: (val: ActionStep[]) => void;
		onToggleSection: (id: string) => void;
		onToggleTriggerExpand: (idx: number) => void;
		onToggleActionExpand: (idx: number) => void;
	}

	let {
		currentView,
		selectedAutomationId,
		formError,
		formName,
		formDescription,
		formTriggers,
		formPrompt,
		formProfileId,
		formProjectId,
		formOutputType,
		formOutputConfig,
		formMaxCostPerRun,
		formMaxCostPerDay,
		formMaxCostTotal,
		formMaxRunsPerDay,
		formMaxTurns,
		formTimeoutSeconds,
		formRequiresApproval,
		formModel,
		formActionSteps,
		savedAutomation = null,
		profiles,
		projects,
		connectors,
		connectorTypes,
		connectorsWithTriggers,
		connectorsWithActions,
		expandedTriggers,
		expandedActions,
		collapsedSections,
		timezone,
		onUpdateFormError,
		onUpdateFormName,
		onUpdateFormDescription,
		onUpdateFormTriggers,
		onUpdateFormPrompt,
		onUpdateFormProfileId,
		onUpdateFormProjectId,
		onUpdateFormOutputType,
		onUpdateFormOutputConfig,
		onUpdateFormMaxCostPerRun,
		onUpdateFormMaxCostPerDay,
		onUpdateFormMaxCostTotal,
		onUpdateFormMaxRunsPerDay,
		onUpdateFormMaxTurns,
		onUpdateFormTimeoutSeconds,
		onUpdateFormRequiresApproval,
		onUpdateFormModel,
		onUpdateFormActionSteps,
		onToggleSection,
		onToggleTriggerExpand,
		onToggleActionExpand
	}: Props = $props();

	// Subscription mode: an automation runs on the user's Claude plan while
	// they are away. Recommend an API key before it is saved (the server adds
	// the same notice to the save response).
	let claudeMode = $state<ClaudeAuthMode | null>(null);
	onMount(() => {
		getClaudeAuth()
			.then((summary) => (claudeMode = summary.mode))
			.catch(() => {
				// No notice when the mode cannot be read.
			});
	});
	const scheduledRunNotice = $derived.by(() => {
		if (claudeMode !== 'subscription') return '';
		const profile = profiles.find((p) => p.id === formProfileId);
		if (profile?.config?.provider) return '';
		return 'Claude Code is in subscription mode, so this automation runs on your Claude plan while you are away. Use an API key for scheduled runs (Settings > Providers > Claude). The loop guard holds unattended turns past its budget until you confirm them.';
	});

	// What runs use with no model of their own: the app default, unless the
	// selected agent still carries a legacy saved model. An edited automation on
	// its saved agent takes the server's report; otherwise it is read from the
	// selected agent.
	const fallbackModel = $derived.by(() => {
		if (
			savedAutomation?.model_source &&
			!savedAutomation.model &&
			savedAutomation.profile_id === formProfileId
		) {
			return savedFallbackModel(savedAutomation);
		}
		return automationFallbackModel(profiles.find((p) => p.id === formProfileId)?.config);
	});
	const modelEmptyLabel = $derived(fallbackModel.legacy ? 'Saved on agent (legacy)' : 'App default');

	// What the Model row says under the picker; an unchanged choice that is
	// gone shows why runs fail.
	const modelHint = $derived.by(() => {
		const unchanged = savedAutomation && (savedAutomation.model || '') === formModel;
		if (unchanged && savedAutomation?.model_error) return savedAutomation.model_error;
		if (formModel) return 'Every run uses this model.';
		return fallbackModel.legacy
			? `Uses ${fallbackModel.label}, saved on this agent (legacy).`
			: `Uses ${fallbackModel.label}, the app default.`;
	});
	const modelHintIsError = $derived(
		!!savedAutomation?.model_error && (savedAutomation.model || '') === formModel
	);

	function addActionStep(type: ActionStepType = 'connector_action') {
		const defaults: Record<ActionStepType, ActionStep> = {
			connector_action: { type: 'connector_action', connector_id: '', action_id: '', params: {} },
			send_prompt: { type: 'send_prompt', prompt: '' },
			webhook_call: { type: 'webhook_call', url: '', method: 'POST', body: '' },
			delay: { type: 'delay', seconds: 5 }
		};
		onUpdateFormActionSteps([...formActionSteps, defaults[type]]);
	}

	function removeActionStep(index: number) {
		onUpdateFormActionSteps(formActionSteps.filter((_, i) => i !== index));
	}

	function changeActionStepType(index: number, newType: ActionStepType) {
		const defaults: Record<ActionStepType, ActionStep> = {
			connector_action: { type: 'connector_action', connector_id: '', action_id: '', params: {} },
			send_prompt: { type: 'send_prompt', prompt: '' },
			webhook_call: { type: 'webhook_call', url: '', method: 'POST', body: '' },
			delay: { type: 'delay', seconds: 5 }
		};
		const updated = [...formActionSteps];
		updated[index] = defaults[newType];
		onUpdateFormActionSteps(updated);
	}

	function actionStepTypeLabel(type: ActionStepType): string {
		switch (type) {
			case 'connector_action': return 'Connector Action';
			case 'send_prompt': return 'AI Prompt';
			case 'webhook_call': return 'Webhook Call';
			case 'delay': return 'Delay';
		}
	}

	function getActionSummary(step: ActionStep): string {
		switch (step.type) {
			case 'connector_action': return step.action_id ? `${step.action_id}` : 'Not configured';
			case 'send_prompt': return step.prompt ? step.prompt.slice(0, 50) + (step.prompt.length > 50 ? '...' : '') : 'Empty prompt';
			case 'webhook_call': return step.url || 'No URL';
			case 'delay': return `Wait ${step.seconds ?? 5}s`;
			default: return '';
		}
	}

	function getActionsForConnector(connectorId: string) {
		const connector = connectors.find(c => c.id === connectorId);
		if (!connector) return [];
		const typeInfo = connectorTypes.find(t => t.type === connector.type);
		return typeInfo?.actions || [];
	}

	function updateActionStep(index: number, step: ActionStep) {
		const updated = [...formActionSteps];
		updated[index] = step;
		onUpdateFormActionSteps(updated);
	}
</script>

{#if formError}
	<div transition:slide={{ duration: 150 }}>
		<Banner severity="error" message={formError} dismissible onDismiss={() => onUpdateFormError('')} />
	</div>
{/if}

{#if scheduledRunNotice}
	<div data-testid="automation-api-key-notice">
		<Banner
			severity="warning"
			message={scheduledRunNotice}
			action={{ label: 'Open Claude settings', onClick: () => openSettings({ category: 'providers', item: 'provider:claude', anchor: 'providers.claudeMode' }) }}
		/>
	</div>
{/if}

<!-- Basics -->
<div class="set-group">
	<button type="button" class="set-grouphead head-toggle" class:collapsed={collapsedSections.has('basics')} onclick={() => onToggleSection('basics')}>
		<h3><CalendarClock />Basics</h3>
		<ChevronDown size={15} class={collapsedSections.has('basics') ? 'gh-chev' : 'gh-chev open'} />
	</button>
	{#if !collapsedSections.has('basics')}
		<div class="set-row">
			<div class="lab"><h4>Name <span class="req">*</span></h4><p>A label to recognize this automation.</p></div>
			<div class="ctl"><input type="text" class="inp" value={formName} oninput={(e) => onUpdateFormName(e.currentTarget.value)} placeholder="e.g. Daily report generator" /></div>
		</div>
		<div class="set-row">
			<div class="lab"><h4>Description</h4><p>What this automation does. Optional.</p></div>
			<div class="ctl"><input type="text" class="inp" value={formDescription} oninput={(e) => onUpdateFormDescription(e.currentTarget.value)} placeholder="Optional description…" /></div>
		</div>
	{/if}
</div>

<!-- Triggers -->
<div class="set-group">
	<button type="button" class="set-grouphead head-toggle" class:collapsed={collapsedSections.has('triggers')} onclick={() => onToggleSection('triggers')}>
		<span class="gh-left"><h3><Zap />Triggers</h3><span class="pill-s acc">{formTriggers.length}</span></span>
		<ChevronDown size={15} class={collapsedSections.has('triggers') ? 'gh-chev' : 'gh-chev open'} />
	</button>
	{#if !collapsedSections.has('triggers')}
		<div class="set-row stack">
			<div class="ctl">
				<AutomationTriggerConfig
					triggers={formTriggers}
					{expandedTriggers}
					{connectors}
					{connectorTypes}
					{connectorsWithTriggers}
					{currentView}
					{selectedAutomationId}
					{timezone}
					onUpdateTriggers={onUpdateFormTriggers}
					onToggleTriggerExpand={onToggleTriggerExpand}
				/>
			</div>
		</div>
	{/if}
</div>

<!-- Prompt & Actions -->
<div class="set-group">
	<button type="button" class="set-grouphead head-toggle" class:collapsed={collapsedSections.has('prompt')} onclick={() => onToggleSection('prompt')}>
		<span class="gh-left"><h3><FileText />Prompt &amp; actions</h3>{#if formActionSteps.length > 0}<span class="pill-s acc">{formActionSteps.length} action{formActionSteps.length !== 1 ? 's' : ''}</span>{/if}</span>
		<ChevronDown size={15} class={collapsedSections.has('prompt') ? 'gh-chev' : 'gh-chev open'} />
	</button>
	{#if !collapsedSections.has('prompt')}
		<div class="set-row stack">
			<div class="lab"><h4>Prompt {#if formActionSteps.length === 0}<span class="req">*</span>{/if}</h4>
				<p>{#if formActionSteps.length > 0}Optional when action steps are configured. Runs before action steps.{:else}The main instruction sent to the AI agent when this automation runs.{/if}</p>
			</div>
			<div class="ctl">
				<textarea class="fld code" value={formPrompt} oninput={(e) => onUpdateFormPrompt(e.currentTarget.value)} placeholder="Enter the prompt template for this automation…"></textarea>
			</div>
		</div>

		<div class="set-row stack">
			<div class="lab"><h4>Action steps</h4><p>Run connectors, prompts, webhooks, or delays in sequence after the prompt.</p></div>
			<div class="ctl">
				{#each formActionSteps as step, i}
					<div class="af-item-card" transition:slide={{ duration: 150 }}>
						<div class="af-item-header" role="button" tabindex="0" onclick={() => onToggleActionExpand(i)} onkeydown={(e) => { if (e.key === 'Enter' || e.key === ' ') onToggleActionExpand(i); }}>
							<div class="af-item-header-left">
								{#if expandedActions.has(i)}
									<ChevronDown size={14} />
								{:else}
									<ChevronRight size={14} />
								{/if}
								<span class="af-item-step-num">{i + 1}</span>
								<span class="af-item-type-badge">{actionStepTypeLabel(step.type)}</span>
								{#if !expandedActions.has(i)}
									<span class="af-item-summary">{getActionSummary(step)}</span>
								{/if}
							</div>
							<div class="af-item-header-actions">
								<button type="button" class="icon-x" aria-label="Remove step" onclick={(e) => { e.stopPropagation(); removeActionStep(i); }}>
									<Trash2 size={13} />
								</button>
							</div>
						</div>

						{#if expandedActions.has(i)}
							<div class="af-item-body" transition:slide={{ duration: 150 }}>
								<select class="af-input"
									value={step.type}
									onchange={(e) => changeActionStepType(i, e.currentTarget.value as ActionStepType)}>
									<option value="connector_action">Connector Action</option>
									<option value="send_prompt">AI Prompt</option>
									<option value="webhook_call">Webhook Call</option>
									<option value="delay">Delay</option>
								</select>

								{#if step.type === 'connector_action'}
									<select class="af-input"
										value={step.connector_id || ''}
										onchange={(e) => { const updated = { ...step, connector_id: e.currentTarget.value, action_id: '', params: {} }; updateActionStep(i, updated); }}>
										<option value="">Select connector…</option>
										{#each connectorsWithActions as c}
											<option value={c.id}>{c.name} ({c.type})</option>
										{/each}
									</select>
									{#if step.connector_id}
										<select class="af-input"
											value={step.action_id || ''}
											onchange={(e) => { const updated = { ...step, action_id: e.currentTarget.value, params: {} }; updateActionStep(i, updated); }}>
											<option value="">Select action…</option>
											{#each getActionsForConnector(step.connector_id) as action}
												<option value={action.id}>{action.name}</option>
											{/each}
										</select>
									{/if}
									{#if step.action_id}
										{@const actionDef = getActionsForConnector(step.connector_id || '').find(a => a.id === step.action_id)}
										{#if actionDef}
											<p class="af-hint">{actionDef.description}</p>
											{#if actionDef.input_schema?.properties}
												<div class="af-param-list">
													{#each Object.entries(actionDef.input_schema.properties) as [key, schemaDef]}
														{@const schemaObj = schemaDef as Record<string, any>}
														<label class="af-mini-label" for="auto-param-{i}-{key}">{key}</label>
														<input type="text" class="af-input" id="auto-param-{i}-{key}"
															value={(step.params || {})[key] || ''}
															oninput={(e) => { const params = { ...(step.params || {}), [key]: e.currentTarget.value }; updateActionStep(i, { ...step, params }); }}
															placeholder={schemaObj.description || key} />
													{/each}
												</div>
												<p class="af-hint">Use {'{{trigger.data.field}}'} or {'{{response}}'} for template variables</p>
											{/if}
										{/if}
									{/if}
								{:else if step.type === 'send_prompt'}
									<textarea class="af-input af-textarea"
										placeholder="Enter prompt to send to AI agent…"
										value={step.prompt || ''}
										oninput={(e) => { updateActionStep(i, { ...step, prompt: e.currentTarget.value }); }}></textarea>
									<div class="af-grid-2">
										<div class="af-mini-field">
											<label class="af-mini-label" for="auto-step-profile-{i}">Agent (optional)</label>
											<select class="af-input" id="auto-step-profile-{i}"
												value={step.profile_id || ''}
												onchange={(e) => { updateActionStep(i, { ...step, profile_id: e.currentTarget.value || undefined }); }}>
												<option value="">Default (automation agent)</option>
												{#each profiles as profile}
													<option value={profile.id}>{profile.name}</option>
												{/each}
											</select>
										</div>
										<div class="af-mini-field">
											<label class="af-mini-label" for="auto-step-model-{i}">Model (optional)</label>
											<ModelPicker
												id="auto-step-model-{i}"
												value={step.model || ''}
												allowEmpty
												emptyLabel="Same as automation"
												allowCustom={false}
												onSelect={(v) => { updateActionStep(i, { ...step, model: v || undefined, provider_id: undefined }); }}
											/>
										</div>
										<div class="af-mini-field">
											<label class="af-mini-label" for="auto-step-maxturns-{i}">Max turns</label>
											<input type="number" class="af-input" id="auto-step-maxturns-{i}"
												min="1" max="25"
												value={step.max_turns ?? 1}
												oninput={(e) => { updateActionStep(i, { ...step, max_turns: parseInt(e.currentTarget.value) || 1 }); }} />
										</div>
									</div>
									<p class="af-hint">Use {'{{response}}'} to reference previous AI output</p>
								{:else if step.type === 'webhook_call'}
									<input type="url" class="af-input"
										placeholder="https://example.com/webhook"
										value={step.url || ''}
										oninput={(e) => { updateActionStep(i, { ...step, url: e.currentTarget.value }); }} />
									<select class="af-input"
										value={step.method || 'POST'}
										onchange={(e) => { updateActionStep(i, { ...step, method: e.currentTarget.value }); }}>
										<option value="POST">POST</option>
										<option value="GET">GET</option>
										<option value="PUT">PUT</option>
										<option value="PATCH">PATCH</option>
										<option value="DELETE">DELETE</option>
									</select>
									<textarea class="af-input af-textarea"
										placeholder="Request body (supports template variables)…"
										value={step.body || ''}
										oninput={(e) => { updateActionStep(i, { ...step, body: e.currentTarget.value }); }}></textarea>
									<p class="af-hint">Use {'{{response}}'} or {'{{trigger.data.field}}'} for template variables</p>
								{:else if step.type === 'delay'}
									<div class="af-mini-field">
										<label class="af-mini-label" for="auto-step-delay-{i}">Seconds to wait</label>
										<input type="number" class="af-input" id="auto-step-delay-{i}" min="1" max="300"
											placeholder="5"
											value={step.seconds ?? 5}
											oninput={(e) => { updateActionStep(i, { ...step, seconds: parseInt(e.currentTarget.value) || 0 }); }} />
										<p class="af-hint">Maximum 300 seconds (5 minutes)</p>
									</div>
								{/if}
							</div>
						{/if}
					</div>
				{/each}

				<button type="button" class="af-add-btn" onclick={() => addActionStep()}>
					<Plus size={14} />
					Add action step
				</button>
			</div>
		</div>
	{/if}
</div>

<!-- Configuration -->
<div class="set-group">
	<button type="button" class="set-grouphead head-toggle" class:collapsed={collapsedSections.has('config')} onclick={() => onToggleSection('config')}>
		<h3><Settings />Configuration</h3>
		<ChevronDown size={15} class={collapsedSections.has('config') ? 'gh-chev' : 'gh-chev open'} />
	</button>
	{#if !collapsedSections.has('config')}
		<div class="set-row">
			<div class="lab"><h4>Agent <span class="req">*</span></h4><p>The agent this automation runs under.</p></div>
			<div class="ctl">
				{#if profiles.length === 0}
					<!-- Nothing is seeded, so a fresh install reaches this required field with an
					     empty picker and a save that can only fail. Name the missing piece here,
					     where the user is already looking, instead of at submit time. -->
					<button type="button" class="no-agents" onclick={() => layout.openFeaturePage('profile')}>
						No agents yet — create one first
					</button>
				{:else}
					<select class="sel" value={formProfileId} onchange={(e) => onUpdateFormProfileId(e.currentTarget.value)}>
						<option value="">Select an agent…</option>
						{#each profiles as profile}
							<option value={profile.id}>{profile.name}</option>
						{/each}
					</select>
				{/if}
			</div>
		</div>
		<div class="set-row" data-testid="automation-model-row">
			<div class="lab"><h4>Model</h4><p class:model-error={modelHintIsError}>{modelHint}</p></div>
			<div class="ctl">
				<ModelPicker
					id="automation-model"
					value={formModel}
					allowEmpty
					emptyLabel={modelEmptyLabel}
					allowCustom={false}
					onSelect={onUpdateFormModel}
				/>
			</div>
		</div>
		<div class="set-row">
			<div class="lab"><h4>Project</h4><p>Scope the run to a project, or leave global.</p></div>
			<div class="ctl">
				<select class="sel" value={formProjectId} onchange={(e) => onUpdateFormProjectId(e.currentTarget.value)}>
					<option value="">None (global)</option>
					{#if formProjectId && !projects.some((p) => p.id === formProjectId)}
						<!-- Saved project isn't in this install's list; without this option the
						     browser would silently display "None (global)" while the stale id stays saved. -->
						<option value={formProjectId}>Unknown project ({formProjectId.slice(0, 8)}…)</option>
					{/if}
					{#each projects as project}
						<option value={project.id}>{project.name}</option>
					{/each}
				</select>
			</div>
		</div>
		<div class="set-row">
			<div class="lab"><h4>Output</h4><p>Where the result is delivered.</p></div>
			<div class="ctl">
				<div class="seg">
					<button type="button" class:on={formOutputType === 'session'} onclick={() => { onUpdateFormOutputType('session'); onUpdateFormOutputConfig({}); }}>Session</button>
					<button type="button" class:on={formOutputType === 'connector'} onclick={() => { onUpdateFormOutputType('connector'); onUpdateFormOutputConfig({}); }}>Connector</button>
					<button type="button" class:on={formOutputType === 'webhook'} onclick={() => { onUpdateFormOutputType('webhook'); onUpdateFormOutputConfig({}); }}>Webhook</button>
					<button type="button" class:on={formOutputType === 'none'} onclick={() => { onUpdateFormOutputType('none'); onUpdateFormOutputConfig({}); }}>None</button>
				</div>
			</div>
		</div>

		{#if formOutputType === 'connector'}
			<div class="set-row">
				<div class="lab"><h4>Connector</h4><p>The destination connector.</p></div>
				<div class="ctl">
					<select class="sel" value={formOutputConfig.connector_id || ''} onchange={(e) => onUpdateFormOutputConfig({ ...formOutputConfig, connector_id: e.currentTarget.value })}>
						<option value="">Select a connector…</option>
						{#each connectors.filter(c => c.is_active || c.status === 'connected') as c}
							<option value={c.id}>{c.name} ({c.type})</option>
						{/each}
						{#if connectors.filter(c => !c.is_active && c.status !== 'connected').length > 0}
							<optgroup label="Inactive">
								{#each connectors.filter(c => !c.is_active && c.status !== 'connected') as c}
									<option value={c.id}>{c.name} ({c.type}) - inactive</option>
								{/each}
							</optgroup>
						{/if}
					</select>
				</div>
			</div>
			<div class="set-row stack">
				<div class="lab"><h4>Platform chat ID</h4><p>Chat, channel ID, or email to deliver to.</p></div>
				<div class="ctl"><input type="text" class="inp mono" value={formOutputConfig.platform_chat_id || ''} oninput={(e) => onUpdateFormOutputConfig({ ...formOutputConfig, platform_chat_id: e.currentTarget.value })} placeholder="chat / channel ID / email" /></div>
			</div>
		{:else if formOutputType === 'webhook'}
			<div class="set-row stack">
				<div class="lab"><h4>Webhook URL</h4><p>The endpoint to POST the result to.</p></div>
				<div class="ctl"><input type="text" class="inp mono" value={formOutputConfig.url || ''} oninput={(e) => onUpdateFormOutputConfig({ ...formOutputConfig, url: e.currentTarget.value })} placeholder="https://example.com/webhook" /></div>
			</div>
		{/if}

		<div class="set-row">
			<div class="lab"><h4>Requires approval</h4><p>Pause before running so you can review.</p></div>
			<div class="ctl">
				<button type="button" class="switch" class:on={formRequiresApproval} role="switch" aria-checked={formRequiresApproval} aria-label="Requires approval" onclick={() => onUpdateFormRequiresApproval(!formRequiresApproval)}><i></i></button>
			</div>
		</div>
	{/if}
</div>

<!-- Budget & Limits -->
<div class="set-group">
	<button type="button" class="set-grouphead head-toggle" class:collapsed={collapsedSections.has('budget')} onclick={() => onToggleSection('budget')}>
		<h3><DollarSign />Budget &amp; limits</h3>
		<ChevronDown size={15} class={collapsedSections.has('budget') ? 'gh-chev' : 'gh-chev open'} />
	</button>
	{#if !collapsedSections.has('budget')}
		<div class="set-row">
			<div class="lab"><h4>Max cost / run</h4><p>Dollar cap per run. Blank = no limit.</p></div>
			<div class="ctl"><input type="number" class="inp num" step="0.01" min="0" value={formMaxCostPerRun} oninput={(e) => onUpdateFormMaxCostPerRun(e.currentTarget.value)} placeholder="No limit" /></div>
		</div>
		<div class="set-row">
			<div class="lab"><h4>Max cost / day</h4><p>Dollar cap per day. Blank = no limit.</p></div>
			<div class="ctl"><input type="number" class="inp num" step="0.01" min="0" value={formMaxCostPerDay} oninput={(e) => onUpdateFormMaxCostPerDay(e.currentTarget.value)} placeholder="No limit" /></div>
		</div>
		<div class="set-row">
			<div class="lab"><h4>Max total</h4><p>Lifetime dollar cap. Blank = no limit.</p></div>
			<div class="ctl"><input type="number" class="inp num" step="0.01" min="0" value={formMaxCostTotal} oninput={(e) => onUpdateFormMaxCostTotal(e.currentTarget.value)} placeholder="No limit" /></div>
		</div>
		<div class="set-row">
			<div class="lab"><h4>Max runs / day</h4><p>Cap the number of runs per day.</p></div>
			<div class="ctl"><input type="number" class="inp num" min="1" value={formMaxRunsPerDay} oninput={(e) => onUpdateFormMaxRunsPerDay(e.currentTarget.value)} placeholder="No limit" /></div>
		</div>
		<div class="set-row">
			<div class="lab"><h4>Max turns</h4><p>Agent turns allowed per run. Blank = no limit.</p></div>
			<div class="ctl"><input type="number" class="inp num" min="1" value={formMaxTurns} oninput={(e) => onUpdateFormMaxTurns(e.currentTarget.value)} placeholder="No limit" /></div>
		</div>
		<div class="set-row">
			<div class="lab"><h4>Timeout</h4><p>Maximum run duration in seconds. Blank = no limit.</p></div>
			<div class="ctl"><input type="number" class="inp num" min="10" value={formTimeoutSeconds} oninput={(e) => onUpdateFormTimeoutSeconds(e.currentTarget.value)} placeholder="No limit" /></div>
		</div>
	{/if}
</div>

<style>
	.req {
		color: var(--destructive);
		font-weight: 700;
	}

	/* Collapsible group head (button styled as a grouphead) */
	.head-toggle {
		display: flex;
		align-items: center;
		justify-content: space-between;
		width: 100%;
		background: none;
		border: none;
		border-bottom: 1px solid var(--border);
		cursor: pointer;
		text-align: left;
		font: inherit;
		transition: background 0.12s;
	}
	.head-toggle:hover {
		background: color-mix(in oklch, var(--muted) 45%, transparent);
	}
	.head-toggle.collapsed {
		border-bottom: none;
	}
	.gh-left {
		display: inline-flex;
		align-items: center;
		gap: 9px;
		min-width: 0;
	}
	.head-toggle :global(.gh-chev) {
		color: var(--muted-foreground);
		flex-shrink: 0;
		transition: transform 0.18s;
		transform: rotate(-90deg);
	}
	.head-toggle :global(.gh-chev.open) {
		transform: rotate(0deg);
	}

	/* Stands in for the agent select when there is nothing to pick yet.
	   No margin-top: `.ctl` centres its contents, so the link sits on the
	   same baseline the select would have. */
	.no-agents {
		background: none;
		border: none;
		padding: 0;
		color: var(--primary);
		cursor: pointer;
		font-size: 0.8125rem;
	}

	.no-agents:hover {
		text-decoration: underline;
	}

	.lab .model-error {
		color: var(--destructive);
	}

	/* Narrow numeric inputs in budget rows */
	.ctl :global(.inp.num) {
		min-width: 0;
		width: 150px;
		text-align: right;
	}

	/* ── Action-step builder (nested cards inside the stacked control) ── */
	.ctl :global(.af-input) {
		width: 100%;
		padding: 9px 12px;
		background: var(--background);
		border: 1px solid var(--border);
		border-radius: 9px;
		color: var(--foreground);
		font-size: 13px;
		font-family: inherit;
		transition: border-color 0.15s;
		outline: none;
	}
	.ctl :global(.af-input:focus) {
		border-color: var(--ring, var(--primary));
	}
	.ctl :global(.af-input::placeholder) {
		color: var(--muted-foreground);
		opacity: 0.65;
	}
	.ctl :global(.af-textarea) {
		min-height: 90px;
		resize: vertical;
		line-height: 1.5;
	}
	.af-hint {
		font-size: 11.5px;
		color: var(--muted-foreground);
		margin: 0;
		line-height: 1.45;
	}
	.af-mini-label {
		font-size: 11px;
		font-weight: 600;
		color: var(--muted-foreground);
	}
	.af-mini-field {
		display: flex;
		flex-direction: column;
		gap: 5px;
	}
	.af-param-list {
		display: flex;
		flex-direction: column;
		gap: 5px;
	}
	.af-grid-2 {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: 10px;
	}
	@media (max-width: 520px) {
		.af-grid-2 {
			grid-template-columns: 1fr;
		}
	}

	.af-item-card {
		background: var(--background);
		border: 1px solid var(--border);
		border-radius: 10px;
		overflow: hidden;
		margin-bottom: 8px;
	}
	/* A step's open model picker must not be clipped by its card (the editor
	   shell does the same for `.set-group`). */
	.af-item-card:has(:global(.mpk-panel)) {
		overflow: visible;
	}
	.af-item-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		width: 100%;
		padding: 9px 12px;
		cursor: pointer;
		text-align: left;
		transition: background 0.12s;
	}
	.af-item-header:hover {
		background: var(--muted);
	}
	.af-item-header-left {
		display: flex;
		align-items: center;
		gap: 9px;
		min-width: 0;
		flex: 1;
	}
	.af-item-header-left :global(svg) {
		flex-shrink: 0;
		color: var(--muted-foreground);
	}
	.af-item-header-actions {
		flex-shrink: 0;
	}
	.icon-x {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 26px;
		height: 26px;
		background: none;
		border: none;
		border-radius: 7px;
		color: var(--destructive);
		cursor: pointer;
		transition: background 0.12s;
	}
	.icon-x:hover {
		background: color-mix(in oklch, var(--destructive) 14%, transparent);
	}
	.af-item-type-badge {
		display: inline-flex;
		align-items: center;
		padding: 2px 9px;
		font-size: 11px;
		font-weight: 600;
		background: var(--primary);
		color: var(--primary-foreground);
		border-radius: 6px;
		white-space: nowrap;
	}
	.af-item-step-num {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 21px;
		height: 21px;
		font-family: var(--font-mono, monospace);
		font-size: 11px;
		font-weight: 700;
		background: var(--muted);
		color: var(--foreground);
		border-radius: 50%;
		flex-shrink: 0;
	}
	.af-item-summary {
		font-size: 12px;
		color: var(--muted-foreground);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		min-width: 0;
	}
	.af-item-body {
		padding: 11px 12px 13px;
		border-top: 1px solid var(--border);
		display: flex;
		flex-direction: column;
		gap: 8px;
	}

	.af-add-btn {
		display: flex;
		align-items: center;
		justify-content: center;
		gap: 6px;
		width: 100%;
		padding: 10px;
		background: none;
		border: 1px dashed var(--border);
		border-radius: 10px;
		color: var(--muted-foreground);
		font-size: 12.5px;
		font-weight: 600;
		cursor: pointer;
		transition: border-color 0.12s, color 0.12s, background 0.12s;
	}
	.af-add-btn:hover {
		border-color: var(--muted-foreground);
		color: var(--foreground);
		background: var(--muted);
	}
</style>
