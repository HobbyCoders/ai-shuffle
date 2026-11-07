<script lang="ts">
	import { RowGroup, SettingRow, SelectField, Switch, Segmented, DetailTabs, InlineBanner } from '$lib/components/layout/view';
 import TextField from '$lib/components/settings/rows/TextField.svelte';
 import TextAreaField from '$lib/components/layout/view/TextAreaField.svelte';
	import {
		automationFallbackModel,
		fallbackModelHint,
		savedFallbackModel,
		type Automation,
		type TriggerItem,
		type ActionStep,
		type ActionStepType,
		type OutputType,
		type AutomationPermissionMode
	} from '$lib/api/automations';
	import { accessModes, effectiveAccessMode } from '$lib/components/chat/session-scope';
	import ModelPicker from '$lib/components/settings/ModelPicker.svelte';
import AutomationModelError from './AutomationModelError.svelte';
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
		DollarSign,
		Sparkles,
		TriangleAlert
	} from 'lucide-svelte';
	import AutomationTriggerConfig from './AutomationTriggerConfig.svelte';
	import AutomationJobChoices from './AutomationJobChoices.svelte';
	import {
		hasDraftChanges,
		type JobChoiceCommit,
		type JobChoiceDraft
	} from '$lib/api/generation-job-choices';
	import HelpTip from '$lib/components/settings/rows/HelpTip.svelte';
	import { layout } from '$lib/stores/layout.svelte';
	import { onMount } from 'svelte';
	import { getClaudeAuth } from '$lib/api/claude-auth';
	import { decodeProviderModel, supportsAutoPermission } from '$lib/constants/models';
	import { buildModelPickerCatalog } from '$lib/constants/model-picker';
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
		/** Permission mode each run starts in (a composer mode). */
		formPermissionMode: AutomationPermissionMode;
		formActionSteps: ActionStep[];
		/** The saved automation when editing, for its fallback / unavailable model. */
		savedAutomation?: Automation | null;
		/** Pinned generation job choices staged until Save (written after the automation). */
		generationDraft?: JobChoiceDraft;
		/** Writes already committed for this job during a partial save. */
		generationCommit?: JobChoiceCommit | null;
		onUpdateGenerationDraft?: (draft: JobChoiceDraft) => void;
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
		onUpdateFormPermissionMode: (val: AutomationPermissionMode) => void;
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
		formPermissionMode,
		formActionSteps,
		savedAutomation = null,
		generationDraft = {},
		generationCommit = null,
		onUpdateGenerationDraft,
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
		onUpdateFormPermissionMode,
		onUpdateFormActionSteps,
		onToggleSection,
		onToggleTriggerExpand,
		onToggleActionExpand
	}: Props = $props();

	// A run on a Claude model in subscription mode uses the user's Claude plan
	// while they are away. Recommend an API key before it is saved (the server
	// adds the same notice to the save response). Only when Claude is signed in
	// and this automation would run on a Claude model: subscription is the
	// worker's default mode, so the mode alone does not mean Claude is in use.
	let claudeSubscription = $state(false);
	onMount(() => {
		getClaudeAuth()
			.then((summary) => (claudeSubscription = summary.mode === 'subscription' && !!summary.cli?.signed_in))
			.catch(() => {
				// No notice when the mode cannot be read.
			});
	});
	const scheduledRunNotice = $derived.by(() => {
		if (!claudeSubscription) return '';
		const profile = profiles.find((p) => p.id === formProfileId);
		if (profile?.config?.provider) return '';
		// No model of its own: unattended runs use Claude's default while Claude
		// is connected, so only an explicit other-provider choice clears this.
		const model = formModel || profile?.config?.model || '';
		if (model && decodeProviderModel(model)) return '';
		return 'Claude runs on your subscription, so this automation uses your Claude plan while you are away. Use an API key for scheduled runs (Settings > Providers > Claude). The loop guard holds unattended turns past its budget until you confirm them.';
	});

	// What runs use with no model of their own: the worker's default for
	// unattended runs (not the chat default), unless the selected agent still
	// carries a legacy saved model. An edited automation on
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
	// Permission modes: the composer's list. Auto is offered only for a run
	// model that supports it, as in the composer; a stored Auto on another
	// model reads (and runs) as Ask.
	const runModelForAccess = $derived(
		formModel || profiles.find((p) => p.id === formProfileId)?.config?.model || savedAutomation?.effective_model || ''
	);
	const accessContext = $derived({ hasProject: !!formProjectId, autoSupported: supportsAutoPermission(runModelForAccess || null) });
	const permissionModes = $derived(accessModes(accessContext));
	const permissionValue = $derived(effectiveAccessMode(formPermissionMode, accessContext));
	const permissionHint = $derived.by(() => {
		const base = permissionModes.find((m) => m.value === permissionValue)?.description ?? '';
		const legacy = !!savedAutomation && !savedAutomation.permission_mode
			&& savedAutomation.permission_mode_source === 'agent'
			&& formPermissionMode === savedAutomation.effective_permission_mode;
		return legacy ? `${base}. Saved on this agent (legacy); pick a mode to set it here.` : `${base}.`;
	});

	const modelEmptyLabel = $derived(fallbackModel.legacy ? 'Saved on agent (legacy)' : 'Worker default');

	// What the Model row says under the picker; an unchanged choice that is
	// gone shows why runs fail.
	const modelHint = $derived.by(() => {
		const unchanged = savedAutomation && (savedAutomation.model || '') === formModel;
		if (unchanged && savedAutomation?.model_error) return savedAutomation.model_error;
		if (formModel) return 'Every run uses this model.';
		const label = fallbackModel.label;
		const option = buildModelPickerCatalog().options.find(option => option.label === label || option.value === label || option.description === label);
		return fallbackModelHint({ ...fallbackModel, label: option?.label ?? (fallbackModel.legacy ? 'the saved model' : null) });
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
			case 'connector_action': return getActionsForConnector(step.connector_id || '').find(a => a.id === step.action_id)?.name || (step.action_id ? 'Saved connector action' : 'Not configured');
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

 let tab = $state('setup');
 const tabs = [{id:'setup',label:'Setup'},{id:'generation',label:'Media generation'},{id:'budget',label:'Budget & limits'}];
</script>
<style>
 .notice-actions { display: inline-flex; align-items: center; gap: 8px; vertical-align: middle; }
 .notice-link { padding: 0; border: 0; border-radius: var(--st-radius-sm); background: none; color: var(--st-fg); font: inherit; text-decoration: underline; text-underline-offset: 3px; }
 .notice-link:hover { color: var(--st-primary); }
 .notice-link:active { opacity: 0.7; }
 .notice-link:focus-visible { outline: 2px solid var(--ring); outline-offset: 3px; }
 /* Basics: Description is a short two-line field, about 72px tall. */
 .basics-description :global(textarea) { min-height: 72px; }
 @media (max-width: 600px) { .notice-link { min-height: 44px; } }
 /* Steps are appended below the Prompt card, so the button stays after them,
    12px clear of the card above and on its left edge. */
 .add-step { align-self: flex-start; margin-top: 12px; }
 .automation-triggers { margin-top: 40px; }
 .automation-triggers header { margin-bottom: 16px; }
 .automation-triggers h3 { margin: 0; font-size: 14px; font-weight: 600; }
 .automation-triggers h3 span { margin-left: 8px; color: var(--st-subtle); font-weight: 400; }
 .automation-triggers header p { margin: 8px 0 0; color: var(--st-mfg); font-size: 13px; line-height: 1.5; }
</style>

{#if formError}
 <div role="alert"><InlineBanner tone="warn">{formError}
  {#snippet actions()}<button class="st-btn" onclick={() => onUpdateFormError('')}>Dismiss</button>{/snippet}
 </InlineBanner></div>
{/if}
{#if scheduledRunNotice}
 <div data-testid="automation-api-key-notice"><InlineBanner tone="warn" compact>Claude runs on your subscription.
  {#snippet actions()}<span class="notice-actions"><button class="notice-link" aria-label="Open Claude settings" onclick={() => openSettings({category:'providers',item:'provider:claude',anchor:'providers.claudeMode'})}>Use an API key</button><HelpTip about="scheduled Claude runs" text={scheduledRunNotice} /></span>{/snippet}
 </InlineBanner></div>
{/if}
<DetailTabs {tabs} current={tab} onselect={(id) => tab = id} label="Automation configuration" idPrefix="automation-form" />
<div role="tabpanel" id="automation-form-panel" aria-labelledby="automation-form-tab-{tab}">
{#if tab === 'setup'}
 <RowGroup title="Basics">
  <!-- Label and hint first, then the field at the card's full inner width (as Prompt). -->
  <SettingRow label="Name" hint="A label to recognize this automation.">
   {#snippet expanded()}<TextField value={formName} oninput={onUpdateFormName} label="Name" width="full" placeholder="e.g. Daily report generator" invalid={!!formError && !formName.trim()} />{/snippet}
  </SettingRow>
  <SettingRow label="Description" hint="What this automation does. Optional.">
   {#snippet expanded()}<div class="basics-description"><TextAreaField value={formDescription} oninput={onUpdateFormDescription} label="Description" rows={2} placeholder="Optional description…" /></div>{/snippet}
  </SettingRow>
 </RowGroup>
 <RowGroup title="Prompt & actions" bare>
  <div class="st-card">
   <SettingRow label="Prompt" hint={formActionSteps.length ? 'Optional when action steps are configured. Runs before action steps.' : 'The main instruction sent to the AI agent when this automation runs.'}>
    {#snippet expanded()}<TextAreaField value={formPrompt} oninput={onUpdateFormPrompt} label="Prompt" placeholder="Enter the prompt template for this automation…" />{/snippet}
   </SettingRow>
  </div>
  {#each formActionSteps as step, i}
   <RowGroup title="Step {i + 1}" bare>
    {#snippet actions()}
     <button class="st-btn" aria-expanded={expandedActions.has(i)} onclick={() => onToggleActionExpand(i)}>{expandedActions.has(i) ? 'Collapse' : 'Expand'} step</button>
     <button class="st-btn icon danger" aria-label="Remove step" onclick={() => removeActionStep(i)}><Trash2 size={14} /></button>
    {/snippet}
    <p class="st-note">{actionStepTypeLabel(step.type)} · {getActionSummary(step)}</p>
    {#if expandedActions.has(i)}
     <div class="st-card">
      <SettingRow label="Action type"><SelectField value={step.type} options={[{value:'connector_action',label:'Connector action'},{value:'send_prompt',label:'AI prompt'},{value:'webhook_call',label:'Webhook call'},{value:'delay',label:'Delay'}]} onchange={(value) => changeActionStepType(i, value as ActionStepType)} /></SettingRow>
      {#if step.type === 'connector_action'}
       <SettingRow label="Connector"><SelectField value={step.connector_id || ''} options={[{value:'',label:'Select a connector…'}, ...connectorsWithActions.map(c => ({value:c.id,label:`${c.name} (${c.type})`}))]} onchange={(value) => updateActionStep(i, {...step,connector_id:value,action_id:'',params:{}})} /></SettingRow>
       {#if step.connector_id}
        <SettingRow label="Action"><SelectField value={step.action_id || ''} options={[{value:'',label:'Select an action…'}, ...getActionsForConnector(step.connector_id).map(a => ({value:a.id,label:a.name}))]} onchange={(value) => updateActionStep(i, {...step,action_id:value,params:{}})} /></SettingRow>
        {@const actionDef = getActionsForConnector(step.connector_id).find(a => a.id === step.action_id)}
        {#if actionDef}
         <p class="st-note">{actionDef.description}</p>
         {#each Object.entries(actionDef.input_schema?.properties || {}) as [key, schemaDef]}
          <SettingRow label={key} hint={(schemaDef as Record<string, any>).description || key}><TextField value={String((step.params || {})[key] || '')} oninput={(value) => updateActionStep(i, {...step,params:{...step.params,[key]:value}})} /></SettingRow>
         {/each}
         <p class="st-note">Use {'{{trigger.data.field}}'} or {'{{response}}'} for template variables</p>
        {/if}
       {/if}
      {:else if step.type === 'send_prompt'}
       <SettingRow label="Step prompt">{#snippet expanded()}<TextAreaField value={step.prompt || ''} label="Step prompt" placeholder="Enter prompt to send to AI agent…" oninput={(value) => updateActionStep(i,{...step,prompt:value})} />{/snippet}</SettingRow>
       <SettingRow label="Agent (optional)"><SelectField value={step.profile_id || ''} options={[{value:'',label:'Default (automation agent)'},...profiles.map(p => ({value:p.id,label:p.name}))]} onchange={(value) => updateActionStep(i,{...step,profile_id:value || undefined})} /></SettingRow>
       <SettingRow label="Model (optional)"><label class="sr-only" for="auto-step-model-{i}">Model (optional)</label><ModelPicker id="auto-step-model-{i}" value={step.model || ''} allowEmpty emptyLabel="Same as automation" fallbackLabel="Saved model (unavailable)" allowCustom={false} onSelect={(value) => updateActionStep(i,{...step,model:value || undefined,provider_id:undefined})} /></SettingRow>
       <SettingRow label="Max turns"><TextField value={String(step.max_turns ?? 1)} inputmode="numeric" oninput={(value) => updateActionStep(i,{...step,max_turns:parseInt(value) || 1})} /></SettingRow>
       <p class="st-note">Use {'{{response}}'} to reference previous AI output</p>
      {:else if step.type === 'webhook_call'}
       <SettingRow label="Webhook URL"><TextField type="url" value={step.url || ''} placeholder="https://example.com/webhook" oninput={(value) => updateActionStep(i,{...step,url:value})} /></SettingRow>
       <SettingRow label="Method"><SelectField value={step.method || 'POST'} options={['POST','GET','PUT','PATCH','DELETE']} onchange={(value) => updateActionStep(i,{...step,method:value})} /></SettingRow>
       <SettingRow label="Request body" hint={'Supports {{response}} and {{trigger.data.field}} template variables.'}>{#snippet expanded()}<TextAreaField label="Request body" value={step.body || ''} oninput={(value) => updateActionStep(i,{...step,body:value})} placeholder="Request body (supports template variables)…" />{/snippet}</SettingRow>
      {:else if step.type === 'delay'}
       <SettingRow label="Seconds to wait" hint="Maximum 300 seconds (5 minutes)."><TextField value={String(step.seconds ?? 5)} inputmode="numeric" oninput={(value) => updateActionStep(i,{...step,seconds:parseInt(value) || 0})} /></SettingRow>
      {/if}
     </div>
    {/if}
   </RowGroup>
  {/each}
  <button class="st-btn add-step" onclick={() => addActionStep()}><Plus size={14} />Add action step</button>
 </RowGroup>
 <!-- The task first: Basics, then Prompt & actions, then when it runs. -->
 <section class="automation-triggers" aria-label="Triggers">
  <header><h3>Triggers <span>{formTriggers.length}</span></h3><p>Any trigger firing will run this automation. At least one is required.</p></header>
  <AutomationTriggerConfig triggers={formTriggers} {expandedTriggers} {connectors} {connectorTypes} {connectorsWithTriggers} {currentView} {selectedAutomationId} {timezone} onUpdateTriggers={onUpdateFormTriggers} {onToggleTriggerExpand} />
 </section>
 <RowGroup title="Configuration">
  <SettingRow label="Agent" hint="The agent this automation runs under.">
   {#if !profiles.length}<button class="st-btn" onclick={() => layout.openFeaturePage('profile')}>No agents yet — create one first</button>
   {:else}<span data-agent-picker><SelectField label="Agent" value={formProfileId} options={[{value:'',label:'Select an agent…'},...profiles.map(p => ({value:p.id,label:p.name}))]} onchange={onUpdateFormProfileId} /></span>{/if}
  </SettingRow>
  <div data-testid="automation-model-row"><SettingRow label="Reasoning model" hint={modelHintIsError ? undefined : modelHint}>
   {#if modelHintIsError}<p class="text-sm text-destructive model-error" role="alert"><AutomationModelError error={modelHint} providerId={savedAutomation?.effective_provider_id} modelId={savedAutomation?.effective_model} /></p>{/if}
   <label class="sr-only" for="automation-model">Reasoning model</label><ModelPicker id="automation-model" value={formModel} allowEmpty emptyLabel={modelEmptyLabel} fallbackLabel="Saved model (unavailable)" allowCustom={false} onSelect={onUpdateFormModel} />
  </SettingRow></div>
  <div data-testid="automation-permission-row"><SettingRow label="Permissions" hint={permissionHint}>
   {#snippet expanded()}
    <Segmented label="Permissions" options={permissionModes} value={permissionValue} onchange={onUpdateFormPermissionMode} />
    {#if permissionValue === 'bypassPermissions'}<p class="st-note" data-testid="automation-bypass-hint">Runs every tool, including shell commands, without asking — nobody is watching an automation.</p>{/if}
   {/snippet}
  </SettingRow></div>
  <SettingRow label="Project" hint="Scope the run to a project, or leave global."><SelectField value={formProjectId} options={[{value:'',label:'None (global)'},...(formProjectId && !projects.some(p => p.id === formProjectId) ? [{value:formProjectId,label:'Saved project (unavailable)'}] : []), ...projects.map(p => ({value:p.id,label:p.name}))]} onchange={onUpdateFormProjectId} /></SettingRow>
  <SettingRow label="Output" hint="Where the result is delivered.">
   {#snippet expanded()}<Segmented label="Output" value={formOutputType} options={[{value:'session',label:'Chat'},{value:'connector',label:'Connector'},{value:'webhook',label:'Webhook'},{value:'none',label:'None'}]} onchange={(value) => {onUpdateFormOutputType(value);onUpdateFormOutputConfig({});}} />{/snippet}
  </SettingRow>
  {#if formOutputType === 'connector'}
   <SettingRow label="Connector" hint="The destination connector."><SelectField value={formOutputConfig.connector_id || ''} options={[{value:'',label:'Select a connector…'},...connectors.map(c => ({value:c.id,label:`${c.name} (${c.type})${c.is_active || c.status === 'connected' ? '' : ' - inactive'}`}))]} onchange={(value) => onUpdateFormOutputConfig({...formOutputConfig,connector_id:value})} /></SettingRow>
   <SettingRow label="Platform chat ID" hint="Chat, channel ID, or email to deliver to."><TextField value={formOutputConfig.platform_chat_id || ''} oninput={(value) => onUpdateFormOutputConfig({...formOutputConfig,platform_chat_id:value})} placeholder="chat / channel ID / email" /></SettingRow>
  {:else if formOutputType === 'webhook'}
   <SettingRow label="Webhook URL" hint="The endpoint to POST the result to."><TextField type="url" value={formOutputConfig.url || ''} oninput={(value) => onUpdateFormOutputConfig({...formOutputConfig,url:value})} placeholder="https://example.com/webhook" /></SettingRow>
  {/if}
  <SettingRow label="Requires approval" hint="Pause before running so you can review."><Switch controlled size="sm" checked={formRequiresApproval} label="Requires approval" onchange={() => onUpdateFormRequiresApproval(!formRequiresApproval)} /></SettingRow>
 </RowGroup>
{:else if tab === 'generation'}
 <div data-testid="automation-generation-group"><RowGroup title="Media generation" bare>
  <AutomationJobChoices jobId={selectedAutomationId} mode="draft" draft={generationDraft} committed={generationCommit} onDraftChange={(draft) => onUpdateGenerationDraft?.(draft)} />
 </RowGroup></div>
{:else}
 <RowGroup title="Budget & limits">
<SettingRow label="Max cost / run" hint="Dollar cap per run. Blank = no limit."><TextField value={formMaxCostPerRun} oninput={onUpdateFormMaxCostPerRun} inputmode="decimal" placeholder="No limit" /></SettingRow>
<SettingRow label="Max cost / day" hint="Dollar cap per day. Blank = no limit."><TextField value={formMaxCostPerDay} oninput={onUpdateFormMaxCostPerDay} inputmode="decimal" placeholder="No limit" /></SettingRow>
<SettingRow label="Max total" hint="Lifetime dollar cap. Blank = no limit."><TextField value={formMaxCostTotal} oninput={onUpdateFormMaxCostTotal} inputmode="decimal" placeholder="No limit" /></SettingRow>
<SettingRow label="Max runs / day" hint="Cap the number of runs per day."><TextField value={formMaxRunsPerDay} oninput={onUpdateFormMaxRunsPerDay} inputmode="numeric" placeholder="No limit" /></SettingRow>
<SettingRow label="Max turns" hint="Agent turns allowed per run. Blank = no limit."><TextField value={formMaxTurns} oninput={onUpdateFormMaxTurns} inputmode="numeric" placeholder="No limit" /></SettingRow>
<SettingRow label="Timeout" hint="Maximum run duration in seconds. Blank = no limit."><TextField value={formTimeoutSeconds} oninput={onUpdateFormTimeoutSeconds} inputmode="numeric" placeholder="No limit" /></SettingRow>
</RowGroup>
{/if}
</div>
