<script lang="ts">
	import { automationPermissionLabel, type Automation, type AutomationRun } from '$lib/api/automations';
	import { DetailPane, RowGroup, SettingRow, Switch, InlineBanner } from '$lib/components/layout/view';
 import { CalendarClock } from 'lucide-svelte';
	import {
		ChevronLeft,
		Pencil,
		Play,
		Trash2
	} from 'lucide-svelte';
	import AutomationRunHistory from './AutomationRunHistory.svelte';
	import AutomationModelLabel from './AutomationModelLabel.svelte';
import AutomationModelError from './AutomationModelError.svelte';
	import AutomationJobChoices from './AutomationJobChoices.svelte';
	import type { Profile } from '$lib/api/client';
	import type { Connector, ConnectorTypeInfo } from '$lib/api/connectors';

	/** Mixed-version tombstone only: older peers may send this retired ID.
	 * Never treat it as a live/selectable agent; the API rejects it with 410. */
	const RETIRED_AGENT_ID = 'builtin_assistant';

	interface Props {
 onToggleEnabled?: (automation: Automation) => void;
 triggeringId?: string | null;
		automation: Automation;
		/** Every agent, or null while the list has not loaded (never guess "removed"). */
		profiles?: Pick<Profile, 'id'>[] | null;
		connectors?: Connector[];
		connectorTypes?: ConnectorTypeInfo[];
		runs: AutomationRun[];
		runsLoading: boolean;
		expandedRunIds: Set<string>;
		detailStats: { total_runs: number; total_cost_usd: number; successful_runs: number; failed_runs: number } | null;
		onBack: () => void;
		onEdit: (automation: Automation) => void;
		/** "Pick an agent": open the editor focused on the agent field (defaults to onEdit). */
		onPickAgent?: (automation: Automation) => void;
		onTrigger: (id: string) => void;
		onDelete: (id: string) => void;
		onToggleRunExpanded: (runId: string) => void;
		formatTime: (dateStr: string | null) => string;
		formatCost: (cost: number) => string;
		formatDuration: (ms: number) => string;
		getStatusColor: (status: Automation['status']) => string;
		getRunStatusBadgeClass: (status: AutomationRun['status']) => string;
		triggerTypeLabel: (type: any) => string;
	}

	let {
 onToggleEnabled, triggeringId = null,		automation,
		profiles = null, connectors = [], connectorTypes = [],
		runs,
		runsLoading,
		expandedRunIds,
		detailStats,
		onBack,
		onEdit,
		onPickAgent,
		onTrigger,
		onDelete,
		onToggleRunExpanded,
		formatTime,
		formatCost,
		formatDuration,
		getStatusColor,
		getRunStatusBadgeClass,
		triggerTypeLabel
	}: Props = $props();

	const agentMissing = $derived(
		profiles !== null &&
			automation.profile_id !== RETIRED_AGENT_ID &&
			!profiles.some((p) => p.id === automation.profile_id)
	);

	function successRate(total: number, successful: number): string {
		if (total === 0) return 'N/A';
		return `${Math.round((successful / total) * 100)}%`;
	}

 let tab = $state('overview');
</script>

