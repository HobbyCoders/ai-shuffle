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
