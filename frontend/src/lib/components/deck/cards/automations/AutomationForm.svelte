<script lang="ts">
	import type {
		TriggerItem,
		ActionStep,
		ActionStepType,
		OutputType
	} from '$lib/api/automations';
	import type { Profile } from '$lib/api/client';
	import type { Project } from '$lib/stores/types';
	import type { Connector, ConnectorTypeInfo } from '$lib/api/connectors';
	import { slide, fade } from 'svelte/transition';
	import {
		AlertCircle,
		X,
		Plus,
		ChevronDown,
		ChevronRight,
		Zap,
		CalendarClock,
		Settings,
		FileText,
		DollarSign,
		Shield
	} from 'lucide-svelte';
	import AutomationTriggerConfig from './AutomationTriggerConfig.svelte';

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
		formActionSteps: ActionStep[];
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
		onUpdateFormActionSteps: (val: ActionStep[]) => void;
		onToggleSection: (id: string) => void;
		onToggleTriggerExpand: (idx: number) => void;
		onToggleActionExpand: (idx: number) => void;
		onSyncCronFields?: (expr: string) => void;
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
		formActionSteps,
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
		onUpdateFormActionSteps,
		onToggleSection,
		onToggleTriggerExpand,
		onToggleActionExpand,
		onSyncCronFields
	}: Props = $props();

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

