/**
 * API client for Automations - scheduled and event-driven AI task execution
 */

import { api } from './client';
import { apiOrigin } from './runtime';
import { encodeProviderModel, getModelLabel, getProviders } from '$lib/constants/models';
import { accessModes, type AccessMode } from '$lib/components/chat/session-scope';

// ============================================================================
// Types
// ============================================================================

export type TriggerType = 'cron' | 'interval' | 'connector_event' | 'connector_trigger' | 'webhook';
export type OutputType = 'connector' | 'webhook' | 'session' | 'none';
export type ActionStepType = 'connector_action' | 'send_prompt' | 'webhook_call' | 'delay';

/**
 * Per-type trigger settings, as read by app/core/automation_manager.py and
 * app/core/automation_service.py. Unlisted keys pass through untouched.
 */
export interface TriggerConfig {
	/** cron */
	expression?: string;
	timezone?: string;
	/** interval */
	seconds?: number;
	/** connector_event / connector_trigger */
	connector_id?: string;
	event_type?: string;
	filter?: Record<string, unknown>;
	trigger_id?: string;
	/** webhook (generated server-side when absent) */
	secret?: string;
	[key: string]: unknown;
}

export interface TriggerItem {
	type: TriggerType;
	config: TriggerConfig;
}

export interface ActionStep {
	type: ActionStepType;
	// connector_action fields
	connector_id?: string;
	action_id?: string;
	params?: Record<string, unknown>;
	// send_prompt fields
	prompt?: string;
	profile_id?: string;
	/** Model for this step (picker value); omitted = the automation's model. */
	model?: string | null;
	provider_id?: string | null;
	max_turns?: number;
	// webhook_call fields
	url?: string;
	method?: string;
	headers?: Record<string, string>;
	body?: string;
	// delay fields
	seconds?: number;
}

export interface AutomationCreate {
	name: string;
	description?: string | null;
	trigger_type?: TriggerType;
	trigger_config?: TriggerConfig;
	triggers?: TriggerItem[];
	prompt?: string | null;
	profile_id: string;
	project_id?: string | null;
	output_config?: Record<string, unknown>;
	action_steps?: ActionStep[] | null;
	max_turns?: number | null;
	timeout_seconds?: number | null;
	max_cost_per_run?: number | null;
	max_cost_per_day?: number | null;
	max_cost_total?: number | null;
	max_runs_per_day?: number | null;
	requires_approval?: boolean;
	/**
	 * Model its runs use: the model picker's value (a Claude id such as
	 * `sonnet`, or `prov:<providerId>:<modelId>`). null = no choice of its own;
	 * runs use the app default, like a new chat with no pick.
	 */
	model?: string | null;
	provider_id?: string | null;
	/** Mode each run starts in; omitted = `bypassPermissions` (see DEFAULT_AUTOMATION_PERMISSION_MODE). */
	permission_mode?: AutomationPermissionMode;
}

export interface AutomationUpdate {
	name?: string;
	description?: string | null;
	trigger_type?: TriggerType;
	trigger_config?: TriggerConfig;
	triggers?: TriggerItem[];
	prompt?: string | null;
	profile_id?: string;
	project_id?: string | null;
	output_config?: Record<string, unknown>;
	max_turns?: number | null;
	timeout_seconds?: number | null;
	max_cost_per_run?: number | null;
	max_cost_per_day?: number | null;
	max_cost_total?: number | null;
	max_runs_per_day?: number | null;
	action_steps?: ActionStep[] | null;
	requires_approval?: boolean;
	is_enabled?: boolean;
	/** Same shape as on create; send null to clear the choice. */
	model?: string | null;
	provider_id?: string | null;
	/** Omitted = unchanged. */
	permission_mode?: AutomationPermissionMode;
}

/** The composer's permission modes (`AccessMode` in components/chat/session-scope.ts). */
export type AutomationPermissionMode = AccessMode['value'];

/**
 * Where a run's permission mode comes from: the automation's own choice, the
 * agent's legacy saved mode (an automation saved before the choice existed),
 * or `default` (no mode anywhere: Ask).
 */
export type AutomationPermissionSource = 'automation' | 'agent' | 'default';

/**
 * A new automation's mode unless the owner picks another. Nobody watches an
 * automation, so in Ask every tool that needs approval waits on a card nobody
 * answers and is denied. Mirrors DEFAULT_AUTOMATION_PERMISSION_MODE in
 * app/core/automation_service.py.
 */
