/**
 * Authentication store
 *
 * Migrated to Svelte 5 runes.
 */

import type { AuthStatus } from '$lib/api/client';
import { getAuthStatus, login as apiLogin, logout as apiLogout, setup as apiSetup } from '$lib/api/auth';
import { createSubscribers } from './store-utils';

// ============================================
// Types
// ============================================

interface AuthState {
	status: AuthStatus | null;
	loading: boolean;
	error: string | null;
}

// ============================================
// Reactive State
// ============================================

let _state = $state<AuthState>({
	status: null,
	loading: true,
	error: null
});

// ============================================
// Subscribe shim (centralized via store-utils)
// ============================================

const { subscribe, update: _update, createDerived } =
	createSubscribers<AuthState>(
		() => _state,
		(v) => { _state = v; }
	);

// ============================================
// Exported Store
// ============================================

export const auth = {
	get current() {
		return _state;
	},

	subscribe,

	async checkAuth() {
		_update(s => ({ ...s, loading: true, error: null }));
		try {
			const status = await getAuthStatus();
			_update(s => ({ ...s, status, loading: false }));
			return status;
		} catch (e) {
			_update(s => ({ ...s, loading: false, error: 'Failed to check auth status' }));
			throw e;
		}
	},

	async setup(username: string, password: string) {
		_update(s => ({ ...s, loading: true, error: null }));
		try {
			await apiSetup(username, password);
			const status = await getAuthStatus();
			_update(s => ({ ...s, status, loading: false }));
		} catch (e: any) {
			_update(s => ({ ...s, loading: false, error: e.detail || 'Setup failed' }));
			throw e;
		}
	},

	async login(username: string, password: string) {
		_update(s => ({ ...s, loading: true, error: null }));
		try {
			await apiLogin(username, password);
			const status = await getAuthStatus();
			_update(s => ({ ...s, status, loading: false }));
		} catch (e: any) {
			_update(s => ({ ...s, loading: false, error: e.detail || 'Login failed' }));
			throw e;
		}
	},

	async logout() {
		_update(s => ({ ...s, loading: true, error: null }));
		try {
			await apiLogout();
			const status = await getAuthStatus();
			_update(s => ({ ...s, status, loading: false }));
		} catch (e: any) {
			_update(s => ({ ...s, loading: false, error: e.detail || 'Logout failed' }));
			throw e;
		}
	},

	clearError() {
		_update(s => ({ ...s, error: null }));
	},

	setError(error: string) {
		_update(s => ({ ...s, error }));
	}
};

// ============================================
// Derived stores (via store-utils createDerived)
// ============================================

export const isAuthenticated = createDerived(() => _state.status?.authenticated ?? false);
export const isAdmin = createDerived(() => _state.status?.is_admin ?? false);
export const setupRequired = createDerived(() => _state.status?.setup_required ?? false);
export const claudeAuthenticated = createDerived(() => _state.status?.claude_authenticated ?? false);
// Honest token-validity: true only when the Claude OAuth token is present AND
// not expired. Older backends omit the field — fall back to claude_authenticated
// so existing installs don't show a spurious "expired" warning.
export const claudeTokenValid = createDerived(
	() => _state.status?.claude_token_valid ?? _state.status?.claude_authenticated ?? false
);
export const githubAuthenticated = createDerived(() => _state.status?.github_authenticated ?? false);
export const onboardingComplete = createDerived(() => _state.status?.onboarding_complete ?? true);
export const username = createDerived(() => _state.status?.username ?? null);
export const authLoading = createDerived(() => _state.loading);
export const authError = createDerived(() => _state.error);