<div class="auto-form" transition:fade={{ duration: 150 }}>
	{#if formError}
		<div class="card-error-banner" transition:slide={{ duration: 150 }}>
			<AlertCircle size={16} />
			<span style="flex:1">{formError}</span>
			<button onclick={() => onUpdateFormError('')}>
				<X size={14} />
			</button>
		</div>
	{/if}

	<div class="af-form-body">

		<!-- SECTION: Basics -->
		<div class="af-section">
			<button class="af-section-header" onclick={() => onToggleSection('basics')}>
				<div class="af-section-title">
					<CalendarClock size={16} />
					<h3>Basics</h3>
				</div>
				{#if collapsedSections.has('basics')}
					<ChevronRight size={14} class="af-chevron" />
				{:else}
					<ChevronDown size={14} class="af-chevron" />
				{/if}
			</button>
			{#if !collapsedSections.has('basics')}
				<div class="af-section-body" transition:slide={{ duration: 150 }}>
					<div class="af-field-grid">
						<div class="af-field">
							<label class="af-label" for="auto-name">Name <span class="af-required">*</span></label>
							<input
								id="auto-name"
								type="text"
								class="af-input"
								value={formName}
								oninput={(e) => onUpdateFormName(e.currentTarget.value)}
								placeholder="e.g. Daily report generator"
							/>
						</div>
						<div class="af-field">
							<label class="af-label" for="auto-description">Description</label>
							<input
								id="auto-description"
								type="text"
								class="af-input"
								value={formDescription}
								oninput={(e) => onUpdateFormDescription(e.currentTarget.value)}
								placeholder="Optional description..."
							/>
						</div>
					</div>
				</div>
			{/if}
		</div>

		<div class="af-divider"></div>

		<!-- SECTION: Triggers -->
		<div class="af-section">
			<button class="af-section-header" onclick={() => onToggleSection('triggers')}>
				<div class="af-section-title">
					<Zap size={16} />
					<h3>Triggers</h3>
					<span class="af-section-count">{formTriggers.length}</span>
				</div>
				{#if collapsedSections.has('triggers')}
					<ChevronRight size={14} class="af-chevron" />
				{:else}
					<ChevronDown size={14} class="af-chevron" />
				{/if}
			</button>
			{#if !collapsedSections.has('triggers')}
				<div class="af-section-body" transition:slide={{ duration: 150 }}>
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
			{/if}
		</div>

		<div class="af-divider"></div>

		<!-- SECTION: Prompt & Actions -->
		<div class="af-section">
			<button class="af-section-header" onclick={() => onToggleSection('prompt')}>
				<div class="af-section-title">
					<FileText size={16} />
					<h3>Prompt & Actions</h3>
					{#if formActionSteps.length > 0}
						<span class="af-section-count">{formActionSteps.length} action{formActionSteps.length !== 1 ? 's' : ''}</span>
					{/if}
				</div>
				{#if collapsedSections.has('prompt')}
					<ChevronRight size={14} class="af-chevron" />
				{:else}
					<ChevronDown size={14} class="af-chevron" />
				{/if}
			</button>
			{#if !collapsedSections.has('prompt')}
				<div class="af-section-body" transition:slide={{ duration: 150 }}>
					<div class="af-field">
						<label class="af-label" for="auto-prompt">
							Prompt {#if formActionSteps.length === 0}<span class="af-required">*</span>{/if}
						</label>
						<textarea
							id="auto-prompt"
							class="af-input af-textarea"
							value={formPrompt}
							oninput={(e) => onUpdateFormPrompt(e.currentTarget.value)}
							placeholder="Enter the prompt template for this automation..."
						></textarea>
						<p class="af-hint">
							{#if formActionSteps.length > 0}
								Optional when action steps are configured. Runs before action steps.
							{:else}
								The main instruction sent to the AI agent when this automation runs
							{/if}
						</p>
					</div>

					<!-- Action steps -->
					{#if formActionSteps.length > 0}
						<div class="af-subsection-label">Action Steps</div>
					{/if}

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
									<button
										class="card-action-btn card-action-btn--destructive"
										onclick={(e) => { e.stopPropagation(); removeActionStep(i); }}
									>
										<X size={12} />
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
										<select class="af-input" style="margin-top: 8px"
											value={step.connector_id || ''}
											onchange={(e) => { const updated = { ...step, connector_id: e.currentTarget.value, action_id: '', params: {} }; updateActionStep(i, updated); }}>
											<option value="">Select connector...</option>
											{#each connectorsWithActions as c}
												<option value={c.id}>{c.name} ({c.type})</option>
											{/each}
										</select>
										{#if step.connector_id}
											<select class="af-input" style="margin-top: 8px"
												value={step.action_id || ''}
												onchange={(e) => { const updated = { ...step, action_id: e.currentTarget.value, params: {} }; updateActionStep(i, updated); }}>
												<option value="">Select action...</option>
												{#each getActionsForConnector(step.connector_id) as action}
													<option value={action.id}>{action.name}</option>
												{/each}
											</select>
										{/if}
										{#if step.action_id}
											{@const actionDef = getActionsForConnector(step.connector_id || '').find(a => a.id === step.action_id)}
											{#if actionDef}
												<p class="af-hint" style="margin-top: 4px">{actionDef.description}</p>
												{#if actionDef.input_schema?.properties}
													<div style="margin-top: 8px;">
														{#each Object.entries(actionDef.input_schema.properties) as [key, schemaDef]}
															{@const schemaObj = schemaDef as Record<string, any>}
															<label class="af-label" for="auto-param-{i}-{key}" style="font-size: 11px;">{key}</label>
															<input type="text" class="af-input" id="auto-param-{i}-{key}" style="font-size: 12px;"
																value={(step.params || {})[key] || ''}
																oninput={(e) => { const params = { ...(step.params || {}), [key]: e.currentTarget.value }; updateActionStep(i, { ...step, params }); }}
																placeholder={schemaObj.description || key} />
														{/each}
													</div>
													<p class="af-hint" style="margin-top: 4px;">Use {'{{trigger.data.field}}'} or {'{{response}}'} for template variables</p>
												{/if}
											{/if}
										{/if}
									{:else if step.type === 'send_prompt'}
										<textarea class="af-input af-textarea" style="margin-top: 8px; min-height: 80px"
											placeholder="Enter prompt to send to AI agent..."
											value={step.prompt || ''}
											oninput={(e) => { updateActionStep(i, { ...step, prompt: e.currentTarget.value }); }}></textarea>
										<div class="af-field-grid" style="margin-top: 8px;">
											<div class="af-field">
												<label class="af-label" for="auto-step-profile-{i}" style="font-size: 11px;">Profile (optional)</label>
												<select class="af-input" id="auto-step-profile-{i}" style="font-size: 12px;"
													value={step.profile_id || ''}
													onchange={(e) => { updateActionStep(i, { ...step, profile_id: e.currentTarget.value || undefined }); }}>
													<option value="">Default (automation profile)</option>
													{#each profiles as profile}
														<option value={profile.id}>{profile.name}</option>
													{/each}
												</select>
											</div>
											<div class="af-field">
												<label class="af-label" for="auto-step-maxturns-{i}" style="font-size: 11px;">Max turns</label>
												<input type="number" class="af-input" id="auto-step-maxturns-{i}" style="font-size: 12px;"
													min="1" max="25"
													value={step.max_turns ?? 1}
													oninput={(e) => { updateActionStep(i, { ...step, max_turns: parseInt(e.currentTarget.value) || 1 }); }} />
											</div>
										</div>
										<p class="af-hint" style="margin-top: 4px;">Use {'{{response}}'} to reference previous AI output</p>
									{:else if step.type === 'webhook_call'}
										<input type="url" class="af-input" style="margin-top: 8px"
											placeholder="https://example.com/webhook"
											value={step.url || ''}
											oninput={(e) => { updateActionStep(i, { ...step, url: e.currentTarget.value }); }} />
										<select class="af-input" style="margin-top: 8px"
											value={step.method || 'POST'}
											onchange={(e) => { updateActionStep(i, { ...step, method: e.currentTarget.value }); }}>
											<option value="POST">POST</option>
											<option value="GET">GET</option>
											<option value="PUT">PUT</option>
											<option value="PATCH">PATCH</option>
											<option value="DELETE">DELETE</option>
										</select>
										<textarea class="af-input af-textarea" style="margin-top: 8px; min-height: 60px"
											placeholder="Request body (supports template variables)..."
											value={step.body || ''}
											oninput={(e) => { updateActionStep(i, { ...step, body: e.currentTarget.value }); }}></textarea>
										<p class="af-hint" style="margin-top: 4px;">Use {'{{response}}'} or {'{{trigger.data.field}}'} for template variables</p>
									{:else if step.type === 'delay'}
										<div style="margin-top: 8px;">
											<label class="af-label" for="auto-step-delay-{i}" style="font-size: 11px;">Seconds to wait</label>
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

					<button class="af-add-btn" onclick={() => addActionStep()}>
						<Plus size={14} />
						Add Action Step
					</button>
				</div>
			{/if}
		</div>

		<div class="af-divider"></div>

		<!-- SECTION: Configuration -->
		<div class="af-section">
			<button class="af-section-header" onclick={() => onToggleSection('config')}>
				<div class="af-section-title">
					<Settings size={16} />
					<h3>Configuration</h3>
				</div>
				{#if collapsedSections.has('config')}
					<ChevronRight size={14} class="af-chevron" />
				{:else}
					<ChevronDown size={14} class="af-chevron" />
				{/if}
			</button>
			{#if !collapsedSections.has('config')}
				<div class="af-section-body" transition:slide={{ duration: 150 }}>
					<div class="af-field-grid">
						<div class="af-field">
							<label class="af-label" for="auto-profile">Profile <span class="af-required">*</span></label>
							<select id="auto-profile" class="af-input"
								value={formProfileId}
								onchange={(e) => onUpdateFormProfileId(e.currentTarget.value)}>
								<option value="">Select a profile...</option>
								{#each profiles as profile}
									<option value={profile.id}>{profile.name}</option>
								{/each}
							</select>
						</div>
						<div class="af-field">
							<label class="af-label" for="auto-project">Project</label>
							<select id="auto-project" class="af-input"
								value={formProjectId}
								onchange={(e) => onUpdateFormProjectId(e.currentTarget.value)}>
								<option value="">None (global)</option>
								{#each projects as project}
									<option value={project.id}>{project.name}</option>
								{/each}
							</select>
						</div>
					</div>

					<div class="af-field" style="margin-top: 4px;">
						<span class="af-label">Output Type</span>
						<div class="auto-trigger-selector">
							<button class="auto-trigger-btn" class:active={formOutputType === 'session'}
								onclick={() => { onUpdateFormOutputType('session'); onUpdateFormOutputConfig({}); }}>Session</button>
							<button class="auto-trigger-btn" class:active={formOutputType === 'connector'}
								onclick={() => { onUpdateFormOutputType('connector'); onUpdateFormOutputConfig({}); }}>Connector</button>
							<button class="auto-trigger-btn" class:active={formOutputType === 'webhook'}
								onclick={() => { onUpdateFormOutputType('webhook'); onUpdateFormOutputConfig({}); }}>Webhook</button>
							<button class="auto-trigger-btn" class:active={formOutputType === 'none'}
								onclick={() => { onUpdateFormOutputType('none'); onUpdateFormOutputConfig({}); }}>None</button>
						</div>
					</div>

					{#if formOutputType === 'connector'}
						<div class="af-field-grid" transition:slide={{ duration: 150 }}>
							<div class="af-field">
								<label class="af-label" for="auto-output-connector">Connector</label>
								<select id="auto-output-connector" class="af-input"
									value={formOutputConfig.connector_id || ''}
									onchange={(e) => onUpdateFormOutputConfig({ ...formOutputConfig, connector_id: e.currentTarget.value })}>
									<option value="">Select a connector...</option>
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
							<div class="af-field">
								<label class="af-label" for="auto-output-chat-id">Platform Chat ID</label>
								<input id="auto-output-chat-id" type="text" class="af-input"
									value={formOutputConfig.platform_chat_id || ''}
									oninput={(e) => onUpdateFormOutputConfig({ ...formOutputConfig, platform_chat_id: e.currentTarget.value })}
									placeholder="chat / channel ID / email" />
							</div>
						</div>
					{:else if formOutputType === 'webhook'}
						<div class="af-field" transition:slide={{ duration: 150 }}>
							<label class="af-label" for="auto-output-webhook-url">Webhook URL</label>
							<input id="auto-output-webhook-url" type="text" class="af-input"
								value={formOutputConfig.url || ''}
								oninput={(e) => onUpdateFormOutputConfig({ ...formOutputConfig, url: e.currentTarget.value })}
								placeholder="https://example.com/webhook" />
						</div>
					{/if}

					<!-- Approval toggle -->
					<label class="af-toggle-row">
						<button
							class="auto-toggle"
							class:active={formRequiresApproval}
							onclick={() => onUpdateFormRequiresApproval(!formRequiresApproval)}
							role="switch"
							aria-checked={formRequiresApproval}
							aria-label="Requires approval"
						>
							<span class="auto-toggle-thumb"></span>
						</button>
						<span class="auto-toggle-label">
							<Shield size={14} />
							Requires approval before running
						</span>
					</label>
				</div>
			{/if}
		</div>

		<div class="af-divider"></div>

		<!-- SECTION: Budget & Limits -->
		<div class="af-section">
			<button class="af-section-header" onclick={() => onToggleSection('budget')}>
				<div class="af-section-title">
					<DollarSign size={16} />
					<h3>Budget & Limits</h3>
				</div>
				{#if collapsedSections.has('budget')}
					<ChevronRight size={14} class="af-chevron" />
				{:else}
					<ChevronDown size={14} class="af-chevron" />
				{/if}
			</button>
			{#if !collapsedSections.has('budget')}
				<div class="af-section-body" transition:slide={{ duration: 150 }}>
					<div class="af-field-grid af-field-grid--3col">
						<div class="af-field">
							<label class="af-label" for="auto-max-cost-run">Max cost/run ($)</label>
							<input id="auto-max-cost-run" type="number" class="af-input" step="0.01" min="0"
								value={formMaxCostPerRun}
								oninput={(e) => onUpdateFormMaxCostPerRun(e.currentTarget.value)}
								placeholder="No limit" />
						</div>
						<div class="af-field">
							<label class="af-label" for="auto-max-cost-day">Max cost/day ($)</label>
							<input id="auto-max-cost-day" type="number" class="af-input" step="0.01" min="0"
								value={formMaxCostPerDay}
								oninput={(e) => onUpdateFormMaxCostPerDay(e.currentTarget.value)}
								placeholder="No limit" />
						</div>
						<div class="af-field">
							<label class="af-label" for="auto-max-cost-total">Max total ($)</label>
							<input id="auto-max-cost-total" type="number" class="af-input" step="0.01" min="0"
								value={formMaxCostTotal}
								oninput={(e) => onUpdateFormMaxCostTotal(e.currentTarget.value)}
								placeholder="No limit" />
						</div>
						<div class="af-field">
							<label class="af-label" for="auto-max-runs-day">Max runs/day</label>
							<input id="auto-max-runs-day" type="number" class="af-input" min="1"
								value={formMaxRunsPerDay}
								oninput={(e) => onUpdateFormMaxRunsPerDay(e.currentTarget.value)}
								placeholder="No limit" />
						</div>
						<div class="af-field">
							<label class="af-label" for="auto-max-turns">Max turns</label>
							<input id="auto-max-turns" type="number" class="af-input" min="1"
								value={formMaxTurns}
								oninput={(e) => onUpdateFormMaxTurns(e.currentTarget.value)} />
						</div>
						<div class="af-field">
							<label class="af-label" for="auto-timeout">Timeout (s)</label>
							<input id="auto-timeout" type="number" class="af-input" min="10"
								value={formTimeoutSeconds}
								oninput={(e) => onUpdateFormTimeoutSeconds(e.currentTarget.value)} />
						</div>
					</div>
				</div>
			{/if}
		</div>

	</div>
</div>

<style>
	.auto-form {
		display: flex;
		flex-direction: column;
		height: 100%;
	}

	.af-form-body {
		flex: 1;
		overflow-y: auto;
		padding: 24px 20px;
		display: flex;
		flex-direction: column;
		gap: 0;
	}

	/* Sections */
	.af-section {
		display: flex;
		flex-direction: column;
	}

	.af-section-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding: 10px 0;
		background: none;
		border: none;
		cursor: pointer;
		color: var(--text-primary, var(--foreground));
		transition: color 150ms;
	}

	.af-section-header:hover {
		color: var(--primary);
	}

	.af-section-title {
		display: flex;
		align-items: center;
		gap: 8px;
	}

	.af-section-title h3 {
		font-size: var(--text-md, 0.875rem);
		font-weight: 600;
		margin: 0;
	}

	.af-section-title :global(svg) {
		color: var(--primary);
		flex-shrink: 0;
	}

	.af-section-count {
		font-size: var(--text-xs, 0.6875rem);
		font-weight: 600;
		color: var(--primary);
		padding: 1px 8px;
		background: color-mix(in oklch, var(--primary) 10%, transparent);
		border-radius: 6px;
	}

	.af-section-header :global(.af-chevron) {
		color: var(--text-secondary, var(--muted-foreground));
		flex-shrink: 0;
	}

	.af-section-body {
		display: flex;
		flex-direction: column;
		gap: 12px;
		padding: 4px 0 12px 0;
	}

	.af-divider {
		height: 1px;
		background: var(--border-subtle, var(--border));
		width: 100%;
	}

	/* Fields */
	.af-field-grid {
		display: grid;
		grid-template-columns: repeat(2, 1fr);
		gap: 16px;
	}

	.af-field-grid--3col {
		grid-template-columns: repeat(3, 1fr);
	}

	@media (max-width: 640px) {
		.af-field-grid {
			grid-template-columns: 1fr;
		}
		.af-field-grid--3col {
			grid-template-columns: repeat(2, 1fr);
		}
	}

	@media (max-width: 400px) {
		.af-field-grid--3col {
			grid-template-columns: 1fr;
		}
	}

	.af-field {
		display: flex;
		flex-direction: column;
		gap: 6px;
	}

	.af-label {
		font-size: var(--text-sm, 0.75rem);
		font-weight: 500;
		color: var(--text-secondary, var(--muted-foreground));
	}

	.af-required {
		color: var(--destructive, #ef4444);
	}

	.af-input {
		width: 100%;
		padding: 10px 14px;
		background: var(--surface-1);
		border: 1px solid var(--border-subtle, var(--border));
		border-radius: var(--radius-md, 10px);
		color: var(--text-primary, var(--foreground));
		font-size: var(--text-md, 0.875rem);
		font-family: inherit;
		transition: border-color 200ms, box-shadow 200ms;
		outline: none;
	}

	.af-input:focus {
		border-color: var(--primary);
		box-shadow: 0 0 0 3px color-mix(in oklch, var(--primary) 15%, transparent);
	}

	.af-input::placeholder {
		color: var(--text-tertiary, var(--muted-foreground));
	}

	.af-textarea {
		min-height: 100px;
		resize: vertical;
	}

	.af-hint {
		font-size: var(--text-xs, 0.6875rem);
		color: var(--text-tertiary, var(--muted-foreground));
		margin: 0;
	}

	.af-subsection-label {
		font-size: var(--text-sm, 0.75rem);
		font-weight: 600;
		color: var(--text-secondary, var(--muted-foreground));
		text-transform: uppercase;
		letter-spacing: 0.06em;
		margin-top: 4px;
	}

	/* Item Cards */
	.af-item-card {
		background: var(--surface-1);
		border: 1px solid var(--border-subtle, var(--border));
		border-radius: var(--radius-md, 10px);
		overflow: hidden;
		transition: border-color 200ms;
	}

	.af-item-card:hover {
		border-color: var(--border-emphasis, var(--border));
	}

	.af-item-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		width: 100%;
		padding: 10px 14px;
		background: none;
		border: none;
		cursor: pointer;
		color: inherit;
		text-align: left;
		transition: background 150ms;
	}

	.af-item-header:hover {
		background: color-mix(in oklch, var(--foreground) 3%, transparent);
	}

	.af-item-header-left {
		display: flex;
		align-items: center;
		gap: 8px;
		min-width: 0;
		flex: 1;
	}

	.af-item-header-left :global(svg) {
		flex-shrink: 0;
		color: var(--text-secondary, var(--muted-foreground));
	}

	.af-item-header-actions {
		display: flex;
		gap: 4px;
		flex-shrink: 0;
	}

	.af-item-type-badge {
		display: inline-flex;
		align-items: center;
		gap: 4px;
		padding: 2px 10px;
		font-size: var(--text-xs, 0.6875rem);
		font-weight: 600;
		background: color-mix(in oklch, var(--primary) 10%, transparent);
		color: var(--primary);
		border-radius: 6px;
		white-space: nowrap;
	}

	.af-item-step-num {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 22px;
		height: 22px;
		font-size: var(--text-xs, 0.6875rem);
		font-weight: 700;
		background: color-mix(in oklch, var(--primary) 12%, transparent);
		color: var(--primary);
		border-radius: 50%;
		flex-shrink: 0;
	}

	.af-item-summary {
		font-size: var(--text-sm, 0.75rem);
		color: var(--text-secondary, var(--muted-foreground));
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		min-width: 0;
	}

	.af-item-body {
		padding: 12px 14px 14px;
		border-top: 1px solid var(--border-subtle, var(--border));
		display: flex;
		flex-direction: column;
		gap: 8px;
	}

	/* Add Button */
	.af-add-btn {
		display: flex;
		align-items: center;
		justify-content: center;
		gap: 6px;
		padding: 10px;
		background: none;
		border: 1px dashed var(--border-subtle, var(--border));
		border-radius: var(--radius-md, 10px);
		color: var(--text-secondary, var(--muted-foreground));
		font-size: var(--text-sm, 0.75rem);
		font-weight: 500;
		cursor: pointer;
		transition: all 200ms;
	}

	.af-add-btn:hover {
		border-color: var(--primary);
		color: var(--primary);
		background: color-mix(in oklch, var(--primary) 5%, transparent);
	}

	/* Toggle Row */
	.af-toggle-row {
		display: flex;
		align-items: center;
		gap: 10px;
		cursor: pointer;
		margin-top: 4px;
	}

	/* Trigger selector */
	.auto-trigger-selector {
		display: flex;
		gap: 6px;
		flex-wrap: wrap;
	}

	.auto-trigger-btn {
		display: flex;
		align-items: center;
		gap: 6px;
		padding: 8px 14px;
		font-size: var(--text-sm, 0.75rem);
		font-weight: 500;
		border-radius: var(--radius-md, calc(var(--radius, 0.75rem) * 0.833));
		background: var(--surface-1);
		border: 1px solid var(--border-subtle, var(--border));
		color: var(--text-secondary, var(--muted-foreground));
		cursor: pointer;
		transition: all 150ms ease;
	}

	.auto-trigger-btn:hover {
		border-color: var(--accent);
		color: var(--text-primary, var(--foreground));
	}

	.auto-trigger-btn:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
	}

	.auto-trigger-btn.active {
		background: var(--selection-bg-emphasis);
		color: var(--selection-text);
		border-color: var(--selection-ring);
		box-shadow: var(--selection-shadow-subtle, var(--selection-shadow));
	}

	/* Approval toggle */
	.auto-toggle {
		position: relative;
		width: 40px;
		height: 22px;
		border-radius: 11px;
		background: var(--muted);
		border: none;
		cursor: pointer;
		transition: background 200ms ease;
		flex-shrink: 0;
		padding: 0;
	}

	.auto-toggle.active {
		background: var(--primary);
	}

	.auto-toggle-thumb {
		position: absolute;
		top: 2px;
		left: 2px;
		width: 18px;
		height: 18px;
		border-radius: 50%;
		background: white;
		transition: transform 200ms ease;
		box-shadow: 0 1px 3px rgba(0,0,0,0.2);
	}

	.auto-toggle.active .auto-toggle-thumb {
		transform: translateX(18px);
	}

	.auto-toggle-label {
		display: flex;
		align-items: center;
		gap: 6px;
		font-size: var(--text-base, 0.8125rem);
		color: var(--text-primary, var(--foreground));
	}
</style>