export const DEFAULT_AUTOMATION_PERMISSION_MODE: AutomationPermissionMode = 'bypassPermissions';

const PERMISSION_MODE_VALUES: readonly AutomationPermissionMode[] = ['default', 'auto', 'acceptEdits', 'bypassPermissions'];

function knownMode(value: string | null | undefined): AutomationPermissionMode | null {
	if (value === 'plan') return 'default';
	return PERMISSION_MODE_VALUES.includes(value as AutomationPermissionMode) ? (value as AutomationPermissionMode) : null;
}

/**
 * The mode the form starts on. A new automation: Bypass. A saved one: its own
 * mode, else (saved before the choice existed) the mode its runs use today —
 * the server's report, else the agent's legacy saved mode, else Ask.
 */
export function formPermissionMode(
	saved: Pick<Automation, 'permission_mode' | 'effective_permission_mode'> | null | undefined,
	agentConfig?: { permission_mode?: string | null } | null
): AutomationPermissionMode {
	if (!saved) return DEFAULT_AUTOMATION_PERMISSION_MODE;
	return (
		knownMode(saved.permission_mode) ??
		knownMode(saved.effective_permission_mode) ??
		knownMode(agentConfig?.permission_mode) ??
		'default'
	);
}

/**
 * The `permission_mode` a save sends. A new automation always sends its mode.
 * An automation saved before the choice existed keeps NULL (its legacy
 * fallback) until the owner picks a mode other than the one it shows; after
 * that, and on every automation with a mode of its own, the form's value.
 * Switching the agent pins the shown mode (`agentChanged`): left NULL, the run
 * would follow the NEW agent's legacy mode, not the one the form displayed.
 */
export function permissionModePayload(
	saved: Pick<Automation, 'permission_mode' | 'effective_permission_mode'> | null | undefined,
	value: AutomationPermissionMode,
	agentConfig?: { permission_mode?: string | null } | null,
	agentChanged = false
): AutomationPermissionMode | undefined {
	if (!saved || saved.permission_mode || agentChanged) return value;
	return value === formPermissionMode(saved, agentConfig) ? undefined : value;
}

/** Composer label for a mode, e.g. `Bypass`; unknown values read as Ask. */
export function permissionModeLabel(mode: string | null | undefined): string {
	const value = knownMode(mode) ?? 'default';
	return accessModes({ hasProject: true, autoSupported: true }).find((m) => m.value === value)?.label ?? 'Ask';
}

/**
 * The detail view's line for the mode a run starts in, e.g. `Bypass`, or
 * `Ask (saved on agent)` for an automation that still uses a legacy mode.
 */
export function automationPermissionLabel(
	automation: Pick<Automation, 'permission_mode' | 'effective_permission_mode' | 'permission_mode_source'>
): string {
	if (automation.permission_mode) return permissionModeLabel(automation.permission_mode);
	const label = permissionModeLabel(automation.effective_permission_mode);
	return automation.permission_mode_source === 'agent' ? `${label} (saved on agent)` : label;
}

/**
 * Where a run's model comes from: the automation's (or step's) own choice,
 * `default` (the app default), or `agent` when the agent still carries a model
 * saved by an older version, which runs without a choice keep using.
 */
export type AutomationModelSource = 'automation' | 'step' | 'agent' | 'default';

