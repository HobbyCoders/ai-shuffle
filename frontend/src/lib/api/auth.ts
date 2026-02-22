/**
 * Authentication API functions
 */

import { api, type AuthStatus } from './client';

export async function getAuthStatus(): Promise<AuthStatus> {
	return api.get<AuthStatus>('/auth/status');
}

export async function setup(username: string, password: string): Promise<void> {
	await api.post('/auth/setup', { username, password });
}

export async function login(username: string, password: string): Promise<void> {
	await api.post('/auth/login', { username, password });
}

export async function logout(): Promise<void> {
	await api.post('/auth/logout');
}

export async function changePassword(currentPassword: string, newPassword: string): Promise<{
	status: string;
	message: string;
}> {
	return api.post('/auth/change-password', { current_password: currentPassword, new_password: newPassword });
}

export async function getClaudeAuthStatus(): Promise<{
	authenticated: boolean;
	config_dir: string;
}> {
	return api.get('/auth/claude/status');
}

export async function getClaudeLoginInstructions(): Promise<{
	status: string;
	message: string;
	instructions?: string[];
	command?: string;
}> {
	return api.get('/auth/claude/login-instructions');
}

// Claude OAuth flow
export async function startClaudeLogin(forceReauth: boolean = false): Promise<{
	success: boolean;
	oauth_url?: string;
	state?: string;
	already_authenticated?: boolean;
	message?: string;
	requires_code?: boolean;
	error?: string;
}> {
	return api.post('/auth/claude/login', { force_reauth: forceReauth });
}

export async function completeClaudeLogin(code: string, state: string): Promise<{
	success: boolean;
	message: string;
	error?: string;
}> {
	return api.post('/auth/claude/complete', { code, state });
}

export async function pollClaudeLogin(): Promise<{
	success: boolean;
	authenticated: boolean;
	message: string;
}> {
	return api.get('/auth/claude/login/poll');
}

// GitHub auth
export async function startGithubWebLogin(): Promise<{
	success: boolean;
	user_code?: string;
	verification_url?: string;
	error?: string;
	install_url?: string;
}> {
	return api.post('/auth/github/login/web');
}

export async function loginGithubWithToken(token: string): Promise<{
	success: boolean;
	message: string;
	error?: string;
}> {
	return api.post('/auth/github/login', { token });
}

export async function getGithubAuthStatus(): Promise<{
	authenticated: boolean;
	user: string | null;
	config_dir: string;
}> {
	return api.get('/auth/github/status');
}

// Onboarding
export async function completeOnboarding(): Promise<{
	status: string;
	message: string;
}> {
	return api.post('/auth/onboarding/complete');
}

// Workspace configuration (for local mode setup)
export async function getWorkspaceConfig(): Promise<import('./client').WorkspaceConfig> {
	return api.get('/workspace/config');
}

export async function setWorkspaceConfig(workspace_path: string): Promise<import('./client').WorkspaceConfig> {
	return api.post('/workspace/config', { workspace_path });
}

export async function validateWorkspacePath(workspace_path: string): Promise<import('./client').WorkspaceValidation> {
	return api.post('/workspace/validate', { workspace_path });
}

export interface LoginResponse {
	status: string;
	message: string;
	is_admin: boolean;
	// FastAPI returns errors in 'detail' field
	detail?: string;
}

export interface AuditLogEntry {
	id: string;
	user_id: string | null;
	user_type: string;
	event_type: string;
	ip_address: string | null;
	user_agent: string | null;
	details: Record<string, any> | null;
	created_at: string;
}

export interface AuditLogResponse {
	entries: AuditLogEntry[];
	total: number;
	limit: number;
	offset: number;
}

export async function getAuditLog(options: {
	event_type?: string;
	limit?: number;
	offset?: number;
} = {}): Promise<AuditLogResponse> {
	const params = new URLSearchParams();
	if (options.event_type) params.set('event_type', options.event_type);
	if (options.limit) params.set('limit', options.limit.toString());
	if (options.offset) params.set('offset', options.offset.toString());
	const query = params.toString();
	return api.get(`/security/audit-log${query ? `?${query}` : ''}`);
}
