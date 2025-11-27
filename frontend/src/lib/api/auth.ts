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