export interface Automation {
	id: string;
	name: string;
	description: string | null;
	trigger_type: TriggerType;
	trigger_config: TriggerConfig;
	triggers: TriggerItem[] | null;
	prompt: string | null;
	/** null once its agent was deleted: the automation is disabled until another agent is picked. */
	profile_id: string | null;
	project_id: string | null;
	output_config: Record<string, unknown>;
	action_steps: ActionStep[] | null;
	max_turns: number | null;
	timeout_seconds: number | null;
	max_cost_per_run: number | null;
	max_cost_per_day: number | null;
	max_cost_total: number | null;
	max_runs_per_day: number | null;
	total_cost_used: number;
	is_enabled: boolean;
	status: 'idle' | 'running' | 'paused' | 'error';
	last_run_at: string | null;
	last_error: string | null;
	run_count: number;
	failure_count: number;
	requires_approval: boolean;
	created_at: string;
	updated_at: string;
	/** Stored model choice (picker value) and the provider it binds. */
	model?: string | null;
	provider_id?: string | null;
	/** The model a run would use now; null on a provider whose default is unset. */
	effective_model?: string | null;
	effective_provider_id?: string | null;
	model_source?: AutomationModelSource | null;
	/** Set when the stored choice is no longer available; runs fail with it. */
	model_error?: string | null;
	/**
	 * Stored permission mode; null on an automation saved before the choice
	 * existed, whose runs use the agent's legacy saved mode, else Ask.
	 */
	permission_mode?: AutomationPermissionMode | null;
	/** The mode a run starts in right now, and where it comes from. */
	effective_permission_mode?: AutomationPermissionMode | null;
	permission_mode_source?: AutomationPermissionSource | null;
	/** Advice from create/update, e.g. use an API key for scheduled runs. */
	notices?: string[];
}

function modelName(model: string | null | undefined, providerId: string | null | undefined): string {
	if (!model) return 'Provider default';
	return getModelLabel(providerId ? encodeProviderModel(providerId, model) : model);
}

/**
 * Short label for the model an automation's runs use, e.g. `Opus` for its own
 * choice, `Sonnet (worker default)` without one (the model the worker
 * reports for unattended runs, not the chat default), `GPT-5 (saved on agent)` when
 * the agent still carries a legacy model, and `Model unavailable` when the
 * stored choice is gone.
 */
export function automationModelLabel(automation: Pick<
	Automation,
	'model' | 'effective_model' | 'effective_provider_id' | 'model_source' | 'model_error'
>): string {
	if (automation.model_error) return 'Model unavailable';
	if (automation.model) return getModelLabel(automation.model);
	const name = modelName(automation.effective_model, automation.effective_provider_id);
	if (automation.model_source === 'agent') return `${name} (saved on agent)`;
	if (automation.model_source === 'default') {
		return automation.effective_model ? `${name} (worker default)` : 'Worker default';
	}
	return name;
}

/** What runs use when an automation has no model of its own. */
export interface AutomationFallbackModel {
	/**
	 * Display name of the model, e.g. `Sonnet`. Null when the run takes the
	 * worker's unattended default and the worker has not said which model that
	 * is (a form not saved yet): say so without naming a model.
	 */
	label: string | null;
	/** True when it is a legacy model saved on the agent, not the app default. */
	legacy: boolean;
}

/**
 * The model a run without its own choice uses, read from the agent the form
 * has selected. Mirrors `resolve_automation_model` in
 * app/core/automation_service.py: a legacy model or provider still saved on
 * the agent, else the worker's default for unattended runs. That default is
 * not the chat default (Claude's default when Claude is connected, else the
 * newest usable provider's), and only the worker knows it, so the label is
 * null until a saved automation reports it (`savedFallbackModel`).
 */
export function automationFallbackModel(
	agentConfig: { model?: string | null; provider?: string | null } | null | undefined
): AutomationFallbackModel {
	const model = agentConfig?.model?.trim() || null;
	const provider = agentConfig?.provider?.trim() || null;
	if (model) return { label: modelName(model, provider), legacy: true };
	if (provider) {
		const fallback = getProviders().find((p) => p.id === provider)?.default_model ?? null;
		return { label: modelName(fallback, provider), legacy: true };
	}
	return { label: null, legacy: false };
}

/** The Model row's hint for a run with no model of its own. */
export function fallbackModelHint(fallback: AutomationFallbackModel): string {
	if (fallback.legacy) return `Uses ${fallback.label ?? 'the provider default'}, saved on this agent (legacy).`;
	return fallback.label
		? `Uses ${fallback.label}, the worker's default for unattended runs.`
		: "Uses the worker's default model for unattended runs.";
}

/** The same answer from a saved automation's server report. */
export function savedFallbackModel(
	automation: Pick<Automation, 'effective_model' | 'effective_provider_id' | 'model_source'>
): AutomationFallbackModel {
	return {
		label: modelName(automation.effective_model, automation.effective_provider_id),
		legacy: automation.model_source === 'agent'
	};
}

/**
 * Where a webhook trigger is called: `POST /api/v1/automations/{id}/trigger`
 * (app/api/automations.py), signed with `X-Webhook-Signature`. The backend
 * has no reliable public base URL, so the address is built from the origin
 * this UI talks to (the paired worker on mobile, the page origin otherwise).
 */
