/**
 * Authentication API functions
 */

import { api, type AuthStatus } from './client';

export async function getAuthStatus(): Promise<AuthStatus> {
	const status = await api.get<AuthStatus>('/auth/status', { cache: 'no-store' });
	if (typeof status?.authenticated !== 'boolean') throw new Error('Invalid authentication status response');
	return status;
}

export async function setupInstallation(): Promise<void> {
	await api.post('/auth/setup', {});
}

export async function logout(): Promise<void> {
	await api.post('/auth/logout');
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

export async function cancelGithubWebLogin(): Promise<{ success: boolean }> {
	return api.post('/auth/github/login/web/cancel');
}

export async function logoutGithub(): Promise<{ success: boolean; error?: string }> {
	return api.post('/auth/github/logout');
}

export async function getGithubAuthStatus(): Promise<{
	authenticated: boolean;
	user: string | null;
	config_dir: string;
	web_login_pending?: boolean;
	web_login_error?: string | null;
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
