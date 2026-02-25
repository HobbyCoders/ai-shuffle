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
	import FeaturePageLayout from '$lib/components/layout/FeaturePageLayout.svelte';
	import type {
		Automation,
		AutomationCreate,
		AutomationUpdate,
		AutomationRun,
		AutomationApproval,
		AutomationTestResult,
		TriggerType,
		OutputType,
		ActionStep
	} from '$lib/api/automations';
	import {
		getAutomations,
		createAutomation,
		updateAutomation,
		deleteAutomation,
		enableAutomation,
		disableAutomation,
		triggerAutomation,
		testAutomation,
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
	import { timezonePreference } from '$lib/stores/timezonePreference';
	import { slide, fade } from 'svelte/transition';
	import {
		Search,
		X,
		Loader2,
		Plus,
		AlertCircle,
		CheckCircle,
		XCircle,
		Clock,
		Timer,
		Webhook,
		Radio,
		Play,
		Pencil,
		Trash2,
		ChevronDown,
		ChevronRight,
		ArrowLeft,
		ToggleLeft,
		ToggleRight,
		Shield,
		Zap,
		Activity,
		CalendarClock
	} from 'lucide-svelte';
	import './card-design-system.css';

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
	let formTriggerType: TriggerType = $state('cron');
	let formTriggerConfig: Record<string, any> = $state({});
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
	let showBudgetSection = $state(false);
	// Action steps form state
	let formActionSteps: ActionStep[] = $state([]);

	// Cron builder state
	let cronMode: 'preset' | 'custom' = $state('preset');
	let cronPreset = $state('0 9 * * *');
	let cronMinute = $state('0');
	let cronHour = $state('9');
	let cronDayOfMonth = $state('*');
	let cronMonth = $state('*');
	let cronDayOfWeek = $state('*');

	// Test result
	let testResult: AutomationTestResult | null = $state(null);

	// ── Derived ─────────────────────────────────────────────────────────
	const filteredAutomations = $derived(() => {
		if (!searchQuery.trim()) return automations;
		const q = searchQuery.toLowerCase();
		return automations.filter(
			(a) =>
				a.name.toLowerCase().includes(q) ||
				a.trigger_type.toLowerCase().includes(q) ||
				a.description?.toLowerCase().includes(q)
		);
	});

	const enabledCount = $derived(automations.filter((a) => a.is_enabled).length);
	const runningCount = $derived(automations.filter((a) => a.status === 'running').length);
	const approvalCount = $derived(pendingApprovals.length);

	// Get connectors that have triggers defined
	const connectorsWithTriggers = $derived(
		connectors.filter(c => {
			const typeInfo = connectorTypes.find(t => t.type === c.type);
			return typeInfo && typeInfo.triggers && typeInfo.triggers.length > 0;
		})
	);

	// Get connectors that have actions defined
	const connectorsWithActions = $derived(
		connectors.filter(c => {
			const typeInfo = connectorTypes.find(t => t.type === c.type);
			return typeInfo && typeInfo.actions && typeInfo.actions.length > 0;
		})
	);

	function getTriggersForConnector(connectorId: string) {
		const connector = connectors.find(c => c.id === connectorId);
		if (!connector) return [];
		const typeInfo = connectorTypes.find(t => t.type === connector.type);
		return typeInfo?.triggers || [];
	}

	function getActionsForConnector(connectorId: string) {
		const connector = connectors.find(c => c.id === connectorId);
		if (!connector) return [];
		const typeInfo = connectorTypes.find(t => t.type === connector.type);
		return typeInfo?.actions || [];
	}

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
		formTriggerType = 'cron';
		formTriggerConfig = { expression: '0 9 * * *' };
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
		showBudgetSection = false;
		formActionSteps = [];
		formError = '';
		testResult = null;
		// Reset cron builder
		cronMode = 'preset';
		cronPreset = '0 9 * * *';
		cronMinute = '0';
		cronHour = '9';
		cronDayOfMonth = '*';
		cronMonth = '*';
		cronDayOfWeek = '*';
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
		formTriggerType = automation.trigger_type;
		formTriggerConfig = { ...automation.trigger_config };
		if (automation.trigger_type === 'cron' && automation.trigger_config.expression) {
			syncCronFieldsFromExpression(automation.trigger_config.expression);
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
		formMaxTurns = String(automation.max_turns);
		formTimeoutSeconds = String(automation.timeout_seconds);
		formRequiresApproval = automation.requires_approval;
		showBudgetSection = !!(automation.max_cost_per_run || automation.max_cost_per_day || automation.max_cost_total || automation.max_runs_per_day);
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

		const validActionSteps = formActionSteps.filter(s => s.connector_id && s.action_id);

		return {
			name: formName.trim(),
			description: formDescription.trim() || undefined,
			trigger_type: formTriggerType,
			trigger_config: formTriggerType === 'cron'
				? { ...formTriggerConfig, timezone: timezonePreference.resolveTimezone() }
				: { ...formTriggerConfig },
			prompt: formPrompt.trim() || undefined,
			profile_id: formProfileId,
			project_id: formProjectId || undefined,
			output_config: outputConfig,
			action_steps: validActionSteps.length > 0 ? validActionSteps : undefined,
			max_turns: parseInt(formMaxTurns) || 1,
			timeout_seconds: parseInt(formTimeoutSeconds) || 300,
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
		const hasActionSteps = formActionSteps.some(s => s.connector_id && s.action_id);
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

	async function handleDelete(id: string) {
		if (!confirm('Are you sure you want to delete this automation?')) return;
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

	async function handleTest(id: string) {
		try {
			testResult = await testAutomation(id);
			setTimeout(() => {
				testResult = null;
			}, 5000);
		} catch (e: any) {
			console.error('Test failed:', e);
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

	function successRate(total: number, successful: number): string {
		if (total === 0) return 'N/A';
		return `${Math.round((successful / total) * 100)}%`;
	}

	// ── Cron builder helpers ────────────────────────────────────────────
	const CRON_PRESETS: Record<string, string> = {
		'0 * * * *': 'Every hour',
		'0 9 * * *': 'Every day at 9 AM',
		'0 9 * * 1-5': 'Every weekday at 9 AM',
		'0 9 * * 1': 'Every Monday at 9 AM',
		'0 0 1 * *': 'Every month on the 1st',
		'*/15 * * * *': 'Every 15 minutes',
		'*/30 * * * *': 'Every 30 minutes'
	};

	function parseCronExpression(expr: string): { minute: string; hour: string; dayOfMonth: string; month: string; dayOfWeek: string } | null {
		if (!expr) return null;
		const parts = expr.trim().split(/\s+/);
		if (parts.length !== 5) return null;
		return { minute: parts[0], hour: parts[1], dayOfMonth: parts[2], month: parts[3], dayOfWeek: parts[4] };
	}

	function buildCronFromFields(): string {
		return `${cronMinute} ${cronHour} ${cronDayOfMonth} ${cronMonth} ${cronDayOfWeek}`;
	}

	function syncCronFieldsFromExpression(expr: string) {
		const parsed = parseCronExpression(expr);
		if (parsed) {
			cronMinute = parsed.minute;
			cronHour = parsed.hour;
			cronDayOfMonth = parsed.dayOfMonth;
			cronMonth = parsed.month;
			cronDayOfWeek = parsed.dayOfWeek;
		}
		if (expr && CRON_PRESETS[expr.trim()]) {
			cronMode = 'preset';
			cronPreset = expr.trim();
		} else {
			cronMode = 'custom';
		}
	}

	function applyCronPreset(preset: string) {
		cronPreset = preset;
		formTriggerConfig = { ...formTriggerConfig, expression: preset };
		const parsed = parseCronExpression(preset);
		if (parsed) {
			cronMinute = parsed.minute;
			cronHour = parsed.hour;
			cronDayOfMonth = parsed.dayOfMonth;
			cronMonth = parsed.month;
			cronDayOfWeek = parsed.dayOfWeek;
		}
	}

	function applyCronFields() {
		const expr = buildCronFromFields();
		formTriggerConfig = { ...formTriggerConfig, expression: expr };
	}

	function describeCron(expr: string): string {
		if (!expr) return '';
		const parts = expr.trim().split(/\s+/);
		if (parts.length !== 5) return 'Invalid expression';
		const [min, hour, dom, month, dow] = parts;

		if (expr.trim() === '* * * * *') return 'Every minute';
		if (CRON_PRESETS[expr.trim()]) return CRON_PRESETS[expr.trim()];

		let desc = '';
		if (min !== '*' && hour !== '*') {
			const h = parseInt(hour);
			const m = parseInt(min);
			const ampm = h >= 12 ? 'PM' : 'AM';
			const h12 = h === 0 ? 12 : h > 12 ? h - 12 : h;
			desc = `At ${h12}:${String(m).padStart(2, '0')} ${ampm}`;
		} else if (min === '0' && hour === '*') {
			desc = 'Every hour';
		} else if (min.startsWith('*/')) {
			desc = `Every ${min.slice(2)} minutes`;
		} else {
			desc = `At minute ${min}`;
		}

		if (dow === '1-5') {
			desc += ', weekdays';
		} else if (dow === '0,6') {
			desc += ', weekends';
		} else if (dow !== '*') {
			const dayNames = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
			const idx = parseInt(dow);
			desc += `, on ${isNaN(idx) ? dow : (dayNames[idx] || dow)}`;
		}

		if (dom !== '*') {
			desc += `, on day ${dom}`;
		}

		if (month !== '*') {
			const monthNames = ['', 'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
			const idx = parseInt(month);
			desc += ` of ${isNaN(idx) ? month : (monthNames[idx] || month)}`;
		}

		return desc;
	}

	function formatHour(h: number): string {
		if (h === 0) return '12 AM';
		if (h < 12) return `${h} AM`;
		if (h === 12) return '12 PM';
		return `${h - 12} PM`;
	}
</script>

{#snippet automationsMainContent()}
	<!-- Test result toast -->
	{#if testResult}
		<div class="auto-toast" class:success={testResult.budget_ok} class:warn={!testResult.budget_ok} transition:slide={{ duration: 200 }}>
			{#if testResult.budget_ok}
				<CheckCircle size={16} />
			{:else}
				<AlertCircle size={16} />
			{/if}
			<span>{testResult.message}</span>
			<button onclick={() => testResult = null} class="toast-close">
				<X size={14} />
			</button>
		</div>
	{/if}

	<!-- Error display -->
	{#if error}
		<div class="auto-error" transition:slide={{ duration: 200 }}>
			<AlertCircle size={16} />
			<span>{error}</span>
			<button onclick={() => error = ''} class="toast-close">
				<X size={14} />
			</button>
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
			{@render listView()}
		{:else if currentView === 'create' || currentView === 'edit'}
			{@render formView()}
		{:else if currentView === 'detail' && selectedAutomation}
			{@render detailView()}
		{:else if currentView === 'approvals'}
			{@render approvalsView()}
		{/if}
	</div>
{/snippet}

<!-- ═══════════════════ LIST VIEW ═══════════════════ -->
{#snippet listView()}
	{#if filteredAutomations().length === 0}
		<div class="card-empty-state">
			<div class="card-empty-icon">
				<CalendarClock size={32} />
			</div>
			<p class="card-empty-title">
				{searchQuery ? 'No matching automations' : 'No automations yet'}
			</p>
			<p class="card-empty-description">
				{searchQuery ? 'Try a different search term' : 'Create automations to run AI tasks on a schedule or in response to events'}
			</p>
			{#if !searchQuery}
				<button class="card-empty-action" onclick={openCreate}>
					Create your first automation
				</button>
			{/if}
		</div>
	{:else}
		<div class="auto-list">
			{#each filteredAutomations() as automation (automation.id)}
				<div class="auto-list-item" transition:slide={{ duration: 150 }}>
					<!-- Status dot + name row -->
					<button class="auto-item-main" onclick={() => openDetail(automation)}>
						<span
							class="auto-status-dot"
							style="background: {getStatusColor(automation.status)}"
							title={automation.status}
						></span>
						<div class="auto-item-info">
							<div class="auto-item-header">
								<span class="auto-item-name">{automation.name}</span>
								<span class="card-badge card-badge--muted">{triggerTypeLabel(automation.trigger_type)}</span>
								{#if !automation.is_enabled}
									<span class="card-badge card-badge--warning">Disabled</span>
								{/if}
							</div>
							{#if automation.description}
								<span class="auto-item-desc">{automation.description}</span>
							{/if}
							<div class="auto-item-meta">
								<span class="fp-meta-chip">
									<Clock size={11} />
									{formatTime(automation.last_run_at)}
								</span>
								<span class="fp-meta-dot"></span>
								<span class="fp-meta-chip">{formatCost(automation.total_cost_used)}</span>
								<span class="fp-meta-dot"></span>
								<span class="fp-meta-chip">{automation.run_count} runs</span>
							</div>
						</div>
					</button>

					<!-- Actions -->
					<div class="auto-item-actions">
						<button
							class="card-action-btn"
							title={automation.is_enabled ? 'Disable' : 'Enable'}
							onclick={() => handleToggleEnabled(automation)}
						>
							{#if automation.is_enabled}
								<ToggleRight size={16} />
							{:else}
								<ToggleLeft size={16} />
							{/if}
						</button>
						<button
							class="card-action-btn"
							title="Trigger now"
							disabled={triggeringId === automation.id}
							onclick={() => handleTrigger(automation.id)}
						>
							{#if triggeringId === automation.id}
								<Loader2 size={16} class="animate-spin" />
							{:else}
								<Play size={16} />
							{/if}
						</button>
						<button
							class="card-action-btn"
							title="Edit"
							onclick={() => openEdit(automation)}
						>
							<Pencil size={16} />
						</button>
						<button
							class="card-action-btn card-action-btn--destructive"
							title="Delete"
							onclick={() => handleDelete(automation.id)}
						>
							<Trash2 size={16} />
						</button>
					</div>
				</div>
			{/each}
		</div>
	{/if}
{/snippet}

<!-- ═══════════════════ FORM VIEW (Create / Edit) ═══════════════════ -->
{#snippet formView()}
	<div class="auto-form" transition:fade={{ duration: 150 }}>
		{#if formError}
			<div class="card-error-banner" transition:slide={{ duration: 150 }}>
				<AlertCircle size={16} />
				<span style="flex:1">{formError}</span>
				<button onclick={() => formError = ''}>
					<X size={14} />
				</button>
			</div>
		{/if}

		<div class="auto-form-body">
			<!-- Name -->
			<div class="card-form-section">
				<label class="card-form-label" for="auto-name">Name <span class="required">*</span></label>
				<input
					id="auto-name"
					type="text"
					class="card-form-input"
					bind:value={formName}
					placeholder="e.g. Daily report generator"
				/>
			</div>

			<!-- Description -->
			<div class="card-form-section">
				<label class="card-form-label" for="auto-description">Description</label>
				<textarea
					id="auto-description"
					class="card-form-input card-form-textarea"
					style="min-height: 60px"
					bind:value={formDescription}
					placeholder="Optional description..."
				></textarea>
			</div>

			<!-- Trigger type selector -->
			<div class="card-form-section">
				<span class="card-form-label">Trigger Type <span class="required">*</span></span>
				<div class="auto-trigger-selector">
					<button
						class="auto-trigger-btn"
						class:active={formTriggerType === 'cron'}
						onclick={() => { formTriggerType = 'cron'; formTriggerConfig = { expression: cronPreset }; }}
					>
						<Clock size={14} />
						Cron
					</button>
					<button
						class="auto-trigger-btn"
						class:active={formTriggerType === 'interval'}
						onclick={() => { formTriggerType = 'interval'; formTriggerConfig = {}; }}
					>
						<Timer size={14} />
						Interval
					</button>
					<button
						class="auto-trigger-btn"
						class:active={formTriggerType === 'connector_event'}
						onclick={() => { formTriggerType = 'connector_event'; formTriggerConfig = {}; }}
					>
						<Radio size={14} />
						Event
					</button>
					<button
						class="auto-trigger-btn"
						class:active={formTriggerType === 'connector_trigger'}
						onclick={() => { formTriggerType = 'connector_trigger'; formTriggerConfig = {}; }}
					>
						<Zap size={14} />
						Connector Trigger
					</button>
					<button
						class="auto-trigger-btn"
						class:active={formTriggerType === 'webhook'}
						onclick={() => { formTriggerType = 'webhook'; formTriggerConfig = {}; }}
					>
						<Webhook size={14} />
						Webhook
					</button>
				</div>
			</div>

			<!-- Trigger config -->
			<div class="card-form-section" transition:slide={{ duration: 150 }}>
				{#if formTriggerType === 'cron'}
					<span class="card-form-label">Schedule</span>
					<!-- Preset/Custom toggle -->
					<div class="cron-mode-selector">
						<button
							class="auto-trigger-btn"
							class:active={cronMode === 'preset'}
							onclick={() => cronMode = 'preset'}
						>
							Preset
						</button>
						<button
							class="auto-trigger-btn"
							class:active={cronMode === 'custom'}
							onclick={() => cronMode = 'custom'}
						>
							Custom
						</button>
					</div>

					{#if cronMode === 'preset'}
						<select
							class="card-form-input"
							value={cronPreset}
							onchange={(e) => applyCronPreset(e.currentTarget.value)}
						>
							<option value="*/15 * * * *">Every 15 minutes</option>
							<option value="*/30 * * * *">Every 30 minutes</option>
							<option value="0 * * * *">Every hour</option>
							<option value="0 9 * * *">Every day at 9 AM</option>
							<option value="0 9 * * 1-5">Every weekday at 9 AM</option>
							<option value="0 9 * * 1">Every Monday at 9 AM</option>
							<option value="0 0 1 * *">Every month on the 1st</option>
						</select>
					{:else}
						<div class="cron-builder-grid">
							<div class="cron-field">
								<label class="card-form-label" for="cron-minute">Minute</label>
								<select id="cron-minute" class="card-form-input" bind:value={cronMinute} onchange={() => applyCronFields()}>
									<option value="*">Every minute</option>
									<option value="*/5">Every 5 min</option>
									<option value="*/10">Every 10 min</option>
									<option value="*/15">Every 15 min</option>
									<option value="*/30">Every 30 min</option>
									<option value="0">:00</option>
									<option value="15">:15</option>
									<option value="30">:30</option>
									<option value="45">:45</option>
								</select>
							</div>
							<div class="cron-field">
								<label class="card-form-label" for="cron-hour">Hour</label>
								<select id="cron-hour" class="card-form-input" bind:value={cronHour} onchange={() => applyCronFields()}>
									<option value="*">Every hour</option>
									{#each Array.from({length: 24}, (_, i) => i) as h}
										<option value={String(h)}>{formatHour(h)}</option>
									{/each}
								</select>
							</div>
							<div class="cron-field">
								<label class="card-form-label" for="cron-dom">Day of Month</label>
								<select id="cron-dom" class="card-form-input" bind:value={cronDayOfMonth} onchange={() => applyCronFields()}>
									<option value="*">Every day</option>
									{#each Array.from({length: 31}, (_, i) => i + 1) as d}
										<option value={String(d)}>{d}</option>
									{/each}
								</select>
							</div>
							<div class="cron-field">
								<label class="card-form-label" for="cron-month">Month</label>
								<select id="cron-month" class="card-form-input" bind:value={cronMonth} onchange={() => applyCronFields()}>
									<option value="*">Every month</option>
									<option value="1">January</option>
									<option value="2">February</option>
									<option value="3">March</option>
									<option value="4">April</option>
									<option value="5">May</option>
									<option value="6">June</option>
									<option value="7">July</option>
									<option value="8">August</option>
									<option value="9">September</option>
									<option value="10">October</option>
									<option value="11">November</option>
									<option value="12">December</option>
								</select>
							</div>
							<div class="cron-field" style="grid-column: 1 / -1">
								<label class="card-form-label" for="cron-dow">Day of Week</label>
								<select id="cron-dow" class="card-form-input" bind:value={cronDayOfWeek} onchange={() => applyCronFields()}>
									<option value="*">Every day</option>
									<option value="1-5">Weekdays (Mon–Fri)</option>
									<option value="0,6">Weekends</option>
									<option value="1">Monday</option>
									<option value="2">Tuesday</option>
									<option value="3">Wednesday</option>
									<option value="4">Thursday</option>
									<option value="5">Friday</option>
									<option value="6">Saturday</option>
									<option value="0">Sunday</option>
								</select>
							</div>
						</div>
					{/if}

					<!-- Summary bar -->
					{#if formTriggerConfig.expression}
						<div class="cron-summary">
							<span class="cron-summary-text">{describeCron(formTriggerConfig.expression)}</span>
							<code class="cron-summary-expr">{formTriggerConfig.expression}</code>
						</div>
					{/if}

					<!-- Raw expression escape hatch -->
					<details class="cron-raw-details">
						<summary class="card-form-hint" style="cursor: pointer; user-select: none;">Edit raw expression</summary>
						<input
							type="text"
							class="card-form-input card-form-input--mono"
							style="margin-top: 6px"
							value={formTriggerConfig.expression || ''}
							oninput={(e) => {
								formTriggerConfig = { ...formTriggerConfig, expression: e.currentTarget.value };
								syncCronFieldsFromExpression(e.currentTarget.value);
							}}
							placeholder="0 9 * * *"
						/>
					</details>

					<!-- Timezone hint -->
					<p class="card-form-hint" style="margin-top: 8px">
						Uses your settings timezone ({timezonePreference.resolveTimezone()})
					</p>
				{:else if formTriggerType === 'interval'}
					<label class="card-form-label" for="auto-interval-secs">Interval (seconds)</label>
					<input
						id="auto-interval-secs"
						type="number"
						class="card-form-input"
						min="60"
						bind:value={formTriggerConfig.seconds}
						placeholder="3600"
					/>
					<p class="card-form-hint">Minimum 60 seconds</p>
				{:else if formTriggerType === 'connector_event'}
					<label class="card-form-label" for="auto-connector-id">Connector</label>
					<select
						id="auto-connector-id"
						class="card-form-input"
						bind:value={formTriggerConfig.connector_id}
					>
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
					<label class="card-form-label" for="auto-event-type" style="margin-top: 12px">Event Type</label>
					<select
						id="auto-event-type"
						class="card-form-input"
						bind:value={formTriggerConfig.event_type}
					>
						<option value="">Select an event...</option>
						<option value="session.complete">Session complete</option>
						<option value="session.error">Session error</option>
						<option value="session.started">Session started</option>
						<option value="permission.requested">Permission requested</option>
						<option value="input.needed">Input needed</option>
					</select>
				{:else if formTriggerType === 'connector_trigger'}
					<label class="card-form-label" for="auto-ct-connector">Connector</label>
					<select
						id="auto-ct-connector"
						class="card-form-input"
						bind:value={formTriggerConfig.connector_id}
						onchange={() => { formTriggerConfig = { ...formTriggerConfig, trigger_id: '' }; }}
					>
						<option value="">Select a connector...</option>
						{#each connectorsWithTriggers as c}
							<option value={c.id}>{c.name} ({c.type})</option>
						{/each}
					</select>

					{#if formTriggerConfig.connector_id}
						<label class="card-form-label" for="auto-ct-trigger" style="margin-top: 12px">Trigger</label>
						<select
							id="auto-ct-trigger"
							class="card-form-input"
							bind:value={formTriggerConfig.trigger_id}
						>
							<option value="">Select a trigger...</option>
							{#each getTriggersForConnector(formTriggerConfig.connector_id) as trigger}
								<option value={trigger.id}>{trigger.name}</option>
							{/each}
						</select>
						{#if formTriggerConfig.trigger_id}
							{@const triggerDef = getTriggersForConnector(formTriggerConfig.connector_id).find(t => t.id === formTriggerConfig.trigger_id)}
							{#if triggerDef}
								<p class="card-form-hint" style="margin-top: 6px">{triggerDef.description}</p>
							{/if}
						{/if}
					{/if}
				{:else if formTriggerType === 'webhook'}
					{#if currentView === 'edit' && selectedAutomation}
						<label class="card-form-label" for="auto-webhook-url">Webhook URL</label>
						<input
							id="auto-webhook-url"
							type="text"
							class="card-form-input card-form-input--mono"
							value={formTriggerConfig.webhook_url || 'Generated after creation'}
							readonly
						/>
						{#if formTriggerConfig.secret}
							<label class="card-form-label" for="auto-webhook-secret" style="margin-top: 12px">Secret</label>
							<input
								id="auto-webhook-secret"
								type="text"
								class="card-form-input card-form-input--mono"
								value={formTriggerConfig.secret}
								readonly
							/>
						{/if}
					{:else}
						<p class="card-form-hint">Webhook URL and secret will be generated after creation.</p>
					{/if}
				{/if}
			</div>

			<!-- Prompt -->
			<div class="card-form-section">
				<label class="card-form-label" for="auto-prompt">
					Prompt {#if formActionSteps.length === 0}<span class="required">*</span>{/if}
				</label>
				<textarea
					id="auto-prompt"
					class="card-form-input card-form-textarea"
					style="min-height: 120px"
					bind:value={formPrompt}
					placeholder="Enter the prompt template for this automation..."
				></textarea>
				<p class="card-form-hint">
					{#if formActionSteps.length > 0}
						Optional when action steps are configured. The AI agent prompt runs before action steps.
					{:else}
						The main instruction sent to the AI agent when this automation runs
					{/if}
				</p>
			</div>

			<!-- Action Steps -->
			<div class="card-form-section">
				<div style="display: flex; align-items: center; justify-content: space-between;">
					<span class="card-form-label">Action Steps</span>
					<button
						class="card-action-btn"
						onclick={() => { formActionSteps = [...formActionSteps, { type: 'connector_action', connector_id: '', action_id: '', params: {} }]; }}
					>
						<Plus size={12} />
						Add Step
					</button>
				</div>
				<p class="card-form-hint">Actions to execute when this automation runs. Can be used with or without a prompt.</p>

				{#each formActionSteps as step, i}
					<div class="action-step-card" transition:slide={{ duration: 150 }}>
						<div style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px;">
							<span class="card-form-label" style="margin: 0;">Step {i + 1}</span>
							<button
								class="card-action-btn card-action-btn--destructive"
								onclick={() => { formActionSteps = formActionSteps.filter((_, idx) => idx !== i); }}
							>
								<X size={12} />
							</button>
						</div>
						<select
							class="card-form-input"
							bind:value={step.connector_id}
							onchange={() => { step.action_id = ''; step.params = {}; formActionSteps = [...formActionSteps]; }}
						>
							<option value="">Select connector...</option>
							{#each connectorsWithActions as c}
								<option value={c.id}>{c.name} ({c.type})</option>
							{/each}
						</select>
						{#if step.connector_id}
							<select
								class="card-form-input"
								style="margin-top: 8px"
								bind:value={step.action_id}
								onchange={() => { step.params = {}; formActionSteps = [...formActionSteps]; }}
							>
								<option value="">Select action...</option>
								{#each getActionsForConnector(step.connector_id) as action}
									<option value={action.id}>{action.name}</option>
								{/each}
							</select>
						{/if}
						{#if step.action_id}
							{@const actionDef = getActionsForConnector(step.connector_id).find(a => a.id === step.action_id)}
							{#if actionDef}
								<p class="card-form-hint" style="margin-top: 4px">{actionDef.description}</p>
								{#if actionDef.input_schema?.properties}
									<div style="margin-top: 8px;">
										{#each Object.entries(actionDef.input_schema.properties) as [key, schemaDef]}
											{@const schemaObj = schemaDef as Record<string, any>}
											<label class="card-form-label" for="step-{i}-{key}" style="font-size: 11px;">{key}</label>
											<input
												id="step-{i}-{key}"
												type="text"
												class="card-form-input"
												style="font-size: 12px;"
												value={step.params[key] || ''}
												oninput={(e) => { step.params[key] = e.currentTarget.value; formActionSteps = [...formActionSteps]; }}
												placeholder={schemaObj.description || key}
											/>
										{/each}
									</div>
									<p class="card-form-hint" style="margin-top: 4px;">Use {'{{trigger.data.field}}'} or {'{{response}}'} for template variables</p>
								{/if}
							{/if}
						{/if}
					</div>
				{/each}
			</div>

			<!-- Profile selector -->
			<div class="card-form-section">
				<label class="card-form-label" for="auto-profile">Profile <span class="required">*</span></label>
				<select id="auto-profile" class="card-form-input" bind:value={formProfileId}>
					<option value="">Select a profile...</option>
					{#each profiles as profile}
						<option value={profile.id}>{profile.name}</option>
					{/each}
				</select>
			</div>

			<!-- Project selector -->
			<div class="card-form-section">
				<label class="card-form-label" for="auto-project">Project</label>
				<select id="auto-project" class="card-form-input" bind:value={formProjectId}>
					<option value="">None (global)</option>
					{#each projects as project}
						<option value={project.id}>{project.name}</option>
					{/each}
				</select>
			</div>

			<!-- Output type -->
			<div class="card-form-section">
				<span class="card-form-label">Output Type</span>
				<div class="auto-trigger-selector">
					<button
						class="auto-trigger-btn"
						class:active={formOutputType === 'session'}
						onclick={() => { formOutputType = 'session'; formOutputConfig = {}; }}
					>
						Session
					</button>
					<button
						class="auto-trigger-btn"
						class:active={formOutputType === 'connector'}
						onclick={() => { formOutputType = 'connector'; formOutputConfig = {}; }}
					>
						Connector
					</button>
					<button
						class="auto-trigger-btn"
						class:active={formOutputType === 'webhook'}
						onclick={() => { formOutputType = 'webhook'; formOutputConfig = {}; }}
					>
						Webhook
					</button>
					<button
						class="auto-trigger-btn"
						class:active={formOutputType === 'none'}
						onclick={() => { formOutputType = 'none'; formOutputConfig = {}; }}
					>
						None
					</button>
				</div>
			</div>

			<!-- Output config -->
			{#if formOutputType === 'connector'}
				<div class="card-form-section" transition:slide={{ duration: 150 }}>
					<label class="card-form-label" for="auto-output-connector">Connector</label>
					<select
						id="auto-output-connector"
						class="card-form-input"
						bind:value={formOutputConfig.connector_id}
					>
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
					<label class="card-form-label" for="auto-output-chat-id" style="margin-top: 12px">Platform Chat ID</label>
					<input
						id="auto-output-chat-id"
						type="text"
						class="card-form-input"
						bind:value={formOutputConfig.platform_chat_id}
						placeholder="chat / channel ID / email address"
					/>
					<p class="card-form-help" style="margin-top: 4px; font-size: 11px; opacity: 0.6;">
						e.g. Telegram chat ID, Discord channel ID, email address
					</p>
				</div>
			{:else if formOutputType === 'webhook'}
				<div class="card-form-section" transition:slide={{ duration: 150 }}>
					<label class="card-form-label" for="auto-output-webhook-url">Webhook URL</label>
					<input
						id="auto-output-webhook-url"
						type="text"
						class="card-form-input"
						bind:value={formOutputConfig.url}
						placeholder="https://example.com/webhook"
					/>
				</div>
			{/if}

			<!-- Budget & Limits (expandable) -->
			<div class="card-form-section">
				<button class="auto-expand-btn" onclick={() => showBudgetSection = !showBudgetSection}>
					{#if showBudgetSection}
						<ChevronDown size={14} />
					{:else}
						<ChevronRight size={14} />
					{/if}
					Budget & Limits
				</button>

				{#if showBudgetSection}
					<div class="auto-budget-grid" transition:slide={{ duration: 150 }}>
						<div class="auto-budget-field">
							<label class="card-form-label" for="auto-max-cost-run">Max cost/run ($)</label>
							<input id="auto-max-cost-run" type="number" class="card-form-input" step="0.01" min="0" bind:value={formMaxCostPerRun} placeholder="No limit" />
						</div>
						<div class="auto-budget-field">
							<label class="card-form-label" for="auto-max-cost-day">Max cost/day ($)</label>
							<input id="auto-max-cost-day" type="number" class="card-form-input" step="0.01" min="0" bind:value={formMaxCostPerDay} placeholder="No limit" />
						</div>
						<div class="auto-budget-field">
							<label class="card-form-label" for="auto-max-cost-total">Max total cost ($)</label>
							<input id="auto-max-cost-total" type="number" class="card-form-input" step="0.01" min="0" bind:value={formMaxCostTotal} placeholder="No limit" />
						</div>
						<div class="auto-budget-field">
							<label class="card-form-label" for="auto-max-runs-day">Max runs/day</label>
							<input id="auto-max-runs-day" type="number" class="card-form-input" min="1" bind:value={formMaxRunsPerDay} placeholder="No limit" />
						</div>
						<div class="auto-budget-field">
							<label class="card-form-label" for="auto-max-turns">Max turns</label>
							<input id="auto-max-turns" type="number" class="card-form-input" min="1" bind:value={formMaxTurns} />
						</div>
						<div class="auto-budget-field">
							<label class="card-form-label" for="auto-timeout">Timeout (s)</label>
							<input id="auto-timeout" type="number" class="card-form-input" min="10" bind:value={formTimeoutSeconds} />
						</div>
					</div>
				{/if}
			</div>

			<!-- Requires approval toggle -->
			<div class="card-form-section">
				<label class="auto-toggle-row">
					<button
						class="auto-toggle"
						class:active={formRequiresApproval}
						onclick={() => formRequiresApproval = !formRequiresApproval}
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
		</div>

	</div>
{/snippet}

<!-- ═══════════════════ DETAIL VIEW ═══════════════════ -->
{#snippet detailView()}
	{#if selectedAutomation}
		<div class="auto-detail" transition:fade={{ duration: 150 }}>
			<!-- Detail header -->
			<div class="auto-detail-header">
				<button class="card-back-btn" onclick={() => currentView = 'list'}>
					<ArrowLeft size={16} />
				</button>
				<div class="auto-detail-header-info">
					<div class="auto-detail-name-row">
						<span
							class="auto-status-dot"
							style="background: {getStatusColor(selectedAutomation.status)}"
						></span>
						<h3 class="auto-detail-name">{selectedAutomation.name}</h3>
						<span class="card-badge card-badge--muted">{triggerTypeLabel(selectedAutomation.trigger_type)}</span>
					</div>
					{#if selectedAutomation.description}
						<p class="auto-detail-desc">{selectedAutomation.description}</p>
					{/if}
				</div>
				<div class="auto-detail-header-actions">
					<button class="card-action-btn" title="Edit" onclick={() => openEdit(selectedAutomation!)}>
						<Pencil size={16} />
					</button>
					<button class="card-action-btn" title="Trigger" onclick={() => handleTrigger(selectedAutomation!.id)}>
						<Play size={16} />
					</button>
					<button class="card-action-btn card-action-btn--destructive" title="Delete" onclick={() => handleDelete(selectedAutomation!.id)}>
						<Trash2 size={16} />
					</button>
				</div>
			</div>

			<!-- Stats summary -->
			{#if detailStats}
				<div class="auto-stats-row" transition:slide={{ duration: 150 }}>
					<div class="auto-stat">
						<span class="auto-stat-value">{detailStats.total_runs}</span>
						<span class="auto-stat-label">Total Runs</span>
					</div>
					<div class="auto-stat">
						<span class="auto-stat-value">{formatCost(detailStats.total_cost_usd)}</span>
						<span class="auto-stat-label">Total Cost</span>
					</div>
					<div class="auto-stat">
						<span class="auto-stat-value">{successRate(detailStats.total_runs, detailStats.successful_runs)}</span>
						<span class="auto-stat-label">Success Rate</span>
					</div>
					<div class="auto-stat">
						<span class="auto-stat-value">{detailStats.failed_runs}</span>
						<span class="auto-stat-label">Failures</span>
					</div>
				</div>
			{/if}

			<!-- Action Steps -->
			{#if selectedAutomation.action_steps?.length}
				<div class="card-form-section" style="padding: 12px 16px; border-bottom: 1px solid var(--border-subtle, var(--border));">
					<span class="card-form-label">Action Steps</span>
					{#each selectedAutomation.action_steps as step, i}
						<div class="auto-detail-row">
							<span class="auto-detail-label">Step {i + 1}</span>
							<span class="auto-detail-value">{step.action_id} on {step.connector_id}</span>
						</div>
					{/each}
				</div>
			{/if}

			<!-- Runs table -->
			<div class="auto-runs-section">
				<h4 class="auto-section-title">Run History</h4>
				{#if runsLoading}
					<div class="auto-loading" style="padding: 24px">
						<Loader2 size={20} class="animate-spin" />
						<p>Loading runs...</p>
					</div>
				{:else if runs.length === 0}
					<p class="auto-runs-empty">No runs yet</p>
				{:else}
					<div class="auto-runs-list">
						{#each runs as run (run.id)}
							<div class="auto-run-item">
								<button class="auto-run-row" onclick={() => toggleRunExpanded(run.id)}>
									{#if expandedRunIds.has(run.id)}
										<ChevronDown size={14} />
									{:else}
										<ChevronRight size={14} />
									{/if}
									<span class="card-badge {getRunStatusBadgeClass(run.status)}">{run.status}</span>
									<span class="auto-run-meta">
										{run.trigger_data?.source || 'manual'}
									</span>
									<span class="auto-run-meta">{formatCost(run.cost_usd)}</span>
									<span class="auto-run-meta">{formatDuration(run.duration_ms)}</span>
									<span class="auto-run-meta auto-run-time">{formatTime(run.created_at)}</span>
								</button>
								{#if expandedRunIds.has(run.id)}
									<div class="auto-run-expanded" transition:slide={{ duration: 150 }}>
										{#if run.response}
											<div class="auto-run-response">
												<span class="card-form-label">Response</span>
												<pre class="auto-run-pre">{run.response}</pre>
											</div>
										{/if}
										{#if run.error}
											<div class="auto-run-error">
												<span class="card-form-label">Error</span>
												<pre class="auto-run-pre auto-run-pre--error">{run.error}</pre>
											</div>
										{/if}
										<div class="auto-run-details-grid">
											<span>Tokens in: {run.tokens_in}</span>
											<span>Tokens out: {run.tokens_out}</span>
											<span>Output: {run.output_status}</span>
											{#if run.session_id}
												<span>Session: {run.session_id.slice(0, 8)}...</span>
											{/if}
										</div>
									</div>
								{/if}
							</div>
						{/each}
					</div>
				{/if}
			</div>
		</div>
	{/if}
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
		<FeaturePageLayout
			icon={CalendarClock}
			title="Automations"
			breadcrumbs={[
				{ label: 'Automations', onclick: backToList },
				{ label: currentView === 'edit' && selectedAutomation ? selectedAutomation.name : 'New Automation' }
			]}
			noPadding={true}
		>
			{#snippet footer()}
				<button class="fp-btn fp-btn--primary" onclick={handleSave} disabled={formSaving}>
					{#if formSaving}
						<Loader2 size={14} class="animate-spin" />
						<span class="btn-label">Saving...</span>
					{:else}
						<span class="btn-label">{currentView === 'edit' ? 'Save Changes' : 'Create Automation'}</span>
					{/if}
				</button>
			{/snippet}
			{#snippet children()}
				<div class="automations-card card-system" style="position: relative;">
					{@render automationsMainContent()}
				</div>
			{/snippet}
		</FeaturePageLayout>
	{:else}
		<FeaturePageLayout
			icon={CalendarClock}
			title="Automations"
			itemCount={automations.length}
			itemLabel="automation"
		>
			{#snippet headerTabs()}
				<div class="fp-tab-bar">
					<button
						class="fp-tab"
						class:active={currentView === 'list' || currentView === 'detail'}
						onclick={() => currentView = 'list'}
					>
						All
						<span class="fp-tab-count">{automations.length}</span>
					</button>
					<button
						class="fp-tab"
						class:active={currentView === 'approvals'}
						onclick={() => currentView = 'approvals'}
					>
						Approvals
						{#if approvalCount > 0}
							<span class="fp-tab-count auto-approval-badge">{approvalCount}</span>
						{/if}
					</button>
				</div>
			{/snippet}
			{#snippet footer()}
				<button class="fp-btn fp-btn--primary" onclick={openCreate} title="New Automation">
					<Plus size={14} /> <span class="btn-label">New Automation</span>
				</button>
			{/snippet}
			{#snippet children()}
				<div class="automations-card card-system" style="position: relative;">
					<div class="fp-search-bar">
						<div class="fph-search-wrapper">
							<Search size={14} />
							<input
								type="text"
								bind:value={searchQuery}
								placeholder="Search automations..."
								class="fph-search-input"
							/>
						</div>
					</div>
					{@render automationsMainContent()}
				</div>
			{/snippet}
		</FeaturePageLayout>
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
	.auto-toast {
		display: flex;
		align-items: center;
		gap: 8px;
		margin: 0 16px;
		padding: 10px 12px;
		border-radius: calc(var(--radius, 0.75rem) * 0.5);
		font-size: 0.8125rem;
	}

	.auto-toast.success {
		background: color-mix(in oklch, var(--success) 15%, transparent);
		color: var(--success);
		border: 1px solid color-mix(in oklch, var(--success) 30%, transparent);
	}

	.auto-toast.warn {
		background: color-mix(in oklch, var(--warning) 15%, transparent);
		color: var(--warning);
		border: 1px solid color-mix(in oklch, var(--warning) 30%, transparent);
	}

	.auto-error {
		display: flex;
		align-items: center;
		gap: 8px;
		margin: 12px 16px 0;
		padding: 10px 12px;
		border-radius: calc(var(--radius, 0.75rem) * 0.5);
		background: color-mix(in oklch, var(--destructive) 15%, transparent);
		color: var(--destructive);
		font-size: 0.8125rem;
	}

	.toast-close {
		margin-left: auto;
		padding: 2px;
		background: transparent;
		border: none;
		cursor: pointer;
		opacity: 0.7;
		color: inherit;
		transition: opacity 150ms ease;
	}

	.toast-close:hover {
		opacity: 1;
	}

	/* ═══════════════════ Content ═══════════════════ */
	.auto-content {
		flex: 1;
		overflow-y: auto;
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

	/* ═══════════════════ List View ═══════════════════ */
	.auto-list {
		display: flex;
		flex-direction: column;
		gap: 8px;
		padding: 16px;
	}

	.auto-list-item {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 12px 16px;
		background: var(--surface-0);
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-md, calc(var(--radius, 0.75rem) * 0.833));
		transition: border-color 150ms, box-shadow 150ms, transform 150ms;
	}

	.auto-list-item:hover {
		border-color: var(--border-emphasis);
		box-shadow: var(--shadow-sm);
		transform: translateY(-1px);
	}

	.auto-item-main {
		flex: 1;
		min-width: 0;
		display: flex;
		align-items: flex-start;
		gap: 10px;
		background: none;
		border: none;
		color: inherit;
		cursor: pointer;
		text-align: left;
		padding: 0;
	}

	.auto-status-dot {
		width: 8px;
		height: 8px;
		border-radius: 50%;
		flex-shrink: 0;
		margin-top: 5px;
	}

	.auto-item-info {
		flex: 1;
		min-width: 0;
		display: flex;
		flex-direction: column;
		gap: 4px;
	}

	.auto-item-header {
		display: flex;
		align-items: center;
		gap: 6px;
		flex-wrap: wrap;
	}

	.auto-item-name {
		font-size: var(--text-md, 0.875rem);
		font-weight: 600;
		color: var(--text-primary, var(--foreground));
	}

	.auto-item-desc {
		font-size: var(--text-sm, 0.75rem);
		color: var(--text-secondary, var(--muted-foreground));
		display: -webkit-box;
		-webkit-line-clamp: 1;
		-webkit-box-orient: vertical;
		overflow: hidden;
	}

	.auto-item-meta {
		display: flex;
		align-items: center;
		gap: 6px;
		flex-wrap: wrap;
		margin-top: 2px;
	}

	.auto-item-actions {
		display: flex;
		align-items: center;
		justify-content: center;
		gap: 2px;
		flex-shrink: 0;
	}

	/* ═══════════════════ Form View ═══════════════════ */
	.auto-form {
		display: flex;
		flex-direction: column;
		height: 100%;
	}

	.auto-form-body {
		flex: 1;
		overflow-y: auto;
		padding: 16px;
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

	/* ═══════════════════ Cron Builder ═══════════════════ */
	.cron-mode-selector {
		display: flex;
		gap: 6px;
		margin-bottom: 10px;
	}

	.cron-builder-grid {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: 10px;
	}

	.cron-field {
		display: flex;
		flex-direction: column;
	}

	.cron-summary {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 8px;
		margin-top: 10px;
		padding: 8px 12px;
		background: var(--surface-1);
		border: 1px solid var(--border-subtle, var(--border));
		border-radius: var(--radius-sm, calc(var(--radius, 0.75rem) * 0.5));
		font-size: var(--text-sm, 0.75rem);
	}

	.cron-summary-text {
		color: var(--text-primary, var(--foreground));
		font-weight: 500;
	}

	.cron-summary-expr {
		font-family: var(--card-font-mono, monospace);
		color: var(--text-secondary, var(--muted-foreground));
		font-size: var(--text-xs, 0.6875rem);
		padding: 2px 6px;
		background: var(--muted);
		border-radius: 4px;
		flex-shrink: 0;
	}

	.cron-raw-details {
		margin-top: 8px;
	}

	.cron-raw-details summary {
		font-size: var(--text-sm, 0.75rem);
	}

	/* Action step cards */
	.action-step-card {
		background: var(--card-surface-elevated, rgba(255, 255, 255, 0.03));
		border: 1px solid var(--card-border, rgba(255, 255, 255, 0.08));
		border-radius: 8px;
		padding: 12px;
		margin-top: 8px;
	}

	/* Detail view rows */
	.auto-detail-row {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 4px 0;
		font-size: var(--text-sm, 0.75rem);
	}

	.auto-detail-label {
		color: var(--text-secondary, var(--muted-foreground));
		font-weight: 500;
		min-width: 60px;
	}

	.auto-detail-value {
		color: var(--text-primary, var(--foreground));
		font-family: var(--card-font-mono, monospace);
		font-size: var(--text-xs, 0.6875rem);
	}

	/* Budget section */
	.auto-expand-btn {
		display: flex;
		align-items: center;
		gap: 6px;
		padding: 6px 0;
		background: none;
		border: none;
		color: var(--text-secondary, var(--muted-foreground));
		font-size: var(--text-md, 0.875rem);
		font-weight: 600;
		cursor: pointer;
		transition: color 150ms ease;
	}

	.auto-expand-btn:hover {
		color: var(--text-primary, var(--foreground));
	}

	.auto-budget-grid {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: 12px;
		margin-top: 12px;
	}

	.auto-budget-field {
		display: flex;
		flex-direction: column;
	}

	/* Approval toggle */
	.auto-toggle-row {
		display: flex;
		align-items: center;
		gap: 10px;
		cursor: pointer;
	}

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

	/* ═══════════════════ Detail View ═══════════════════ */
	.auto-detail {
		display: flex;
		flex-direction: column;
		height: 100%;
	}

	.auto-detail-header {
		display: flex;
		align-items: flex-start;
		gap: 8px;
		padding: 12px 16px;
		border-bottom: 1px solid var(--border-subtle, var(--border));
		flex-shrink: 0;
	}

	.auto-detail-header-info {
		flex: 1;
		min-width: 0;
	}

	.auto-detail-name-row {
		display: flex;
		align-items: center;
		gap: 8px;
	}

	.auto-detail-name {
		font-size: var(--text-lg, 1rem);
		font-weight: 600;
		color: var(--text-primary, var(--foreground));
		margin: 0;
	}

	.auto-detail-desc {
		font-size: var(--text-sm, 0.75rem);
		color: var(--text-secondary, var(--muted-foreground));
		margin: 4px 0 0 0;
	}

	.auto-detail-header-actions {
		display: flex;
		gap: 2px;
		flex-shrink: 0;
	}

	/* Stats row */
	.auto-stats-row {
		display: flex;
		gap: 16px;
		padding: 12px 16px;
		border-bottom: 1px solid var(--border-subtle, var(--border));
		background: var(--surface-1);
		flex-shrink: 0;
		flex-wrap: wrap;
	}

	.auto-stat {
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: 2px;
		flex: 1;
		min-width: 60px;
	}

	.auto-stat-value {
		font-size: var(--text-md, 0.875rem);
		font-weight: 700;
		color: var(--text-primary, var(--foreground));
	}

	.auto-stat-label {
		font-size: var(--text-xs, 0.6875rem);
		color: var(--text-tertiary, var(--muted-foreground));
	}

	/* Runs section */
	.auto-runs-section {
		flex: 1;
		overflow-y: auto;
		padding: 16px;
	}

	.auto-section-title {
		font-size: var(--text-md, 0.875rem);
		font-weight: 600;
		color: var(--text-primary, var(--foreground));
		margin: 0 0 12px 0;
	}

	.auto-runs-empty {
		font-size: var(--text-sm, 0.75rem);
		color: var(--text-tertiary, var(--muted-foreground));
		text-align: center;
		padding: 24px;
	}

	.auto-runs-list {
		display: flex;
		flex-direction: column;
		gap: 4px;
	}

	.auto-run-item {
		border: 1px solid var(--border-subtle, var(--border));
		border-radius: var(--radius-sm, calc(var(--radius, 0.75rem) * 0.5));
		overflow: hidden;
	}

	.auto-run-row {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 8px 12px;
		background: var(--surface-1);
		border: none;
		color: inherit;
		cursor: pointer;
		width: 100%;
		text-align: left;
		font-size: var(--text-sm, 0.75rem);
		transition: background 100ms ease;
	}

	.auto-run-row:hover {
		background: var(--surface-hover, var(--muted));
	}

	.auto-run-meta {
		color: var(--text-secondary, var(--muted-foreground));
		font-size: var(--text-xs, 0.6875rem);
		white-space: nowrap;
	}

	.auto-run-time {
		margin-left: auto;
	}

	.auto-run-expanded {
		padding: 12px;
		border-top: 1px solid var(--border-subtle, var(--border));
		background: var(--surface-0, var(--card));
	}

	.auto-run-response,
	.auto-run-error {
		margin-bottom: 8px;
	}

	.auto-run-pre {
		padding: 8px 12px;
		background: var(--surface-1);
		border: 1px solid var(--border-subtle, var(--border));
		border-radius: var(--radius-sm, calc(var(--radius, 0.75rem) * 0.5));
		font-size: var(--text-xs, 0.6875rem);
		font-family: var(--card-font-mono, monospace);
		white-space: pre-wrap;
		word-break: break-word;
		max-height: 200px;
		overflow-y: auto;
		margin: 4px 0 0 0;
		color: var(--text-primary, var(--foreground));
	}

	.auto-run-pre--error {
		background: color-mix(in oklch, var(--destructive) 10%, transparent);
		border-color: color-mix(in oklch, var(--destructive) 25%, transparent);
		color: var(--destructive);
	}

	.auto-run-details-grid {
		display: flex;
		flex-wrap: wrap;
		gap: 12px;
		font-size: var(--text-xs, 0.6875rem);
		color: var(--text-tertiary, var(--muted-foreground));
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
		.auto-list-item {
			flex-wrap: wrap;
		}

		.auto-item-actions {
			flex: 1 0 100%;
			justify-content: center;
			padding-top: 8px;
			border-top: 1px solid var(--border-subtle, var(--border));
		}

		.auto-budget-grid {
			grid-template-columns: 1fr;
		}

		.cron-builder-grid {
			grid-template-columns: 1fr;
		}

		.cron-summary {
			flex-direction: column;
			align-items: flex-start;
			gap: 4px;
		}

		.auto-stats-row {
			gap: 8px;
		}

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

	.fp-search-bar {
		max-width: 640px;
		width: 100%;
		margin: 0 auto;
	}

	.fp-search-bar :global(.fph-search-wrapper) {
		width: 100%;
	}

	.fp-search-bar :global(.fph-search-input) {
		width: 100% !important;
	}

	.btn-label {
		display: inline;
	}
</style>