export function automationWebhookUrl(automationId: string): string {
	const origin = apiOrigin() || (typeof window !== 'undefined' ? window.location.origin : '');
	return `${origin}/api/v1/automations/${encodeURIComponent(automationId)}/trigger`;
}

/** Labels for `trigger_data.trigger`, as written by app/core/automation_manager.py. */
const RUN_TRIGGER_LABELS: Record<string, string> = {
	schedule: 'Schedule',
	connector_event: 'Event',
	connector_trigger: 'Connector',
	webhook: 'Webhook',
	manual: 'Manual'
};

/** What started a run. Older rows may carry `source` instead of `trigger`. */
export function runTriggerLabel(triggerData: Record<string, unknown> | null | undefined): string {
	const raw = triggerData?.trigger ?? triggerData?.source;
	if (typeof raw !== 'string' || !raw) return RUN_TRIGGER_LABELS.manual;
	return RUN_TRIGGER_LABELS[raw] ?? raw;
}

export interface AutomationRun {
	id: string;
	automation_id: string;
	status: 'pending' | 'approved' | 'running' | 'completed' | 'failed' | 'cancelled' | 'budget_exceeded' | 'skipped_running';
	trigger_data: Record<string, unknown> | null;
	session_id: string | null;
	prompt_rendered: string | null;
	response: string | null;
	error: string | null;
	cost_usd: number;
	tokens_in: number;
	tokens_out: number;
	duration_ms: number;
	output_status: string;
	output_error: string | null;
	started_at: string | null;
	completed_at: string | null;
	created_at: string;
}

export interface AutomationApproval {
	id: string;
	run_id: string;
	automation_id: string;
	automation_name?: string;
	status: 'pending' | 'approved' | 'denied';
	prompt_preview: string | null;
	decided_by: string | null;
	decided_at: string | null;
	created_at: string;
}

export interface AutomationStats {
	automation_id: string;
	total_runs: number;
	successful_runs: number;
	failed_runs: number;
	total_cost_usd: number;
	avg_duration_ms: number;
	today_runs: number;
	today_cost_usd: number;
}

/** Manual-trigger acknowledgement (automation_manager.trigger_manual). */
export interface AutomationTriggerResult {
	automation_id: string;
	status: 'triggered';
}

export interface AutomationTestResult {
	prompt_rendered: string;
	budget_ok: boolean;
	message: string;
}

// ============================================================================
// API Functions
// ============================================================================

export async function getAutomations(): Promise<Automation[]> {
	return api.get<Automation[]>('/automations');
}

export async function createAutomation(data: AutomationCreate): Promise<Automation> {
	return api.post<Automation>('/automations', data);
}

export async function updateAutomation(id: string, data: AutomationUpdate): Promise<Automation> {
	return api.patch<Automation>(`/automations/${id}`, data);
}

export async function deleteAutomation(id: string): Promise<void> {
	await api.delete(`/automations/${id}`);
}

export async function enableAutomation(id: string): Promise<Automation> {
	return api.post<Automation>(`/automations/${id}/enable`);
}

export async function disableAutomation(id: string): Promise<Automation> {
	return api.post<Automation>(`/automations/${id}/disable`);
}

export async function triggerAutomation(id: string): Promise<AutomationTriggerResult> {
	return api.post<AutomationTriggerResult>(`/automations/${id}/trigger`);
}

export async function getAutomationRuns(id: string, limit = 50, offset = 0): Promise<AutomationRun[]> {
	return api.get<AutomationRun[]>(`/automations/${id}/runs?limit=${limit}&offset=${offset}`);
}

export async function getAutomationStats(id: string): Promise<AutomationStats> {
	return api.get<AutomationStats>(`/automations/${id}/stats`);
}

export async function getPendingApprovals(): Promise<AutomationApproval[]> {
	return api.get<AutomationApproval[]>('/automations/approvals/pending');
}

export async function approveRun(approvalId: string): Promise<AutomationApproval> {
	return api.post<AutomationApproval>(`/automations/approvals/${approvalId}/approve`);
}

export async function denyRun(approvalId: string): Promise<AutomationApproval> {
	return api.post<AutomationApproval>(`/automations/approvals/${approvalId}/deny`);
}
