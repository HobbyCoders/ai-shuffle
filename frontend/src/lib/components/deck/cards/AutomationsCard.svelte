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
		ActionStep,
		ActionStepType,
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
		CalendarClock,
		Settings,
		FileText,
		DollarSign,
		GripVertical,
		type IconNode
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
	// Multi-trigger support
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
	let showBudgetSection = $state(false);
	// Action steps form state (expanded types)
	let formActionSteps: ActionStep[] = $state([]);

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

	function getTriggerSummary(trigger: TriggerItem): string {
		switch (trigger.type) {
			case 'cron': return trigger.config.expression ? describeCron(trigger.config.expression) : 'Not configured';
			case 'interval': return trigger.config.seconds ? `Every ${trigger.config.seconds}s` : 'Not configured';
			case 'connector_event': return trigger.config.event_type || 'Not configured';
			case 'connector_trigger': return trigger.config.trigger_id || 'Not configured';
			case 'webhook': return 'Webhook endpoint';
			default: return 'Not configured';
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

	function getTriggerIcon(type: TriggerType) {
		switch (type) {
			case 'cron': return Clock;
			case 'interval': return Timer;
			case 'connector_event': return Radio;
			case 'connector_trigger': return Zap;
			case 'webhook': return Webhook;
			default: return Clock;
		}
	}

	// Cron builder state (scoped to active trigger being edited)
	let cronMode: 'preset' | 'custom' = $state('preset');
	let cronPreset = $state('0 9 * * *');
	let cronMinute = $state('0');
	let cronHour = $state('9');
	let cronDayOfMonth = $state('*');
	let cronMonth = $state('*');
	let cronDayOfWeek = $state('*');

	// Trigger/action helpers
	function addTrigger() {
		formTriggers = [...formTriggers, { type: 'cron', config: { expression: '0 9 * * *' } }];
	}

	function removeTrigger(index: number) {
		if (formTriggers.length <= 1) return;
		formTriggers = formTriggers.filter((_, i) => i !== index);
	}

	function updateTriggerType(index: number, type: TriggerType) {
		const defaultConfig: Record<TriggerType, Record<string, any>> = {
			cron: { expression: '0 9 * * *' },
			interval: { seconds: 3600 },
			connector_event: {},
			connector_trigger: {},
			webhook: {}
		};
		formTriggers[index] = { type, config: defaultConfig[type] };
		formTriggers = [...formTriggers];
	}

	function updateTriggerConfig(index: number, config: Record<string, any>) {
		formTriggers[index] = { ...formTriggers[index], config };
		formTriggers = [...formTriggers];
	}

	function addActionStep(type: ActionStepType = 'connector_action') {
		const defaults: Record<ActionStepType, ActionStep> = {
			connector_action: { type: 'connector_action', connector_id: '', action_id: '', params: {} },
			send_prompt: { type: 'send_prompt', prompt: '' },
			webhook_call: { type: 'webhook_call', url: '', method: 'POST', body: '' },
			delay: { type: 'delay', seconds: 5 }
		};
		formActionSteps = [...formActionSteps, defaults[type]];
	}

	function removeActionStep(index: number) {
		formActionSteps = formActionSteps.filter((_, i) => i !== index);
	}

	function changeActionStepType(index: number, newType: ActionStepType) {
		const defaults: Record<ActionStepType, ActionStep> = {
			connector_action: { type: 'connector_action', connector_id: '', action_id: '', params: {} },
			send_prompt: { type: 'send_prompt', prompt: '' },
			webhook_call: { type: 'webhook_call', url: '', method: 'POST', body: '' },
			delay: { type: 'delay', seconds: 5 }
		};
		formActionSteps[index] = defaults[newType];
		formActionSteps = [...formActionSteps];
	}

	function actionStepTypeLabel(type: ActionStepType): string {
		switch (type) {
			case 'connector_action': return 'Connector Action';
			case 'send_prompt': return 'AI Prompt';
			case 'webhook_call': return 'Webhook Call';
			case 'delay': return 'Delay';
		}
	}

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
				(a.triggers || []).some(t => t.type.toLowerCase().includes(q)) ||
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
		// Load triggers array, falling back to single trigger for backward compat
		if (automation.triggers && automation.triggers.length > 0) {
			formTriggers = automation.triggers.map(t => ({ type: t.type, config: { ...t.config } }));
		} else {
			formTriggers = [{ type: automation.trigger_type, config: { ...automation.trigger_config } }];
		}
		// Sync cron builder for the first cron trigger
		const firstCron = formTriggers.find(t => t.type === 'cron');
		if (firstCron?.config.expression) {
			syncCronFieldsFromExpression(firstCron.config.expression);
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

		// Filter valid action steps based on type
		const validActionSteps = formActionSteps.filter(s => {
			if (s.type === 'connector_action') return s.connector_id && s.action_id;
			if (s.type === 'send_prompt') return s.prompt?.trim();
			if (s.type === 'webhook_call') return s.url?.trim();
			if (s.type === 'delay') return (s.seconds ?? 0) > 0;
			return false;
		});

		// Process triggers — add timezone to cron triggers
		const processedTriggers = formTriggers.map(t => {
			if (t.type === 'cron') {
				return { type: t.type, config: { ...t.config, timezone: timezonePreference.resolveTimezone() } };
			}
			return { ...t };
		});

		return {
			name: formName.trim(),
			description: formDescription.trim() || undefined,
			triggers: processedTriggers,
			// Set trigger_type/trigger_config from first trigger for backward compat
			trigger_type: processedTriggers[0]?.type,
			trigger_config: processedTriggers[0]?.config || {},
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
		const parsed = parseCronExpression(preset);
		if (parsed) {
			cronMinute = parsed.minute;
			cronHour = parsed.hour;
			cronDayOfMonth = parsed.dayOfMonth;
			cronMonth = parsed.month;
			cronDayOfWeek = parsed.dayOfWeek;
		}
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
								{#each (automation.triggers || [{ type: automation.trigger_type }]).slice(0, 3) as trig}
									<span class="card-badge card-badge--muted">{triggerTypeLabel(trig.type)}</span>
								{/each}
								{#if (automation.triggers || []).length > 3}
									<span class="card-badge card-badge--muted">+{(automation.triggers || []).length - 3}</span>
								{/if}
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

		<div class="af-form-body">

			<!-- ═══ SECTION: Basics ═══ -->
			<div class="af-section">
				<button class="af-section-header" onclick={() => toggleSection('basics')}>
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
									bind:value={formName}
									placeholder="e.g. Daily report generator"
								/>
							</div>
							<div class="af-field">
								<label class="af-label" for="auto-description">Description</label>
								<input
									id="auto-description"
									type="text"
									class="af-input"
									bind:value={formDescription}
									placeholder="Optional description..."
								/>
							</div>
						</div>
					</div>
				{/if}
			</div>

			<div class="af-divider"></div>

			<!-- ═══ SECTION: Triggers ═══ -->
			<div class="af-section">
				<button class="af-section-header" onclick={() => toggleSection('triggers')}>
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
						<p class="af-hint">Any trigger firing will run this automation. At least one required.</p>

						{#each formTriggers as trigger, trigIdx}
							<div class="af-item-card" transition:slide={{ duration: 150 }}>
								<div class="af-item-header" role="button" tabindex="0" onclick={() => toggleTriggerExpand(trigIdx)} onkeydown={(e) => { if (e.key === 'Enter' || e.key === ' ') toggleTriggerExpand(trigIdx); }}>
									<div class="af-item-header-left">
										{#if expandedTriggers.has(trigIdx)}
											<ChevronDown size={14} />
										{:else}
											<ChevronRight size={14} />
										{/if}
										<span class="af-item-type-badge">
											<svelte:component this={getTriggerIcon(trigger.type)} size={12} />
											{triggerTypeLabel(trigger.type)}
										</span>
										{#if !expandedTriggers.has(trigIdx)}
											<span class="af-item-summary">{getTriggerSummary(trigger)}</span>
										{/if}
									</div>
									<div class="af-item-header-actions">
										{#if formTriggers.length > 1}
											<button
												class="card-action-btn card-action-btn--destructive"
												onclick={(e) => { e.stopPropagation(); removeTrigger(trigIdx); }}
											>
												<X size={12} />
											</button>
										{/if}
									</div>
								</div>

								{#if expandedTriggers.has(trigIdx)}
									<div class="af-item-body" transition:slide={{ duration: 150 }}>
										<!-- Type selector -->
										<div class="auto-trigger-selector">
											<button class="auto-trigger-btn" class:active={trigger.type === 'cron'}
												onclick={() => updateTriggerType(trigIdx, 'cron')}>
												<Clock size={14} /> Cron
											</button>
											<button class="auto-trigger-btn" class:active={trigger.type === 'interval'}
												onclick={() => updateTriggerType(trigIdx, 'interval')}>
												<Timer size={14} /> Interval
											</button>
											<button class="auto-trigger-btn" class:active={trigger.type === 'connector_event'}
												onclick={() => updateTriggerType(trigIdx, 'connector_event')}>
												<Radio size={14} /> Event
											</button>
											<button class="auto-trigger-btn" class:active={trigger.type === 'connector_trigger'}
												onclick={() => updateTriggerType(trigIdx, 'connector_trigger')}>
												<Zap size={14} /> Connector
											</button>
											<button class="auto-trigger-btn" class:active={trigger.type === 'webhook'}
												onclick={() => updateTriggerType(trigIdx, 'webhook')}>
												<Webhook size={14} /> Webhook
											</button>
										</div>

										<!-- Type-specific config -->
										{#if trigger.type === 'cron'}
											<div class="af-trigger-config">
												<div class="cron-mode-selector">
													<button class="auto-trigger-btn" class:active={cronMode === 'preset'}
														onclick={() => cronMode = 'preset'}>Preset</button>
													<button class="auto-trigger-btn" class:active={cronMode === 'custom'}
														onclick={() => cronMode = 'custom'}>Custom</button>
												</div>
												{#if cronMode === 'preset'}
													<select class="af-input"
														value={trigger.config.expression || '0 9 * * *'}
														onchange={(e) => {
															const val = e.currentTarget.value;
															updateTriggerConfig(trigIdx, { ...trigger.config, expression: val });
															syncCronFieldsFromExpression(val);
														}}>
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
															<label class="af-label">Minute</label>
															<select class="af-input" bind:value={cronMinute}
																onchange={() => { const expr = buildCronFromFields(); updateTriggerConfig(trigIdx, { ...trigger.config, expression: expr }); }}>
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
															<label class="af-label">Hour</label>
															<select class="af-input" bind:value={cronHour}
																onchange={() => { const expr = buildCronFromFields(); updateTriggerConfig(trigIdx, { ...trigger.config, expression: expr }); }}>
																<option value="*">Every hour</option>
																{#each Array.from({length: 24}, (_, i) => i) as h}
																	<option value={String(h)}>{formatHour(h)}</option>
																{/each}
															</select>
														</div>
														<div class="cron-field">
															<label class="af-label">Day</label>
															<select class="af-input" bind:value={cronDayOfMonth}
																onchange={() => { const expr = buildCronFromFields(); updateTriggerConfig(trigIdx, { ...trigger.config, expression: expr }); }}>
																<option value="*">Every day</option>
																{#each Array.from({length: 31}, (_, i) => i + 1) as d}
																	<option value={String(d)}>{d}</option>
																{/each}
															</select>
														</div>
														<div class="cron-field">
															<label class="af-label">Month</label>
															<select class="af-input" bind:value={cronMonth}
																onchange={() => { const expr = buildCronFromFields(); updateTriggerConfig(trigIdx, { ...trigger.config, expression: expr }); }}>
																<option value="*">Every month</option>
																{#each ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'] as m, mi}
																	<option value={String(mi+1)}>{m}</option>
																{/each}
															</select>
														</div>
														<div class="cron-field" style="grid-column: 1 / -1">
															<label class="af-label">Day of Week</label>
															<select class="af-input" bind:value={cronDayOfWeek}
																onchange={() => { const expr = buildCronFromFields(); updateTriggerConfig(trigIdx, { ...trigger.config, expression: expr }); }}>
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
												{#if trigger.config.expression}
													<div class="cron-summary">
														<span class="cron-summary-text">{describeCron(trigger.config.expression)}</span>
														<code class="cron-summary-expr">{trigger.config.expression}</code>
													</div>
												{/if}
												<details class="cron-raw-details">
													<summary class="af-hint" style="cursor: pointer; user-select: none;">Edit raw expression</summary>
													<input type="text" class="af-input af-input--mono" style="margin-top: 6px"
														value={trigger.config.expression || ''}
														oninput={(e) => { updateTriggerConfig(trigIdx, { ...trigger.config, expression: e.currentTarget.value }); syncCronFieldsFromExpression(e.currentTarget.value); }}
														placeholder="0 9 * * *" />
												</details>
												<p class="af-hint" style="margin-top: 8px">Timezone: {timezonePreference.resolveTimezone()}</p>
											</div>
										{:else if trigger.type === 'interval'}
											<div class="af-trigger-config">
												<label class="af-label">Interval (seconds)</label>
												<input type="number" class="af-input" min="60"
													value={trigger.config.seconds || ''}
													oninput={(e) => updateTriggerConfig(trigIdx, { ...trigger.config, seconds: parseInt(e.currentTarget.value) || 0 })}
													placeholder="3600" />
												<p class="af-hint">Minimum 60 seconds</p>
											</div>
										{:else if trigger.type === 'connector_event'}
											<div class="af-trigger-config">
												<label class="af-label">Connector</label>
												<select class="af-input"
													value={trigger.config.connector_id || ''}
													onchange={(e) => updateTriggerConfig(trigIdx, { ...trigger.config, connector_id: e.currentTarget.value })}>
													<option value="">Select a connector...</option>
													{#each connectors.filter(c => c.is_active || c.status === 'connected') as c}
														<option value={c.id}>{c.name} ({c.type})</option>
													{/each}
												</select>
												<label class="af-label" style="margin-top: 12px">Event Type</label>
												<select class="af-input"
													value={trigger.config.event_type || ''}
													onchange={(e) => updateTriggerConfig(trigIdx, { ...trigger.config, event_type: e.currentTarget.value })}>
													<option value="">Select an event...</option>
													<option value="session.complete">Session complete</option>
													<option value="session.error">Session error</option>
													<option value="session.started">Session started</option>
													<option value="permission.requested">Permission requested</option>
													<option value="input.needed">Input needed</option>
												</select>
											</div>
										{:else if trigger.type === 'connector_trigger'}
											<div class="af-trigger-config">
												<label class="af-label">Connector</label>
												<select class="af-input"
													value={trigger.config.connector_id || ''}
													onchange={(e) => updateTriggerConfig(trigIdx, { ...trigger.config, connector_id: e.currentTarget.value, trigger_id: '' })}>
													<option value="">Select a connector...</option>
													{#each connectorsWithTriggers as c}
														<option value={c.id}>{c.name} ({c.type})</option>
													{/each}
												</select>
												{#if trigger.config.connector_id}
													<label class="af-label" style="margin-top: 12px">Trigger</label>
													<select class="af-input"
														value={trigger.config.trigger_id || ''}
														onchange={(e) => updateTriggerConfig(trigIdx, { ...trigger.config, trigger_id: e.currentTarget.value })}>
														<option value="">Select a trigger...</option>
														{#each getTriggersForConnector(trigger.config.connector_id) as trig}
															<option value={trig.id}>{trig.name}</option>
														{/each}
													</select>
												{/if}
											</div>
										{:else if trigger.type === 'webhook'}
											<div class="af-trigger-config">
												{#if currentView === 'edit' && selectedAutomation}
													<label class="af-label">Webhook URL</label>
													<input type="text" class="af-input af-input--mono"
														value={trigger.config.webhook_url || 'Generated after creation'} readonly />
													{#if trigger.config.secret}
														<label class="af-label" style="margin-top: 12px">Secret</label>
														<input type="text" class="af-input af-input--mono"
															value={trigger.config.secret} readonly />
													{/if}
												{:else}
													<p class="af-hint">Webhook URL and secret will be generated after creation.</p>
												{/if}
											</div>
										{/if}
									</div>
								{/if}
							</div>
						{/each}

						<button class="af-add-btn" onclick={addTrigger}>
							<Plus size={14} />
							Add Trigger
						</button>
					</div>
				{/if}
			</div>

			<div class="af-divider"></div>

			<!-- ═══ SECTION: Prompt & Actions ═══ -->
			<div class="af-section">
				<button class="af-section-header" onclick={() => toggleSection('prompt')}>
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
								bind:value={formPrompt}
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
								<div class="af-item-header" role="button" tabindex="0" onclick={() => toggleActionExpand(i)} onkeydown={(e) => { if (e.key === 'Enter' || e.key === ' ') toggleActionExpand(i); }}>
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
												onchange={(e) => { step.connector_id = e.currentTarget.value; step.action_id = ''; step.params = {}; formActionSteps = [...formActionSteps]; }}>
												<option value="">Select connector...</option>
												{#each connectorsWithActions as c}
													<option value={c.id}>{c.name} ({c.type})</option>
												{/each}
											</select>
											{#if step.connector_id}
												<select class="af-input" style="margin-top: 8px"
													value={step.action_id || ''}
													onchange={(e) => { step.action_id = e.currentTarget.value; step.params = {}; formActionSteps = [...formActionSteps]; }}>
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
																<label class="af-label" style="font-size: 11px;">{key}</label>
																<input type="text" class="af-input" style="font-size: 12px;"
																	value={(step.params || {})[key] || ''}
																	oninput={(e) => { if (!step.params) step.params = {}; step.params[key] = e.currentTarget.value; formActionSteps = [...formActionSteps]; }}
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
												oninput={(e) => { step.prompt = e.currentTarget.value; formActionSteps = [...formActionSteps]; }}></textarea>
											<div class="af-field-grid" style="margin-top: 8px;">
												<div class="af-field">
													<label class="af-label" style="font-size: 11px;">Profile (optional)</label>
													<select class="af-input" style="font-size: 12px;"
														value={step.profile_id || ''}
														onchange={(e) => { step.profile_id = e.currentTarget.value || undefined; formActionSteps = [...formActionSteps]; }}>
														<option value="">Default (automation profile)</option>
														{#each profiles as profile}
															<option value={profile.id}>{profile.name}</option>
														{/each}
													</select>
												</div>
												<div class="af-field">
													<label class="af-label" style="font-size: 11px;">Max turns</label>
													<input type="number" class="af-input" style="font-size: 12px;"
														min="1" max="25"
														value={step.max_turns ?? 1}
														oninput={(e) => { step.max_turns = parseInt(e.currentTarget.value) || 1; formActionSteps = [...formActionSteps]; }} />
												</div>
											</div>
											<p class="af-hint" style="margin-top: 4px;">Use {'{{response}}'} to reference previous AI output</p>
										{:else if step.type === 'webhook_call'}
											<input type="url" class="af-input" style="margin-top: 8px"
												placeholder="https://example.com/webhook"
												value={step.url || ''}
												oninput={(e) => { step.url = e.currentTarget.value; formActionSteps = [...formActionSteps]; }} />
											<select class="af-input" style="margin-top: 8px"
												value={step.method || 'POST'}
												onchange={(e) => { step.method = e.currentTarget.value; formActionSteps = [...formActionSteps]; }}>
												<option value="POST">POST</option>
												<option value="GET">GET</option>
												<option value="PUT">PUT</option>
												<option value="PATCH">PATCH</option>
												<option value="DELETE">DELETE</option>
											</select>
											<textarea class="af-input af-textarea" style="margin-top: 8px; min-height: 60px"
												placeholder="Request body (supports template variables)..."
												value={step.body || ''}
												oninput={(e) => { step.body = e.currentTarget.value; formActionSteps = [...formActionSteps]; }}></textarea>
											<p class="af-hint" style="margin-top: 4px;">Use {'{{response}}'} or {'{{trigger.data.field}}'} for template variables</p>
										{:else if step.type === 'delay'}
											<div style="margin-top: 8px;">
												<label class="af-label" style="font-size: 11px;">Seconds to wait</label>
												<input type="number" class="af-input" min="1" max="300"
													placeholder="5"
													value={step.seconds ?? 5}
													oninput={(e) => { step.seconds = parseInt(e.currentTarget.value) || 0; formActionSteps = [...formActionSteps]; }} />
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

			<!-- ═══ SECTION: Configuration ═══ -->
			<div class="af-section">
				<button class="af-section-header" onclick={() => toggleSection('config')}>
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
								<select id="auto-profile" class="af-input" bind:value={formProfileId}>
									<option value="">Select a profile...</option>
									{#each profiles as profile}
										<option value={profile.id}>{profile.name}</option>
									{/each}
								</select>
							</div>
							<div class="af-field">
								<label class="af-label" for="auto-project">Project</label>
								<select id="auto-project" class="af-input" bind:value={formProjectId}>
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
									onclick={() => { formOutputType = 'session'; formOutputConfig = {}; }}>Session</button>
								<button class="auto-trigger-btn" class:active={formOutputType === 'connector'}
									onclick={() => { formOutputType = 'connector'; formOutputConfig = {}; }}>Connector</button>
								<button class="auto-trigger-btn" class:active={formOutputType === 'webhook'}
									onclick={() => { formOutputType = 'webhook'; formOutputConfig = {}; }}>Webhook</button>
								<button class="auto-trigger-btn" class:active={formOutputType === 'none'}
									onclick={() => { formOutputType = 'none'; formOutputConfig = {}; }}>None</button>
							</div>
						</div>

						{#if formOutputType === 'connector'}
							<div class="af-field-grid" transition:slide={{ duration: 150 }}>
								<div class="af-field">
									<label class="af-label" for="auto-output-connector">Connector</label>
									<select id="auto-output-connector" class="af-input" bind:value={formOutputConfig.connector_id}>
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
										bind:value={formOutputConfig.platform_chat_id}
										placeholder="chat / channel ID / email" />
								</div>
							</div>
						{:else if formOutputType === 'webhook'}
							<div class="af-field" transition:slide={{ duration: 150 }}>
								<label class="af-label" for="auto-output-webhook-url">Webhook URL</label>
								<input id="auto-output-webhook-url" type="text" class="af-input"
									bind:value={formOutputConfig.url}
									placeholder="https://example.com/webhook" />
							</div>
						{/if}

						<!-- Approval toggle -->
						<label class="af-toggle-row">
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
				{/if}
			</div>

			<div class="af-divider"></div>

			<!-- ═══ SECTION: Budget & Limits ═══ -->
			<div class="af-section">
				<button class="af-section-header" onclick={() => toggleSection('budget')}>
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
								<input id="auto-max-cost-run" type="number" class="af-input" step="0.01" min="0" bind:value={formMaxCostPerRun} placeholder="No limit" />
							</div>
							<div class="af-field">
								<label class="af-label" for="auto-max-cost-day">Max cost/day ($)</label>
								<input id="auto-max-cost-day" type="number" class="af-input" step="0.01" min="0" bind:value={formMaxCostPerDay} placeholder="No limit" />
							</div>
							<div class="af-field">
								<label class="af-label" for="auto-max-cost-total">Max total ($)</label>
								<input id="auto-max-cost-total" type="number" class="af-input" step="0.01" min="0" bind:value={formMaxCostTotal} placeholder="No limit" />
							</div>
							<div class="af-field">
								<label class="af-label" for="auto-max-runs-day">Max runs/day</label>
								<input id="auto-max-runs-day" type="number" class="af-input" min="1" bind:value={formMaxRunsPerDay} placeholder="No limit" />
							</div>
							<div class="af-field">
								<label class="af-label" for="auto-max-turns">Max turns</label>
								<input id="auto-max-turns" type="number" class="af-input" min="1" bind:value={formMaxTurns} />
							</div>
							<div class="af-field">
								<label class="af-label" for="auto-timeout">Timeout (s)</label>
								<input id="auto-timeout" type="number" class="af-input" min="10" bind:value={formTimeoutSeconds} />
							</div>
						</div>
					</div>
				{/if}
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
						{#each (selectedAutomation.triggers || [{ type: selectedAutomation.trigger_type }]) as trig}
							<span class="card-badge card-badge--muted">{triggerTypeLabel(trig.type)}</span>
						{/each}
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

	.af-form-body {
		flex: 1;
		overflow-y: auto;
		padding: 24px 20px;
		display: flex;
		flex-direction: column;
		gap: 0;
	}

	/* ─── Sections ─── */
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

	/* ─── Fields ─── */
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

	.af-input--mono {
		font-family: var(--card-font-mono, 'JetBrains Mono', monospace);
		font-size: var(--text-sm, 0.75rem);
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

	/* ─── Item Cards (Triggers & Actions) ─── */
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

	.af-trigger-config {
		display: flex;
		flex-direction: column;
		gap: 8px;
	}

	/* ─── Add Button ─── */
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

	/* ─── Toggle Row ─── */
	.af-toggle-row {
		display: flex;
		align-items: center;
		gap: 10px;
		cursor: pointer;
		margin-top: 4px;
	}

	/* Trigger selector (shared) */
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
		gap: 4px;
	}

	.cron-summary {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 8px;
		margin-top: 10px;
		padding: 8px 12px;
		background: color-mix(in oklch, var(--primary) 5%, transparent);
		border: 1px solid color-mix(in oklch, var(--primary) 15%, transparent);
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
