<script lang="ts">
	/**
	 * AutomationsCard - Scheduled and event-driven AI task management
	 *
	 * Features:
	 * - List, create, edit, delete automations
	 * - Enable/disable and manually trigger automations
	 * - View run history with stats
	 * - Approve/deny pending automation runs
	 */
	import { onMount } from 'svelte';
	import StudioEditorShell from '$lib/components/layout/StudioEditorShell.svelte';
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
		TriggerItem
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
		denyRun
	} from '$lib/api/automations';
	import { api } from '$lib/api/client';
	import type { Profile } from '$lib/api/client';
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
		ArrowLeft,
		Shield,
		Check
	} from 'lucide-svelte';
	import './card-design-system.css';

	// Sub-components
	import Banner from '$lib/components/Banner.svelte';
	import AutomationsList from './automations/AutomationsList.svelte';
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
	let formActionSteps: ActionStep[] = $state([]);

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

	async function loadData() {
		loading = true;
		error = '';
		try {
			const [automationsData, approvalsData, profilesData, projectsData, connectorsData, connectorTypesData] = await Promise.all([
				getAutomations(),
				getPendingApprovals(),
				api.get<Profile[]>('/profiles'),
				api.get<Project[]>('/projects'),
				getConnectors().catch(() => [] as Connector[]),
				getConnectorTypes().catch(() => [] as ConnectorTypeInfo[])
			]);
			automations = automationsData;
			pendingApprovals = approvalsData;
			profiles = profilesData;
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
		formActionSteps = [];
		formError = '';
		testResult = null;
	}

	function backToList() {
		currentView = 'list';
		resetForm();
		selectedAutomation = null;
	}

	function openCreate() {
		resetForm();
		currentView = 'create';
	}

	function openEdit(automation: Automation) {
		selectedAutomation = automation;
		formName = automation.name;
		formDescription = automation.description || '';
		if (automation.triggers && automation.triggers.length > 0) {
			formTriggers = automation.triggers.map(t => ({ type: t.type, config: { ...t.config } }));
		} else {
			formTriggers = [{ type: automation.trigger_type, config: { ...automation.trigger_config } }];
		}
		formPrompt = automation.prompt || '';
		formProfileId = automation.profile_id;
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
		formError = '';
		testResult = null;
		currentView = 'edit';
	}

	async function openDetail(automation: Automation) {
		selectedAutomation = automation;
		currentView = 'detail';
		runsLoading = true;
		expandedRunIds = new Set();
		detailStats = null;
		try {
			const [runsData, statsData] = await Promise.all([
				getAutomationRuns(automation.id),
				getAutomationStats(automation.id)
			]);
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
			requires_approval: formRequiresApproval
		};
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
			formError = 'Profile is required';
			return;
		}
		formSaving = true;
		formError = '';
		try {
			const data = buildFormData();
			if (currentView === 'edit' && selectedAutomation) {
				await updateAutomation(selectedAutomation.id, data as AutomationUpdate);
			} else {
				await createAutomation(data);
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
</script>

{#snippet automationsMainContent()}
	<!-- Test result -->
	{#if testResult}
		<div class="auto-banner" transition:slide={{ duration: 200 }}>
			<Banner
				severity={testResult.budget_ok ? 'success' : 'warning'}
				message={testResult.message}
				dismissible
				onDismiss={() => (testResult = null)}
			/>
		</div>
	{/if}

	<!-- Error display -->
	{#if error}
		<div class="auto-error-banner" transition:slide={{ duration: 200 }}>
			<Banner severity="error" message={error} dismissible onDismiss={() => (error = '')} />
		</div>
	{/if}

	<!-- View router -->
	<div class="auto-content">
		{#if loading}
			<div class="auto-loading">
				<Loader2 size={32} class="animate-spin" />
				<p>Loading automations...</p>
			</div>
		{:else if currentView === 'list'}
			<AutomationsList
				{automations}
				{searchQuery}
				{triggeringId}
				onOpenDetail={openDetail}
				onOpenCreate={openCreate}
				onOpenEdit={openEdit}
				onToggleEnabled={handleToggleEnabled}
				onTrigger={handleTrigger}
				onDelete={handleDelete}
				{formatTime}
				{formatCost}
				{triggerTypeLabel}
				{getStatusColor}
			/>
		{:else if currentView === 'create' || currentView === 'edit'}
			<AutomationForm
				currentView={currentView as 'create' | 'edit'}
				selectedAutomationId={selectedAutomation?.id ?? null}
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
				onUpdateFormActionSteps={(val) => formActionSteps = val}
				onToggleSection={toggleSection}
				onToggleTriggerExpand={toggleTriggerExpand}
				onToggleActionExpand={toggleActionExpand}
			/>
		{:else if currentView === 'detail' && selectedAutomation}
			<AutomationDetail
				automation={selectedAutomation}
				{runs}
				{runsLoading}
				{expandedRunIds}
				{detailStats}
				onBack={() => currentView = 'list'}
				onEdit={openEdit}
				onTrigger={handleTrigger}
				onDelete={handleDelete}
				onToggleRunExpanded={toggleRunExpanded}
				{formatTime}
				{formatCost}
				{formatDuration}
				{getStatusColor}
				{getRunStatusBadgeClass}
				{triggerTypeLabel}
			/>
		{:else if currentView === 'approvals'}
			{@render approvalsView()}
		{/if}
	</div>
{/snippet}

<!-- ═══════════════════ APPROVALS VIEW ═══════════════════ -->
{#snippet approvalsView()}
	<div class="auto-approvals" transition:fade={{ duration: 150 }}>
		<div class="auto-form-header">
			<button class="card-back-btn" onclick={() => currentView = 'list'}>
				<ArrowLeft size={16} />
			</button>
			<h3 class="auto-form-title">Pending Approvals</h3>
			<span class="card-badge card-badge--warning">{pendingApprovals.length}</span>
		</div>

		{#if pendingApprovals.length === 0}
			<div class="card-empty-state">
				<div class="card-empty-icon">
					<Shield size={32} />
				</div>
				<p class="card-empty-title">No pending approvals</p>
				<p class="card-empty-description">Automations requiring approval will show up here</p>
			</div>
		{:else}
			<div class="auto-approval-list">
				{#each pendingApprovals as approval (approval.id)}
					<div class="auto-approval-item" transition:slide={{ duration: 150 }}>
						<div class="auto-approval-info">
							<span class="auto-approval-name">{approval.automation_name || 'Automation'}</span>
							{#if approval.prompt_preview}
								<p class="auto-approval-prompt">{approval.prompt_preview}</p>
							{/if}
							<span class="fp-meta-chip">{formatTime(approval.created_at)}</span>
						</div>
						<div class="auto-approval-actions">
							<button class="card-btn-primary auto-approval-btn" onclick={() => handleApprove(approval.id)}>
								<CheckCircle size={14} />
								Approve
							</button>
							<button class="card-btn-secondary auto-approval-btn" onclick={() => handleDeny(approval.id)}>
								<XCircle size={14} />
								Deny
							</button>
						</div>
					</div>
				{/each}
			</div>
		{/if}
	</div>
{/snippet}

<!-- ═══════════════════ RENDER ═══════════════════ -->
{#if !mobile}
	{#if currentView === 'create' || currentView === 'edit'}
		<StudioEditorShell
			title={currentView === 'edit' && selectedAutomation ? selectedAutomation.name : 'New Automation'}
			subtitle={currentView === 'edit' ? 'Automation' : 'Create a new automation'}
			backLabel="Automations"
			onBack={backToList}
		>
			{#snippet actions()}
				<button type="button" class="ed-btn primary" onclick={handleSave} disabled={formSaving}>
					{#if formSaving}
						<Loader2 size={14} class="animate-spin" />
						<span>Saving...</span>
					{:else}
						<Check size={14} />
						<span>{currentView === 'edit' ? 'Save' : 'Create'}</span>
					{/if}
				</button>
			{/snippet}
			<div class="card-system" style="position: relative;">
				{@render automationsMainContent()}
			</div>
		</StudioEditorShell>
	{:else if currentView === 'detail' && selectedAutomation}
		<!-- Detail (run history) — full canvas; AutomationDetail renders its own header/back. -->
		<div class="automations-card card-system" style="position: relative;">
			{#if error}
				<div class="auto-error-banner" transition:slide={{ duration: 200 }}>
					<Banner severity="error" message={error} dismissible onDismiss={() => (error = '')} />
				</div>
			{/if}
			<div class="auto-content">
				<div class="auto-detail-center">
				<AutomationDetail
					automation={selectedAutomation}
					{runs}
					{runsLoading}
					{expandedRunIds}
					{detailStats}
					onBack={() => currentView = 'list'}
					onEdit={openEdit}
					onTrigger={handleTrigger}
					onDelete={handleDelete}
					onToggleRunExpanded={toggleRunExpanded}
					{formatTime}
					{formatCost}
					{formatDuration}
					{getStatusColor}
					{getRunStatusBadgeClass}
					{triggerTypeLabel}
				/>
				</div>
			</div>
		</div>
	{:else}
		<!-- List — Studio-v2 surface (header + approval banners + run list) -->
		{#if loading}
			<div class="auto-loading">
				<Loader2 size={32} class="animate-spin" />
				<p>Loading automations...</p>
			</div>
		{:else}
			<AutomationsStudio
				{automations}
				{pendingApprovals}
				{profiles}
				{loading}
				{error}
				{triggeringId}
				onOpenCreate={openCreate}
				onOpenDetail={openDetail}
				onOpenEdit={openEdit}
				onToggleEnabled={handleToggleEnabled}
				onTrigger={handleTrigger}
				onDelete={handleDelete}
				onApprove={handleApprove}
				onDeny={handleDeny}
				onClearError={() => error = ''}
				{formatTime}
				{formatCost}
				{triggerTypeLabel}
			/>
		{/if}
	{/if}
{:else if mobile}
	<div class="automations-card card-system mobile">
		<!-- Tabs -->
		<div class="auto-tabs">
			<button
				onclick={() => currentView = 'list'}
				class="auto-tab"
				class:active={currentView !== 'approvals'}
			>
				Automations
				<span class="auto-tab-count" class:active={currentView !== 'approvals'}>
					{automations.length}
				</span>
			</button>
			<button
				onclick={() => currentView = 'approvals'}
				class="auto-tab"
				class:active={currentView === 'approvals'}
			>
				Approvals
				{#if approvalCount > 0}
					<span class="auto-tab-count auto-approval-badge" class:active={currentView === 'approvals'}>
						{approvalCount}
					</span>
				{/if}
			</button>
		</div>

		<!-- Search and actions -->
		<div class="auto-search-bar">
			<div class="search-input-wrapper">
				<Search size={16} class="search-icon" />
				<input
					type="text"
					bind:value={searchQuery}
					placeholder="Search automations..."
					class="search-input"
				/>
			</div>
			<button onclick={openCreate} class="add-auto-btn">
				<Plus size={16} />
				New
			</button>
		</div>

		{@render automationsMainContent()}

		<!-- Footer -->
		<div class="auto-footer">
			<div class="footer-stats">
				{enabledCount} enabled / {runningCount} running
			</div>
		</div>
	</div>
{/if}

<!-- Delete automation confirmation -->
<ConfirmDialog
	open={!!pendingDeleteId}
	title="Delete automation"
	message="Are you sure you want to delete this automation?"
	confirmLabel="Delete"
	danger
	onConfirm={confirmDelete}
	onCancel={() => (pendingDeleteId = null)}
/>

<style>
	/* ═══════════════════ Layout ═══════════════════ */
	.automations-card {
		display: flex;
		flex-direction: column;
		height: 100%;
		overflow: hidden;
	}

	/* ═══════════════════ Tabs ═══════════════════ */
	.auto-tabs {
		display: flex;
		align-items: center;
		gap: 4px;
		padding: 8px 16px;
		border-bottom: 1px solid var(--border);
		background: var(--surface-1);
		overflow-x: auto;
		flex-shrink: 0;
	}

	.auto-tab {
		padding: 6px 12px;
		font-size: 0.8125rem;
		font-weight: 500;
		border-radius: calc(var(--radius, 0.75rem) * 0.5);
		white-space: nowrap;
		display: flex;
		align-items: center;
		gap: 6px;
		color: var(--muted-foreground);
		background: transparent;
		border: 1px solid transparent;
		cursor: pointer;
		transition: all 150ms ease;
	}

	.auto-tab:hover {
		color: var(--foreground);
		border-color: var(--accent);
		box-shadow: 0 0 8px color-mix(in oklch, var(--accent) 20%, transparent);
	}

	.auto-tab:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
	}

	.auto-tab.active {
		background: var(--selection-bg-emphasis);
		color: var(--selection-text);
		border-color: var(--selection-ring);
		box-shadow: var(--selection-shadow);
	}

	.auto-tab-count {
		padding: 1px 6px;
		font-size: 0.6875rem;
		border-radius: 9999px;
		background: var(--muted);
		color: var(--muted-foreground);
	}

	.auto-tab-count.active {
		background: color-mix(in oklch, var(--selection) 20%, transparent);
		color: var(--selection-text);
	}

	.auto-approval-badge {
		background: color-mix(in oklch, var(--warning) 25%, transparent) !important;
		color: var(--warning) !important;
	}

	/* ═══════════════════ Search Bar ═══════════════════ */
	.auto-search-bar {
		display: flex;
		align-items: center;
		gap: 12px;
		padding: 12px 16px;
		border-bottom: 1px solid var(--border);
		flex-shrink: 0;
	}

	.search-input-wrapper {
		position: relative;
		flex: 1;
	}

	.search-input-wrapper :global(.search-icon) {
		position: absolute;
		left: 12px;
		top: 50%;
		transform: translateY(-50%);
		color: var(--muted-foreground);
	}

	.search-input {
		width: 100%;
		padding: 8px 12px 8px 36px;
		border-radius: calc(var(--radius, 0.75rem) * 0.5);
		background: var(--input);
		border: 1px solid var(--border);
		color: var(--foreground);
		font-size: 0.875rem;
		transition: all 150ms ease;
	}

	.search-input::placeholder {
		color: var(--muted-foreground);
	}

	.search-input:focus {
		outline: none;
		border-color: var(--ring);
		box-shadow: 0 0 0 2px color-mix(in oklch, var(--ring) 25%, transparent);
	}

	.add-auto-btn {
		display: flex;
		align-items: center;
		gap: 6px;
		padding: 8px 12px;
		font-size: 0.875rem;
		font-weight: 500;
		border-radius: calc(var(--radius, 0.75rem) * 0.5);
		background: var(--primary);
		color: var(--primary-foreground);
		border: none;
		cursor: pointer;
		transition: all 150ms ease;
		white-space: nowrap;
	}

	.add-auto-btn:hover {
		filter: brightness(1.1);
	}

	.add-auto-btn:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
	}

	/* ═══════════════════ Toast / Error ═══════════════════ */
	.auto-banner {
		margin: 0 16px;
	}

	.auto-error-banner {
		margin: 12px 16px 0;
	}

	/* ═══════════════════ Content ═══════════════════ */
	.auto-content {
		flex: 1;
		overflow-y: auto;
	}

	/* Centered detail (run history) — matches the list's max-width wrap, not full-bleed. */
	.auto-detail-center {
		width: 100%;
		max-width: 1240px;
		min-height: 100%;
		margin: 0 auto;
	}

	.auto-loading {
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		padding: 48px;
		color: var(--muted-foreground);
		gap: 12px;
	}

	.auto-loading :global(svg) {
		color: var(--primary);
	}

	/* ═══════════════════ Approvals View ═══════════════════ */
	.auto-approvals {
		display: flex;
		flex-direction: column;
		height: 100%;
	}

	.auto-approval-list {
		flex: 1;
		overflow-y: auto;
		padding: 16px;
		display: flex;
		flex-direction: column;
		gap: 10px;
	}

	.auto-approval-item {
		display: flex;
		align-items: flex-start;
		gap: 12px;
		padding: 14px 16px;
		background: var(--surface-1);
		border: 1px solid var(--border-subtle, var(--border));
		border-radius: var(--radius-md, calc(var(--radius, 0.75rem) * 0.833));
	}

	.auto-approval-info {
		flex: 1;
		min-width: 0;
		display: flex;
		flex-direction: column;
		gap: 4px;
	}

	.auto-approval-name {
		font-size: var(--text-md, 0.875rem);
		font-weight: 600;
		color: var(--text-primary, var(--foreground));
	}

	.auto-approval-prompt {
		font-size: var(--text-sm, 0.75rem);
		color: var(--text-secondary, var(--muted-foreground));
		display: -webkit-box;
		-webkit-line-clamp: 3;
		line-clamp: 3;
		-webkit-box-orient: vertical;
		overflow: hidden;
		margin: 0;
		line-height: 1.4;
	}

	.auto-approval-actions {
		display: flex;
		flex-direction: column;
		gap: 6px;
		flex-shrink: 0;
	}

	.auto-approval-btn {
		padding: 6px 12px !important;
		font-size: var(--text-sm, 0.75rem) !important;
	}

	/* ═══════════════════ Footer ═══════════════════ */
	.auto-footer {
		display: flex;
		justify-content: flex-end;
		align-items: center;
		padding: 12px 16px;
		border-top: 1px solid var(--border);
		background: var(--surface-1);
		flex-shrink: 0;
	}

	.footer-stats {
		font-size: 0.75rem;
		color: var(--muted-foreground);
	}

	/* ═══════════════════ Spinner ═══════════════════ */
	:global(.animate-spin) {
		animation: spin 1s linear infinite;
	}

	@keyframes spin {
		from { transform: rotate(0deg); }
		to { transform: rotate(360deg); }
	}

	/* ═══════════════════ Mobile ═══════════════════ */
	.automations-card.mobile .auto-tabs {
		padding: 6px 8px;
		gap: 2px;
		scrollbar-width: none;
	}

	.automations-card.mobile .auto-tabs::-webkit-scrollbar {
		display: none;
	}

	.automations-card.mobile .auto-tab {
		padding: 5px 8px;
		font-size: 0.75rem;
		gap: 4px;
		flex-shrink: 0;
	}

	.automations-card.mobile .auto-tab-count {
		font-size: 0.625rem;
		padding: 0 4px;
	}

	.automations-card.mobile .auto-search-bar {
		flex-wrap: wrap;
		padding: 8px;
		gap: 8px;
	}

	.automations-card.mobile .search-input-wrapper {
		width: 100%;
	}

	.automations-card.mobile .add-auto-btn {
		width: 100%;
		justify-content: center;
	}

	@media (max-width: 639px) {
		.auto-approval-item {
			flex-direction: column;
		}

		.auto-approval-actions {
			flex-direction: row;
			width: 100%;
		}

		.auto-approval-btn {
			flex: 1;
		}
	}

</style>
