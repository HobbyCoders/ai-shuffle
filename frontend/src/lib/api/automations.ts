/**
 * API client for Automations - scheduled and event-driven AI task execution
 */

import { api } from './client';

// ============================================================================
// Types
// ============================================================================

export type TriggerType = 'cron' | 'interval' | 'connector_event' | 'connector_trigger' | 'webhook';
export type OutputType = 'connector' | 'webhook' | 'session' | 'none';
export type ActionStepType = 'connector_action' | 'send_prompt' | 'webhook_call' | 'delay';

export interface TriggerItem {
	type: TriggerType;
	config: Record<string, any>;
}

export interface ActionStep {
	type: ActionStepType;
	// connector_action fields
	connector_id?: string;
	action_id?: string;
	params?: Record<string, any>;
	// send_prompt fields
	prompt?: string;
	profile_id?: string;
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
	description?: string;
	trigger_type?: TriggerType;
	trigger_config?: Record<string, any>;
	triggers?: TriggerItem[];
	prompt?: string;
	profile_id: string;
	project_id?: string;
	output_config?: Record<string, any>;
	action_steps?: ActionStep[];
	max_turns?: number | null;
	timeout_seconds?: number | null;
	max_cost_per_run?: number | null;
	max_cost_per_day?: number | null;
	max_cost_total?: number | null;
	max_runs_per_day?: number | null;
	requires_approval?: boolean;
}

export interface AutomationUpdate {
	name?: string;
	description?: string;
	trigger_type?: TriggerType;
	trigger_config?: Record<string, any>;
	triggers?: TriggerItem[];
	prompt?: string;
	profile_id?: string;
	project_id?: string;
	output_config?: Record<string, any>;
	max_turns?: number | null;
	timeout_seconds?: number | null;
	max_cost_per_run?: number | null;
	max_cost_per_day?: number | null;
	max_cost_total?: number | null;
	max_runs_per_day?: number | null;
	action_steps?: ActionStep[];
	requires_approval?: boolean;
	is_enabled?: boolean;
}

export interface Automation {
	id: string;
	name: string;
	description: string | null;
	trigger_type: TriggerType;
	trigger_config: Record<string, any>;
	triggers: TriggerItem[] | null;
	prompt: string | null;
	profile_id: string;
	project_id: string | null;
	output_config: Record<string, any>;
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
}

export interface AutomationRun {
	id: string;
	automation_id: string;
	status: 'pending' | 'approved' | 'running' | 'completed' | 'failed' | 'cancelled' | 'budget_exceeded';
	trigger_data: Record<string, any> | null;
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

export async function getAutomation(id: string): Promise<Automation> {
	return api.get<Automation>(`/automations/${id}`);
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

export async function triggerAutomation(id: string): Promise<any> {
	return api.post<any>(`/automations/${id}/trigger`);
}

export async function testAutomation(id: string): Promise<AutomationTestResult> {
	return api.post<AutomationTestResult>(`/automations/${id}/test`);
}

export async function getAutomationRuns(id: string, limit = 50, offset = 0): Promise<AutomationRun[]> {
	return api.get<AutomationRun[]>(`/automations/${id}/runs?limit=${limit}&offset=${offset}`);
}

export async function getAutomationRun(automationId: string, runId: string): Promise<AutomationRun> {
	return api.get<AutomationRun>(`/automations/${automationId}/runs/${runId}`);
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
