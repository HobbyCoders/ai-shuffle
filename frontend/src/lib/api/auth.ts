/**
 * Authentication API functions
 */

import { api, type AuthStatus, type ApiUserInfo } from './client';

export async function getAuthStatus(): Promise<AuthStatus> {
	return api.get<AuthStatus>('/auth/status');
}

export async function setup(username: string, password: string): Promise<void> {
	await api.post('/auth/setup', { username, password });
}

export async function login(username: string, password: string): Promise<void> {
	await api.post('/auth/login', { username, password });
}

export async function loginWithApiKey(apiKey: string): Promise<{
	status: string;
	message: string;
	is_admin: boolean;
	api_user: ApiUserInfo;
}> {
	return api.post('/auth/login/api-key', { api_key: apiKey });
}

export async function registerApiUser(apiKey: string, username: string, password: string): Promise<{
	status: string;
	message: string;
	is_admin: boolean;
	api_user: ApiUserInfo & { username: string };
}> {
	return api.post('/auth/register/api-user', { api_key: apiKey, username, password });
}

export async function loginApiUser(username: string, password: string): Promise<{
	status: string;
	message: string;
	is_admin: boolean;
	api_user: ApiUserInfo & { username: string };
}> {
	return api.post('/auth/login/api-user', { username, password });
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

// ============================================================================
// Two-Factor Authentication
// ============================================================================

export interface TwoFactorSetupResponse {
	secret: string;
	qr_code: string;
	uri: string;
}

export interface TwoFactorStatusResponse {
	enabled: boolean;
	has_recovery_codes: boolean;
	recovery_codes_count: number;
	verified_at: string | null;
}

export interface RecoveryCodesResponse {
	codes: string[];
	count: number;
}

export interface LoginResponse {
	status: string;
	message: string;
	is_admin: boolean;
	requires_2fa?: boolean;
	pending_2fa_token?: string;
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

export async function get2FAStatus(): Promise<TwoFactorStatusResponse> {
	return api.get('/security/2fa/status');
}

export async function setup2FA(): Promise<TwoFactorSetupResponse> {
	return api.post('/security/2fa/setup');
}

export async function verify2FA(code: string): Promise<{
	status: string;
	message: string;
	recovery_codes: string[];
}> {
	return api.post('/security/2fa/verify', { code });
}

export async function disable2FA(code: string, password: string): Promise<{
	status: string;
	message: string;
}> {
	return api.post('/security/2fa/disable', { code, password });
}

export async function regenerateRecoveryCodes(): Promise<RecoveryCodesResponse> {
	return api.post('/security/2fa/recovery-codes');
}

export async function verify2FALogin(code: string, pendingToken: string, useRecoveryCode: boolean = false): Promise<{
	status: string;
	message: string;
	is_admin: boolean;
}> {
	return api.post('/auth/verify-2fa', {
		code,
		pending_2fa_token: pendingToken,
		use_recovery_code: useRecoveryCode
	});
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
