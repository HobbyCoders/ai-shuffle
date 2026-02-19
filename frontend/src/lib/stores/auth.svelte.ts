/**
 * Authentication store
 *
 * Migrated to Svelte 5 runes.
 */

import type { AuthStatus } from '$lib/api/client';
import { getAuthStatus, login as apiLogin, logout as apiLogout, setup as apiSetup } from '$lib/api/auth';

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
// Subscribe shim
// ============================================

type Subscriber<T> = (value: T) => void;

const _subscribers = new Set<Subscriber<AuthState>>();

function _notifySubscribers() {
	for (const fn of _subscribers) {
		fn(_state);
	}
}

function _update(fn: (s: AuthState) => AuthState) {
	_state = fn(_state);
	_notifySubscribers();
}

// ============================================
// Exported Store
// ============================================

export const auth = {
	get current() {
		return _state;
	},

	subscribe(fn: Subscriber<AuthState>) {
		_subscribers.add(fn);
		fn(_state);
		return () => {
			_subscribers.delete(fn);
		};
	},

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
// Derived stores with subscribe shim
// ============================================

function createDerived<T>(getter: () => T) {
	return {
		get current() {
			return getter();
		},
		subscribe(fn: Subscriber<T>) {
			const handler: Subscriber<AuthState> = () => fn(getter());
			_subscribers.add(handler);
			fn(getter());
			return () => {
				_subscribers.delete(handler);
			};
		}
	};
}

export const isAuthenticated = createDerived(() => _state.status?.authenticated ?? false);
export const isAdmin = createDerived(() => _state.status?.is_admin ?? false);
export const setupRequired = createDerived(() => _state.status?.setup_required ?? false);
export const claudeAuthenticated = createDerived(() => _state.status?.claude_authenticated ?? false);
export const githubAuthenticated = createDerived(() => _state.status?.github_authenticated ?? false);
export const onboardingComplete = createDerived(() => _state.status?.onboarding_complete ?? true);
export const username = createDerived(() => _state.status?.username ?? null);
export const authLoading = createDerived(() => _state.loading);
export const authError = createDerived(() => _state.error);
