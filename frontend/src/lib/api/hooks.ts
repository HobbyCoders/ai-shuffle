/**
 * Hooks API types and functions
 */

import { api } from './client';

// ============================================================================
// Hook Types
// ============================================================================

/**
 * Valid hook event types matching Claude Code events
 */
export const HOOK_EVENT_TYPES = [
	'PreToolUse',
	'PostToolUse',
	'Stop',
	'SessionStart',
	'SessionEnd',
	'UserPromptSubmit',
	'PreCompact',
	'SubagentStop',
	'Notification'
] as const;

export type HookEventType = (typeof HOOK_EVENT_TYPES)[number];

export type HookType = 'bash' | 'webhook' | 'message';

export interface Hook {
	id: string;
	project_id: string | null;
	name: string;
	event: HookEventType;
	hook_type: HookType;
	command: string | null;
	timeout: number;
	url: string | null;
	secret: string | null;
	message_template: string | null;
	enabled: boolean;
	tool_matcher: string | null;
	created_at: string;
	updated_at: string;
	last_triggered_at: string | null;
	failure_count: number;
}

export interface HookCreate {
	name: string;
	event: HookEventType;
	hook_type: HookType;
	command?: string;
	timeout?: number;
	url?: string;
	secret?: string;
	message_template?: string;
	enabled?: boolean;
	tool_matcher?: string;
}

export interface HookUpdate {
	name?: string;
	event?: HookEventType;
	hook_type?: HookType;
	command?: string | null;
	timeout?: number;
	url?: string | null;
	secret?: string | null;
	message_template?: string | null;
	enabled?: boolean;
	tool_matcher?: string | null;
}

export interface HookTestResult {
	success: boolean;
	duration_ms: number | null;
	output: string | null;
	error: string | null;
}

export interface PluginHookInfo {
	plugin_id: string;
	plugin_name: string;
	event: string;
	hook_type: string;
	description: string | null;
	tool_matcher: string | null;
}

// ============================================================================
// Hook API Functions
// ============================================================================

/**
 * Get all hooks for a project (includes app-level hooks)
 */
export async function getProjectHooks(projectId: string): Promise<Hook[]> {
	return api.get<Hook[]>(`/hooks/project/${projectId}`);
}

/**
 * Get all app-level hooks (not scoped to any project)
 */
export async function getAppHooks(): Promise<Hook[]> {
	return api.get<Hook[]>('/hooks/app');
}

/**
 * Create a new hook for a project
 */
export async function createHook(projectId: string, data: HookCreate): Promise<Hook> {
	return api.post<Hook>(`/hooks/project/${projectId}`, data);
}

/**
 * Create a new app-level hook (fires for all sessions)
 */
export async function createAppHook(data: HookCreate): Promise<Hook> {
	return api.post<Hook>('/hooks/app', data);
}

/**
 * Update a hook
 */
export async function updateHook(hookId: string, data: HookUpdate): Promise<Hook> {
	return api.patch<Hook>(`/hooks/${hookId}`, data);
}

/**
 * Delete a hook
 */
export async function deleteHook(hookId: string): Promise<void> {
	await api.delete(`/hooks/${hookId}`);
}

/**
 * Test a hook with sample event data
 */
export async function testHook(hookId: string): Promise<HookTestResult> {
	return api.post<HookTestResult>(`/hooks/${hookId}/test`);
}

/**
 * Get all hooks contributed by globally installed plugins (read-only)
 */
export async function getPluginHooks(): Promise<PluginHookInfo[]> {
	return api.get<PluginHookInfo[]>('/hooks/plugins/all');
}

/**
 * Get all hooks contributed by project-installed plugins (read-only)
 */
export async function getProjectPluginHooks(projectId: string): Promise<PluginHookInfo[]> {
	return api.get<PluginHookInfo[]>(`/projects/${projectId}/plugins/hooks`);
}
