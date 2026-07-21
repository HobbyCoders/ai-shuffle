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

// ---------------------------------------------------------------------------
// Multiple Claude accounts
//
// The app can hold several Claude Code OAuth sign-ins at once and switch which
// one it authenticates with, without re-doing the browser flow each time. The
// list lives server-side, encrypted with the admin password — so it can only be
// read/switched while `encryption_ready` is true (i.e. the admin has logged in
// this session). These client functions use the shared `api` client, which
// throws `{ detail, status }` on non-2xx; callers inspect `.status === 409`
// (encryption locked / can't-delete-active) and surface `.detail`.
// ---------------------------------------------------------------------------

export interface ClaudeAccount {
	id: string;
	name: string;
	email: string;
	subscription_type: string;
	is_active: boolean;
	/** Unix epoch milliseconds, or null when the token has no known expiry. */
	expires_at: number | null;
	has_refresh_token: boolean;
	/** True when the latest refreshed tokens couldn't be persisted (encryption
	 *  was locked during a refresh) — the account still works this session but
	 *  the stored copy is stale until the admin logs in again. */
	sync_pending: boolean;
	/** Unix epoch milliseconds of last use, or null if never used. */
	last_used_at: number | null;
	created_at: number | null;
}

export interface ClaudeAccountsResponse {
	accounts: ClaudeAccount[];
	active_account_id: string | null;
	/** False when the encrypted store can't be opened (admin must re-login). */
	encryption_ready: boolean;
}

export async function listClaudeAccounts(): Promise<ClaudeAccountsResponse> {
	return api.get('/auth/claude/accounts');
}

/** Begin an OAuth PKCE flow that ADDS a new account without disturbing the
 *  currently-active login. Returns the URL to open in the browser + the state
 *  to pass back to `completeAddClaudeAccount`. */
export async function startAddClaudeAccount(): Promise<{
	success: boolean;
	oauth_url?: string;
	state?: string;
	error?: string;
	message?: string;
}> {
	return api.post('/auth/claude/accounts/start');
}

/** Finish adding an account with the pasted browser code. `makeActive` switches
 *  the app to the new account immediately. Throws `{ status: 409, detail }` when
 *  encryption is locked. */
export async function completeAddClaudeAccount(
	code: string,
	state: string,
	makeActive?: boolean
): Promise<{
	success: boolean;
	account: ClaudeAccount;
	active_account_id: string | null;
}> {
	const body: Record<string, unknown> = { code, state };
	if (makeActive !== undefined) body.make_active = makeActive;
	return api.post('/auth/claude/accounts/complete', body);
}

/** Switch the app to authenticate with account `id`. Throws `{ status: 409 }`
 *  when encryption is locked, `{ status: 404 }` for an unknown id. */
export async function activateClaudeAccount(id: string): Promise<{
	success: boolean;
	active_account_id: string | null;
}> {
	return api.post(`/auth/claude/accounts/${id}/activate`);
}

/** Remove an account. Throws `{ status: 409, detail }` when deleting the active
 *  account while others remain ("switch first"). Deleting the sole account logs
 *  the app out of Claude entirely. */
export async function deleteClaudeAccount(id: string): Promise<{ success: boolean } | void> {
	return api.delete<{ success: boolean }>(`/auth/claude/accounts/${id}`);
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
