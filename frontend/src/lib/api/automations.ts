/**
 * API client for Automations - scheduled and event-driven AI task execution
 */

import { api } from './client';
import { apiOrigin } from './runtime';
import { encodeProviderModel, getModelLabel } from '$lib/constants/models';

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
	 * runs use the agent's saved model, then the app default.
	 */
	model?: string | null;
	provider_id?: string | null;
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
}

/**
 * Where a run's model comes from: the automation's (or step's) own choice, or
 * the fallback for automations without one (the agent's saved model, then the
 * app default).
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
	profile_id: string;
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
	/** Advice from create/update, e.g. use an API key for scheduled runs. */
	notices?: string[];
}

/**
 * Short label for the model an automation's runs use, e.g. `Opus` for its own
 * choice, `Sonnet (agent)` / `Sonnet (default)` for the fallback, and
 * `Model unavailable` when the stored choice is gone.
 */
export function automationModelLabel(automation: Pick<
	Automation,
	'model' | 'effective_model' | 'effective_provider_id' | 'model_source' | 'model_error'
>): string {
	if (automation.model_error) return 'Model unavailable';
	if (automation.model) return getModelLabel(automation.model);
	const effective = automation.effective_model;
	const name = effective
		? getModelLabel(
				automation.effective_provider_id
					? encodeProviderModel(automation.effective_provider_id, effective)
					: effective
			)
		: 'Provider default';
	if (automation.model_source === 'agent') return `${name} (agent)`;
	if (automation.model_source === 'default') return `${name} (default)`;
	return name;
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
	status: 'pending' | 'approved' | 'running' | 'completed' | 'failed' | 'cancelled' | 'budget_exceeded';
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
