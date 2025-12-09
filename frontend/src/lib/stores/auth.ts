/**
 * Authentication store
 */

import { writable, derived } from 'svelte/store';
import type { AuthStatus } from '$lib/api/client';
import { getAuthStatus, login as apiLogin, loginWithApiKey as apiLoginWithApiKey, registerApiUser as apiRegisterApiUser, loginApiUser as apiLoginApiUser, logout as apiLogout, setup as apiSetup } from '$lib/api/auth';

interface AuthState {
	status: AuthStatus | null;
	loading: boolean;
	error: string | null;
}

function createAuthStore() {
	const { subscribe, set, update } = writable<AuthState>({
		status: null,
		loading: true,
		error: null
	});

	return {
		subscribe,

		async checkAuth() {
			update(s => ({ ...s, loading: true, error: null }));
			try {
				const status = await getAuthStatus();
				update(s => ({ ...s, status, loading: false }));
				return status;
			} catch (e) {
				update(s => ({ ...s, loading: false, error: 'Failed to check auth status' }));
				throw e;
			}
		},

		async setup(username: string, password: string) {
			update(s => ({ ...s, loading: true, error: null }));
			try {
				await apiSetup(username, password);
				const status = await getAuthStatus();
				update(s => ({ ...s, status, loading: false }));
			} catch (e: any) {
				update(s => ({ ...s, loading: false, error: e.detail || 'Setup failed' }));
				throw e;
			}
		},

		async login(username: string, password: string) {
			update(s => ({ ...s, loading: true, error: null }));
			try {
				await apiLogin(username, password);
				const status = await getAuthStatus();
				update(s => ({ ...s, status, loading: false }));
			} catch (e: any) {
				update(s => ({ ...s, loading: false, error: e.detail || 'Login failed' }));
				throw e;
			}
		},

		async loginWithApiKey(apiKey: string) {
			update(s => ({ ...s, loading: true, error: null }));
			try {
				await apiLoginWithApiKey(apiKey);
				const status = await getAuthStatus();
				update(s => ({ ...s, status, loading: false }));
			} catch (e: any) {
				update(s => ({ ...s, loading: false, error: e.detail || 'API key login failed' }));
				throw e;
			}
		},

		async registerApiUser(apiKey: string, username: string, password: string) {
			update(s => ({ ...s, loading: true, error: null }));
			try {
				await apiRegisterApiUser(apiKey, username, password);
				const status = await getAuthStatus();
				update(s => ({ ...s, status, loading: false }));
			} catch (e: any) {
				update(s => ({ ...s, loading: false, error: e.detail || 'Registration failed' }));
				throw e;
			}
		},

		async loginApiUser(username: string, password: string) {
			update(s => ({ ...s, loading: true, error: null }));
			try {
				await apiLoginApiUser(username, password);
				const status = await getAuthStatus();
				update(s => ({ ...s, status, loading: false }));
			} catch (e: any) {
				update(s => ({ ...s, loading: false, error: e.detail || 'Login failed' }));
				throw e;
			}
		},

		async logout() {
			update(s => ({ ...s, loading: true, error: null }));
			try {
				await apiLogout();
				const status = await getAuthStatus();
				update(s => ({ ...s, status, loading: false }));
			} catch (e: any) {
				update(s => ({ ...s, loading: false, error: e.detail || 'Logout failed' }));
				throw e;
			}
		},

		clearError() {
			update(s => ({ ...s, error: null }));
		}
	};
}

export const auth = createAuthStore();

// Derived stores for convenience
export const isAuthenticated = derived(auth, $auth => $auth.status?.authenticated ?? false);
export const isAdmin = derived(auth, $auth => $auth.status?.is_admin ?? false);
export const setupRequired = derived(auth, $auth => $auth.status?.setup_required ?? false);
export const claudeAuthenticated = derived(auth, $auth => $auth.status?.claude_authenticated ?? false);
export const githubAuthenticated = derived(auth, $auth => $auth.status?.github_authenticated ?? false);
export const username = derived(auth, $auth => $auth.status?.username ?? null);
export const apiUser = derived(auth, $auth => $auth.status?.api_user ?? null);
export const authLoading = derived(auth, $auth => $auth.loading);
export const authError = derived(auth, $auth => $auth.error);