<DetailPane width="reading" name={automation.name} icon={CalendarClock} onBack={onBack} backLabel="Automations"
 subtitle={automation.description || (automation.triggers || [{type:automation.trigger_type}]).map(t => triggerTypeLabel(t.type)).join(' · ')}
 tab={tab} onTabChange={(value) => tab = value} tabs={[{id:'overview',label:'Overview'},{id:'runs',label:'Run history',count:detailStats?.total_runs ?? runs.length},{id:'generation',label:'Media generation'}]}>
 {#snippet badges()}
  {#if !automation.is_enabled}<span class="text-xs text-muted-foreground" data-testid="automation-disabled-badge" title="This automation does not run until it is enabled">Disabled</span>{/if}
 {/snippet}
 {#snippet primary()}
  <button class="st-btn" title="Edit" onclick={() => onEdit(automation)}><Pencil size={14} />Edit</button>
  <button class="st-btn" title="Trigger" aria-label="Trigger" disabled={triggeringId === automation.id} onclick={() => onTrigger(automation.id)}><Play size={14} />{triggeringId === automation.id ? 'Running…' : 'Run now'}</button>
 {/snippet}
 {#snippet banner()}
  {#if automation.last_error}<p role="status"><InlineBanner tone="info">{automation.last_error}</InlineBanner></p>{/if}
  {#if automation.model_error}<div role="alert"><InlineBanner tone="warn"><AutomationModelError error={automation.model_error} providerId={automation.effective_provider_id} modelId={automation.effective_model} /></InlineBanner></div>{/if}
  {#if agentMissing}
   <div data-testid="automation-agent-missing"><InlineBanner tone="warn">
    {automation.is_enabled ? 'Its agent was removed. Pick another agent so it can run.' : 'Its agent was removed. Pick another agent to enable it.'}
    {#snippet actions()}<button class="st-btn" onclick={() => (onPickAgent ?? onEdit)(automation)}>Pick an agent</button>{/snippet}
   </InlineBanner></div>
  {/if}
 {/snippet}
 {#snippet children(current)}
  {#if current === 'overview'}
   <RowGroup title="Configuration">
    <SettingRow label="Enabled" hint="Allow scheduled and event-driven runs.">
     <Switch controlled label={automation.is_enabled ? 'Disable automation' : 'Enable automation'} checked={automation.is_enabled} disabled={!onToggleEnabled} onchange={() => onToggleEnabled?.(automation)} />
    </SettingRow>
    <SettingRow label="Reasoning model"><AutomationModelLabel {automation} /></SettingRow>
    <SettingRow label="Permissions"><span data-testid="automation-permission-badge" title="Permission mode its runs start in">{automationPermissionLabel(automation)}</span></SettingRow>
    <SettingRow label="Last run"><span>{formatTime(automation.last_run_at)}</span></SettingRow>
    <div class="automation-prompt"><h4>Prompt</h4><p>{automation.prompt || 'No prompt. This automation uses action steps.'}</p></div>
   </RowGroup>
   {#if detailStats}
    <RowGroup title="Run summary">
     <SettingRow label="Total runs"><span>{detailStats.total_runs}</span></SettingRow>
     <SettingRow label="Total cost"><span>{formatCost(detailStats.total_cost_usd)}</span></SettingRow>
     <SettingRow label="Success rate"><span>{successRate(detailStats.total_runs,detailStats.successful_runs)}</span></SettingRow>
     <SettingRow label="Failures"><span>{detailStats.failed_runs}</span></SettingRow>
    </RowGroup>
   {/if}
   {#if automation.action_steps?.length}
    <RowGroup title="Action steps">
     {#each automation.action_steps as step, i}<SettingRow label="Step {i+1}" hint={step.type.replaceAll('_',' ')}><span title={[step.connector_id, step.action_id].filter(Boolean).join(' · ') || undefined}>{step.prompt || step.url || (step.type === 'delay' ? `${step.seconds}s` : step.type === 'connector_action' ? (connectorTypes.find(t => t.type === connectors.find(c => c.id === step.connector_id)?.type)?.actions.find(a => a.id === step.action_id)?.name || 'Saved connector action') : 'Saved action')}{#if step.connector_id} · {connectors.find(c => c.id === step.connector_id)?.name || 'Saved connector'}{/if}</span></SettingRow>{/each}
    </RowGroup>
   {/if}
   <RowGroup title="Danger zone" tone="danger"><SettingRow label="Delete automation" hint="Permanently removes this automation and its history."><button class="st-btn danger" title="Delete" onclick={() => onDelete(automation.id)}><Trash2 size={14} />Delete</button></SettingRow></RowGroup>
  {:else if current === 'generation'}
   <section aria-label="Media generation" data-testid="automation-generation"><RowGroup title="Media generation" bare><AutomationJobChoices jobId={automation.id} mode="live" /></RowGroup></section>
  {:else}
   <AutomationRunHistory {runs} {runsLoading} {expandedRunIds} {onToggleRunExpanded} {formatTime} {formatCost} {formatDuration} {getRunStatusBadgeClass} />
  {/if}
 {/snippet}
</DetailPane>
<style>
 .automation-prompt { padding: 14px 16px; border-top: 1px solid var(--st-border); }
 .automation-prompt h4 { margin: 0; font-size: 13.5px; font-weight: 500; }
 .automation-prompt p { margin: 8px 0 0; font-size: 14px; line-height: 1.6; white-space: pre-wrap; overflow-wrap: anywhere; }
</style>
