<script lang="ts">
	import { watchResources } from '$lib/services/watch-resources';
	/**
	 * AutomationsCard - Scheduled and event-driven AI task management
	 *
	 * Features:
	 * - List, create, edit, delete automations
	 * - Enable/disable and manually trigger automations
	 * - View run history with stats
	 * - Approve/deny pending automation runs
	 */
	import { onMount, tick } from 'svelte';
	import { ViewShell, ViewMain, DetailPane, EmptyDetail, InlineBanner } from '$lib/components/layout/view';
	import ConfirmDialog from '$lib/components/ui/ConfirmDialog.svelte';
	import type {
		Automation,
		AutomationCreate,
		AutomationUpdate,
		AutomationRun,
		AutomationApproval,
		AutomationTestResult,
		TriggerType,
		OutputType,
		ActionStep,
		TriggerItem,
		AutomationPermissionMode
	} from '$lib/api/automations';
	import {
		getAutomations,
		createAutomation,
		updateAutomation,
		deleteAutomation,
		enableAutomation,
		disableAutomation,
		triggerAutomation,
		getAutomationRuns,
		getAutomationStats,
		getPendingApprovals,
		approveRun,
		denyRun,
		DEFAULT_AUTOMATION_PERMISSION_MODE,
		formPermissionMode as initialPermissionMode,
		permissionModePayload
	} from '$lib/api/automations';
	import { api, getErrorMessage } from '$lib/api/client';
	import type { Profile } from '$lib/api/client';
	import {
		commitJobChoiceDraft,
		hasDraftChanges,
		type JobChoiceCommit,
		type JobChoiceDraft
	} from '$lib/api/generation-job-choices';
	import type { Project } from '$lib/stores/types';
	import { getConnectors, getConnectorTypes } from '$lib/api/connectors';
	import type { Connector, ConnectorTypeInfo } from '$lib/api/connectors';
	import { timezonePreference } from '$lib/stores/timezonePreference.svelte';
	import { slide, fade } from 'svelte/transition';
	import {
		Search,
		Loader2,
		Plus,
		CheckCircle,
		XCircle,
		ChevronLeft,
		Shield,
		Check,
		CalendarClock
	} from 'lucide-svelte';


	// Sub-components
	import Banner from '$lib/components/Banner.svelte';

	import AutomationsStudio from './automations/AutomationsStudio.svelte';
	import AutomationForm from './automations/AutomationForm.svelte';
	import AutomationDetail from './automations/AutomationDetail.svelte';

	interface Props {
		mobile?: boolean;
	}

	let {
		mobile = false
	}: Props = $props();

	// ── Data state ──────────────────────────────────────────────────────
	let automations: Automation[] = $state([]);
	let pendingApprovals: AutomationApproval[] = $state([]);
	let profiles: Profile[] = $state([]);
	/** False until /agents has loaded, so the detail never calls a live agent "removed". */
	let profilesLoaded = $state(false);
	let projects: Project[] = $state([]);
	let connectors: Connector[] = $state([]);
	let connectorTypes: ConnectorTypeInfo[] = $state([]);
	let loading = $state(true);
	let error = $state('');

	// A pending delete awaiting confirmation (replaces native confirm()).
	let pendingDeleteId = $state<string | null>(null);

	// ── UI state ────────────────────────────────────────────────────────
	type View = 'list' | 'create' | 'edit' | 'detail' | 'approvals';
	let currentView: View = $state('list');
	let searchQuery = $state('');
	let selectedAutomation: Automation | null = $state(null);
	let triggeringId: string | null = $state(null);

	// Detail view state
	let runs: AutomationRun[] = $state([]);
	let runsLoading = $state(false);
	let expandedRunIds: Set<string> = $state(new Set());
	let detailStats: { total_runs: number; total_cost_usd: number; successful_runs: number; failed_runs: number } | null = $state(null);

	// Form state
	let formSaving = $state(false);
	let formError = $state('');
	let formName = $state('');
	let formDescription = $state('');
	let formTriggers: TriggerItem[] = $state([{ type: 'cron', config: { expression: '0 9 * * *' } }]);
	let formPrompt = $state('');
	let formProfileId = $state('');
	let formProjectId = $state('');
	let formOutputType: OutputType = $state('session');
	let formOutputConfig: Record<string, any> = $state({});
	let formMaxCostPerRun: string = $state('');
	let formMaxCostPerDay: string = $state('');
	let formMaxCostTotal: string = $state('');
	let formMaxRunsPerDay: string = $state('');
	let formMaxTurns = $state('1');
	let formTimeoutSeconds = $state('300');
	let formRequiresApproval = $state(false);
	// The model picker's value; '' = no choice of its own (the app default).
	let formModel = $state('');
	// The mode each run starts in. A new automation starts on Bypass; a saved
	// one on its own mode, or the mode its runs use today when it predates the choice.
	let formPermissionMode: AutomationPermissionMode = $state(DEFAULT_AUTOMATION_PERMISSION_MODE);
	let formActionSteps: ActionStep[] = $state([]);
	// Pinned generation job choices, staged until Save and written after the
	// automation itself (a new automation is created paused, so it cannot run
	// before its choices are stored).
	let generationDraft: JobChoiceDraft = $state({});
	// Writes that landed during a partial save, handed to the open editor so it
	// shows what the server now holds before the staged entries are dropped.
	let generationCommit: JobChoiceCommit | null = $state(null);

	// Test result
	let testResult: AutomationTestResult | null = $state(null);

	// ── Section collapse state ──────────────────────────────────────────
	let collapsedSections: Set<string> = $state(new Set(['budget']));
	let expandedTriggers: Set<number> = $state(new Set([0]));
	let expandedActions: Set<number> = $state(new Set());

	function toggleSection(id: string) {
		if (collapsedSections.has(id)) {
			collapsedSections = new Set([...collapsedSections].filter(s => s !== id));
		} else {
			collapsedSections = new Set([...collapsedSections, id]);
		}
	}

	function toggleTriggerExpand(idx: number) {
		if (expandedTriggers.has(idx)) {
			expandedTriggers = new Set([...expandedTriggers].filter(i => i !== idx));
		} else {
			expandedTriggers = new Set([...expandedTriggers, idx]);
		}
	}

	function toggleActionExpand(idx: number) {
		if (expandedActions.has(idx)) {
			expandedActions = new Set([...expandedActions].filter(i => i !== idx));
		} else {
			expandedActions = new Set([...expandedActions, idx]);
		}
	}

	// ── Derived ─────────────────────────────────────────────────────────
	const enabledCount = $derived(automations.filter((a) => a.is_enabled).length);
	const runningCount = $derived(automations.filter((a) => a.status === 'running').length);
	const approvalCount = $derived(pendingApprovals.length);

	const connectorsWithTriggers = $derived(
		connectors.filter(c => {
			const typeInfo = connectorTypes.find(t => t.type === c.type);
			return typeInfo && typeInfo.triggers && typeInfo.triggers.length > 0;
		})
	);

	const connectorsWithActions = $derived(
		connectors.filter(c => {
			const typeInfo = connectorTypes.find(t => t.type === c.type);
			return typeInfo && typeInfo.actions && typeInfo.actions.length > 0;
		})
	);

	// ── Load data ───────────────────────────────────────────────────────
	onMount(() => {
		loadData();
	});

	async function loadData(quiet = false) {
		if (!quiet) loading = true;
		error = '';
		try {
			const [automationsData, approvalsData, profilesData, projectsData, connectorsData, connectorTypesData] = await Promise.all([
				getAutomations(),
				getPendingApprovals(),
				api.get<Profile[]>('/agents'),
				api.get<Project[]>('/projects'),
				getConnectors().catch(() => [] as Connector[]),
				getConnectorTypes().catch(() => [] as ConnectorTypeInfo[])
			]);
			automations = automationsData;
			pendingApprovals = approvalsData;
			profiles = profilesData;
			profilesLoaded = true;
			projects = projectsData;
			connectors = connectorsData;
			connectorTypes = connectorTypesData;
		} catch (e: any) {
			error = e.detail || e.message || 'Failed to load automations';
		} finally {
			loading = false;
		}
	}

	// ── CRUD handlers ───────────────────────────────────────────────────
	function resetForm() {
		formName = '';
		formDescription = '';
		formTriggers = [{ type: 'cron', config: { expression: '0 9 * * *' } }];
		formPrompt = '';
		formProfileId = profiles.length > 0 ? profiles[0].id : '';
		formProjectId = '';
		formOutputType = 'session';
		formOutputConfig = {};
		formMaxCostPerRun = '';
		formMaxCostPerDay = '';
		formMaxCostTotal = '';
		formMaxRunsPerDay = '';
		formMaxTurns = '1';
		formTimeoutSeconds = '300';
		formRequiresApproval = false;
		formModel = '';
		formPermissionMode = DEFAULT_AUTOMATION_PERMISSION_MODE;
		formActionSteps = [];
		generationDraft = {};
		generationCommit = null;
		formError = '';
		testResult = null;
	}

	function backToList() {
		currentView = 'list';
		resetForm();
		selectedAutomation = null;
	}

	function openCreate() {
		selectedAutomation = null;
		error = '';
		resetForm();
		currentView = 'create';
	}

	function openEdit(automation: Automation) {
		error = '';
		selectedAutomation = automation;
		formName = automation.name;
		formDescription = automation.description || '';
		if (automation.triggers && automation.triggers.length > 0) {
			formTriggers = automation.triggers.map(t => ({ type: t.type, config: { ...t.config } }));
		} else {
			formTriggers = [{ type: automation.trigger_type, config: { ...automation.trigger_config } }];
		}
		formPrompt = automation.prompt || '';
		formProfileId = automation.profile_id ?? '';
		formProjectId = automation.project_id || '';
		formOutputType = (automation.output_config?.type as OutputType) || 'session';
		formOutputConfig = { ...automation.output_config };
		formActionSteps = automation.action_steps ? automation.action_steps.map(s => ({ ...s })) : [];
		formMaxCostPerRun = automation.max_cost_per_run != null ? String(automation.max_cost_per_run) : '';
		formMaxCostPerDay = automation.max_cost_per_day != null ? String(automation.max_cost_per_day) : '';
		formMaxCostTotal = automation.max_cost_total != null ? String(automation.max_cost_total) : '';
		formMaxRunsPerDay = automation.max_runs_per_day != null ? String(automation.max_runs_per_day) : '';
		formMaxTurns = automation.max_turns != null ? String(automation.max_turns) : '';
		formTimeoutSeconds = automation.timeout_seconds != null ? String(automation.timeout_seconds) : '';
		formRequiresApproval = automation.requires_approval;
		formModel = automation.model || '';
		formPermissionMode = initialPermissionMode(automation, agentConfigFor(automation.profile_id));
		generationDraft = {};
		generationCommit = null;
		formError = '';
		testResult = null;
		currentView = 'edit';
		remoteBaseline = remoteSnapshot();
		remoteChange = '';
	}
	/** "Pick an agent" (the detail's removed-agent warning): open the editor
	 *  with the Agent section expanded and put keyboard focus on its picker. */
	async function pickAgent(automation: Automation) {
		if (collapsedSections.has('config')) toggleSection('config');
		openEdit(automation);
		await tick();
		document.querySelector<HTMLSelectElement>('[data-agent-picker] select')?.focus();
	}
	async function openDetail(automation: Automation, quiet = false) {
		selectedAutomation = automation;
		currentView = 'detail';
		runsLoading = true;
		if (!quiet) { expandedRunIds = new Set(); detailStats = null; }
		try {
			const [runsData, statsData] = await Promise.all([
				getAutomationRuns(automation.id),
				getAutomationStats(automation.id)
			]);
			if (selectedAutomation?.id !== automation.id || currentView !== 'detail') return;
			runs = runsData;
			detailStats = statsData;
		} catch (e: any) {
			runs = [];
		} finally {
			runsLoading = false;
		}
	}

	function buildFormData(): AutomationCreate {
		const outputConfig: Record<string, any> = { type: formOutputType };
		if (formOutputType === 'connector') {
			outputConfig.connector_id = formOutputConfig.connector_id || '';
			outputConfig.platform_chat_id = formOutputConfig.platform_chat_id || '';
		} else if (formOutputType === 'webhook') {
			outputConfig.url = formOutputConfig.url || '';
		}

		const validActionSteps = formActionSteps.filter(s => {
			if (s.type === 'connector_action') return s.connector_id && s.action_id;
			if (s.type === 'send_prompt') return s.prompt?.trim();
			if (s.type === 'webhook_call') return s.url?.trim();
			if (s.type === 'delay') return (s.seconds ?? 0) > 0;
			return false;
		});

		const processedTriggers = formTriggers.map(t => {
			if (t.type === 'cron') {
				return { type: t.type, config: { ...t.config, timezone: timezonePreference.resolveTimezone() } };
			}
			return { ...t };
		});

		// Clearable fields must send explicit null, never undefined: the PATCH
		// endpoint uses exclude_unset, so an undefined key is dropped by JSON
		// and the old value silently survives (e.g. "Global" kept a stale project).
		return {
			name: formName.trim(),
			description: formDescription.trim() || null,
			triggers: processedTriggers,
			trigger_type: processedTriggers[0]?.type,
			trigger_config: processedTriggers[0]?.config || {},
			prompt: formPrompt.trim() || null,
			profile_id: formProfileId,
			project_id: formProjectId || null,
			output_config: outputConfig,
			action_steps: validActionSteps.length > 0 ? validActionSteps : null,
			max_turns: formMaxTurns ? parseInt(formMaxTurns) : null,
			timeout_seconds: formTimeoutSeconds ? parseInt(formTimeoutSeconds) : null,
			max_cost_per_run: formMaxCostPerRun ? parseFloat(formMaxCostPerRun) : null,
			max_cost_per_day: formMaxCostPerDay ? parseFloat(formMaxCostPerDay) : null,
			max_cost_total: formMaxCostTotal ? parseFloat(formMaxCostTotal) : null,
			max_runs_per_day: formMaxRunsPerDay ? parseInt(formMaxRunsPerDay) : null,
			requires_approval: formRequiresApproval,
			// null clears the choice on edit (PATCH skips undefined keys).
			model: formModel || null,
			// undefined (dropped) keeps a pre-choice automation on its legacy
			// fallback until the owner picks a different mode. Switching the
			// agent pins the shown mode: left NULL, the run would follow the NEW
			// agent's legacy mode, not the one the form displayed.
			permission_mode: permissionModePayload(
				currentView === 'edit' ? selectedAutomation : null,
				formPermissionMode,
				agentConfigFor(selectedAutomation?.profile_id),
				currentView === 'edit' && formProfileId !== (selectedAutomation?.profile_id ?? '')
			)
		};
	}

	function agentConfigFor(profileId: string | null | undefined): { permission_mode?: string | null } | null {
		if (!profileId) return null;
		return (profiles.find((p) => p.id === profileId)?.config as { permission_mode?: string | null } | undefined) ?? null;
	}

	async function handleSave() {
		if (!formName.trim()) {
			formError = 'Name is required';
			return;
		}
		if (formTriggers.length === 0) {
			formError = 'At least one trigger is required';
			return;
		}
		const hasActionSteps = formActionSteps.some(s => {
			if (s.type === 'connector_action') return s.connector_id && s.action_id;
			if (s.type === 'send_prompt') return s.prompt?.trim();
			if (s.type === 'webhook_call') return s.url?.trim();
			if (s.type === 'delay') return (s.seconds ?? 0) > 0;
			return false;
		});
		if (!formPrompt.trim() && !hasActionSteps) {
			formError = 'Either a prompt or at least one action step is required';
			return;
		}
		if (!formProfileId) {
			formError = profiles.length === 0 ? 'No agents yet — create one first' : 'Agent is required';
			return;
		}
		formSaving = true;
		formError = '';
		try {
			const data = buildFormData();
			const creating = !(currentView === 'edit' && selectedAutomation);
			const saved = creating
				? await createAutomation(data)
				: await updateAutomation(selectedAutomation!.id, data as AutomationUpdate);
			if (saved?.id && hasDraftChanges(generationDraft)) {
				const { remaining, applied, errors } = await commitJobChoiceDraft(saved.id, generationDraft, (e) =>
					getErrorMessage(e, 'not saved')
				);
				if (errors.length) {
					// Keep every unsaved pick on screen, on the saved automation, so
					// Save retries the writes instead of creating a duplicate. A new
					// automation stays paused; nothing here enables it.
					await loadData(true);
					selectedAutomation = automations.find((a) => a.id === saved.id) ?? saved;
					currentView = 'edit';
					generationCommit = { seq: (generationCommit?.seq ?? 0) + 1, applied };
					generationDraft = remaining;
					remoteBaseline = remoteSnapshot({});
					const state = saved.is_enabled ? 'saved' : 'saved, paused';
					formError =
						`The automation was ${creating ? 'created' : 'saved'} (${state}), but some media ` +
						`generation choices were not saved: ${errors.map((e) => e.replace(/[.\s]+$/, '')).join('; ')}. ` +
						'Save again to retry.';
					return;
				}
				generationDraft = {};
			}
			await loadData();
			currentView = 'list';
		} catch (e: any) {
			formError = e.detail || e.message || 'Failed to save automation';
		} finally {
			formSaving = false;
		}
	}

	function handleDelete(id: string) {
		pendingDeleteId = id;
	}

	async function confirmDelete() {
		const id = pendingDeleteId;
		pendingDeleteId = null;
		if (!id) return;
		try {
			await deleteAutomation(id);
			if (currentView === 'detail') currentView = 'list';
			await loadData();
		} catch (e: any) {
			error = e.detail || 'Failed to delete automation';
		}
	}

	async function handleToggleEnabled(automation: Automation) {
		try {
			if (automation.is_enabled) {
				await disableAutomation(automation.id);
			} else {
				await enableAutomation(automation.id);
			}
			await loadData();
			if (selectedAutomation?.id === automation.id) {
				selectedAutomation = automations.find((item) => item.id === automation.id) ?? selectedAutomation;
			}
		} catch (e: any) {
			error = e.detail || 'Failed to toggle automation';
		}
	}

	async function handleTrigger(id: string) {
		triggeringId = id;
		try {
			await triggerAutomation(id);
			await loadData();
		} catch (e: any) {
			error = e.detail || 'Failed to trigger automation';
		} finally {
			triggeringId = null;
		}
	}

	async function handleApprove(approvalId: string) {
		try {
			await approveRun(approvalId);
			await loadData();
		} catch (e: any) {
			error = e.detail || 'Failed to approve run';
		}
	}

	async function handleDeny(approvalId: string) {
		try {
			await denyRun(approvalId);
			await loadData();
		} catch (e: any) {
			error = e.detail || 'Failed to deny run';
		}
	}

	function toggleRunExpanded(runId: string) {
		const next = new Set(expandedRunIds);
		if (next.has(runId)) {
			next.delete(runId);
		} else {
			next.add(runId);
		}
		expandedRunIds = next;
	}

	// ── Helpers ─────────────────────────────────────────────────────────
	function getStatusColor(status: Automation['status']): string {
		switch (status) {
			case 'running':
				return 'var(--success)';
			case 'error':
				return 'var(--destructive)';
			case 'paused':
				return 'var(--warning)';
			default:
				return 'var(--muted-foreground)';
		}
	}

	function getRunStatusBadgeClass(status: AutomationRun['status']): string {
		switch (status) {
			case 'completed':
				return 'card-badge--success';
			case 'running':
			case 'approved':
				return 'card-badge--info';
			case 'failed':
			case 'budget_exceeded':
				return 'card-badge--warning';
			case 'cancelled':
				return 'card-badge--muted';
			default:
				return 'card-badge--muted';
		}
	}

	function triggerTypeLabel(type: TriggerType): string {
		switch (type) {
			case 'cron':
				return 'Cron';
			case 'interval':
				return 'Interval';
			case 'connector_event':
				return 'Event';
			case 'connector_trigger':
				return 'Connector';
			case 'webhook':
				return 'Webhook';
		}
	}

	function formatTime(dateStr: string | null): string {
		if (!dateStr) return 'Never';
		const d = new Date(dateStr);
		const now = new Date();
		const diffMs = now.getTime() - d.getTime();
		const diffMin = Math.floor(diffMs / 60000);
		if (diffMin < 1) return 'Just now';
		if (diffMin < 60) return `${diffMin}m ago`;
		const diffHr = Math.floor(diffMin / 60);
		if (diffHr < 24) return `${diffHr}h ago`;
		const diffDays = Math.floor(diffHr / 24);
		return `${diffDays}d ago`;
	}

	function formatCost(cost: number): string {
		if (cost === 0) return '$0.00';
		if (cost < 0.01) return '<$0.01';
		return `$${cost.toFixed(2)}`;
	}

	function formatDuration(ms: number): string {
		if (ms < 1000) return `${ms}ms`;
		return `${(ms / 1000).toFixed(1)}s`;
	}
	let remoteChange = $state('');
	let remoteBaseline = '';
	function remoteSnapshot(draft: JobChoiceDraft = generationDraft) { return JSON.stringify([formName, formDescription, formTriggers, formPrompt, formProfileId, formProjectId, formOutputType, formOutputConfig, formMaxCostPerRun, formMaxCostPerDay, formMaxCostTotal, formMaxRunsPerDay, formMaxTurns, formTimeoutSeconds, formRequiresApproval, formModel, formPermissionMode, formActionSteps, draft]); }
	// The project picker: a project deleted elsewhere leaves it at once (#434).
	watchResources(['projects'], async () => {
		try { projects = await api.get<Project[]>('/projects'); } catch { /* the next load retries */ }
	});
	watchResources(['automations'], async () => {
		const selected = selectedAutomation;
		const snapshot = remoteSnapshot();
		await loadData(true);
		if (!selected || selectedAutomation !== selected) return;
		const fresh = automations.find((item) => item.id === selected.id);
		if (currentView === 'detail') {
			if (fresh) await openDetail(fresh, true);
			else { currentView = 'list'; selectedAutomation = null; remoteChange = 'This automation was deleted elsewhere.'; }
			return;
		}
		// Only an open editor follows remote changes; after Save the list stays put.
		if (currentView !== 'edit') return;
		if (formSaving || snapshot !== remoteSnapshot() || remoteSnapshot() !== remoteBaseline) {
			if (!fresh || JSON.stringify(fresh) !== JSON.stringify(selected)) remoteChange = fresh
				? 'This item changed elsewhere. Your unsaved changes are kept here.'
				: 'This item was deleted elsewhere. Copy your unsaved changes before leaving.';
			return;
		}
		if (fresh) openEdit(fresh);
		else { currentView = 'list'; selectedAutomation = null; remoteChange = 'This item was deleted elsewhere.'; }
	});
</script>
<ViewShell label="Automations" detailOpen={currentView !== 'list'} onBack={backToList}>
 {#snippet panel()}
  <AutomationsStudio {automations} {pendingApprovals} {profiles} {loading} error={currentView === 'list' ? error : ''} {triggeringId}
   selectedId={currentView === 'detail' || currentView === 'edit' ? selectedAutomation?.id : null}
   onOpenCreate={openCreate} onOpenDetail={openDetail} onOpenEdit={openEdit}
   onToggleEnabled={handleToggleEnabled} onTrigger={handleTrigger} onDelete={handleDelete}
   onApprove={handleApprove} onDeny={handleDeny} onClearError={() => error = ''}
   {formatCost} {triggerTypeLabel} />
 {/snippet}
 {#snippet main(back)}
   {#if remoteChange}<InlineBanner tone="info">{remoteChange}</InlineBanner>{/if}
   {#if currentView === 'create' || currentView === 'edit'}
    <DetailPane width="reading" icon={CalendarClock} name={currentView === 'edit' ? formName || 'Edit automation' : 'New automation'}
     subtitle={currentView === 'edit' ? 'Update its triggers, instructions and run limits.' : 'Run tasks on a schedule or in response to events.'}
     onBack={back} backLabel="Automations">
     {#snippet primary()}
      <button type="button" class="st-btn primary" onclick={handleSave} disabled={formSaving}>
       {#if formSaving}<Loader2 size={14} class="animate-spin" />Saving…{:else}<Check size={14} />{currentView === 'edit' ? 'Save' : 'Create'}{/if}
      </button>
     {/snippet}
     {#snippet children()}
			<AutomationForm
				currentView={currentView as 'create' | 'edit'}
				selectedAutomationId={selectedAutomation?.id ?? null}
				savedAutomation={currentView === 'edit' ? selectedAutomation : null}
				{formError}
				{formName}
				{formDescription}
				{formTriggers}
				{formPrompt}
				{formProfileId}
				{formProjectId}
				{formOutputType}
				{formOutputConfig}
				{formMaxCostPerRun}
				{formMaxCostPerDay}
				{formMaxCostTotal}
				{formMaxRunsPerDay}
				{formMaxTurns}
				{formTimeoutSeconds}
				{formRequiresApproval}
				{formModel}
				{formPermissionMode}
				{formActionSteps}
				{profiles}
				{projects}
				{connectors}
				{connectorTypes}
				{connectorsWithTriggers}
				{connectorsWithActions}
				{expandedTriggers}
				{expandedActions}
				{collapsedSections}
				timezone={timezonePreference.resolveTimezone()}
				onUpdateFormError={(val) => formError = val}
				onUpdateFormName={(val) => formName = val}
				onUpdateFormDescription={(val) => formDescription = val}
				onUpdateFormTriggers={(val) => formTriggers = val}
				onUpdateFormPrompt={(val) => formPrompt = val}
				onUpdateFormProfileId={(val) => formProfileId = val}
				onUpdateFormProjectId={(val) => formProjectId = val}
				onUpdateFormOutputType={(val) => formOutputType = val}
				onUpdateFormOutputConfig={(val) => formOutputConfig = val}
				onUpdateFormMaxCostPerRun={(val) => formMaxCostPerRun = val}
				onUpdateFormMaxCostPerDay={(val) => formMaxCostPerDay = val}
				onUpdateFormMaxCostTotal={(val) => formMaxCostTotal = val}
				onUpdateFormMaxRunsPerDay={(val) => formMaxRunsPerDay = val}
				onUpdateFormMaxTurns={(val) => formMaxTurns = val}
				onUpdateFormTimeoutSeconds={(val) => formTimeoutSeconds = val}
				onUpdateFormRequiresApproval={(val) => formRequiresApproval = val}
				onUpdateFormModel={(val) => formModel = val}
				onUpdateFormPermissionMode={(val) => formPermissionMode = val}
				onUpdateFormActionSteps={(val) => formActionSteps = val}
				{generationDraft}
				{generationCommit}
				onUpdateGenerationDraft={(val) => generationDraft = val}
				onToggleSection={toggleSection}
				onToggleTriggerExpand={toggleTriggerExpand}
				onToggleActionExpand={toggleActionExpand}
			/>
     {/snippet}
    </DetailPane>
   {:else if currentView === 'detail' && selectedAutomation}
    {#if error}<InlineBanner tone="warn">{error}</InlineBanner>{/if}
    <AutomationDetail automation={selectedAutomation} profiles={profilesLoaded ? profiles : null} {connectors} {connectorTypes}
     {runs} {runsLoading} {expandedRunIds} {detailStats} {triggeringId}
     onBack={back} onEdit={openEdit} onPickAgent={pickAgent} onTrigger={handleTrigger}
     onDelete={handleDelete} onToggleEnabled={handleToggleEnabled} onToggleRunExpanded={toggleRunExpanded}
     {formatTime} {formatCost} {formatDuration} {getStatusColor} {getRunStatusBadgeClass} {triggerTypeLabel} />
   {:else}
    <ViewMain width="reading"><EmptyDetail title={loading ? 'Loading automations…' : 'Choose an automation'}
     message="Run AI tasks on a schedule or in response to events.">
     <button type="button" class="st-btn primary" onclick={openCreate}><Plus size={14} />New automation</button>
    </EmptyDetail></ViewMain>
   {/if}
 {/snippet}
</ViewShell>
<ConfirmDialog open={pendingDeleteId !== null} title="Delete automation?"
 message="This automation and its run history will be permanently deleted. This cannot be undone."
 confirmLabel="Delete" danger onConfirm={confirmDelete} onCancel={() => pendingDeleteId = null} />
