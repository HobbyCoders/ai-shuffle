/**
 * Credentials Store - Track credential status for API users
 *
 * This store manages the credential status for the current API user,
 * enabling UI components to conditionally show/hide features based on
 * whether required API keys are configured and what policies apply.
 */

import { writable, derived, get } from 'svelte/store';
import { apiUser } from './auth';
import { getMyCredentials, type CredentialStatus, type AllCredentialsResponse } from '$lib/api/userSelfService';

// ============================================================================
// Types
// ============================================================================

export interface CredentialsState {
	credentials: CredentialStatus[];
	hasMissingRequired: boolean;
	loading: boolean;
	error: string | null;
	loaded: boolean;
}

// ============================================================================
// Store
// ============================================================================

const initialState: CredentialsState = {
	credentials: [],
	hasMissingRequired: false,
	loading: false,
	error: null,
	loaded: false
};

function createCredentialsStore() {
	const { subscribe, set, update } = writable<CredentialsState>(initialState);

	return {
		subscribe,

		/**
		 * Load credentials for the current API user
		 */
		async load(): Promise<void> {
			// Only load for API users
			const user = get(apiUser);
			if (!user) {
				set(initialState);
				return;
			}

			update(s => ({ ...s, loading: true, error: null }));

			try {
				const response: AllCredentialsResponse = await getMyCredentials();
				update(s => ({
					...s,
					credentials: response.credentials,
					hasMissingRequired: response.has_missing_required,
					loading: false,
					loaded: true
				}));
			} catch (e: any) {
				console.error('Failed to load credentials:', e);
				update(s => ({
					...s,
					loading: false,
					error: e.detail || e.message || 'Failed to load credentials'
				}));
			}
		},

		/**
		 * Refresh credentials (alias for load)
		 */
		async refresh(): Promise<void> {
			return this.load();
		},

		/**
		 * Reset to initial state (on logout)
		 */
		reset(): void {
			set(initialState);
		}
	};
}

export const credentials = createCredentialsStore();

// ============================================================================
// Derived Stores & Helpers
// ============================================================================

/**
 * Check if a specific credential is available (either user-set or admin-provided)
 */
export const hasCredential = derived(credentials, ($credentials) => {
	return (credentialType: string): boolean => {
		const cred = $credentials.credentials.find(c => c.credential_type === credentialType);
		if (!cred) return false;

		// Available if user has set it, OR admin has provided it (for admin_provided or optional policies)
		return cred.is_set || cred.admin_available;
	};
});

/**
 * Check if a feature can be used based on credential requirements
 * Returns true if:
 * - The credential is set by the user, OR
 * - The credential is provided by admin (policy is admin_provided or optional with admin fallback)
 */
export const canUseFeature = derived(credentials, ($credentials) => {
	return (credentialType: string): boolean => {
		const cred = $credentials.credentials.find(c => c.credential_type === credentialType);
		if (!cred) {
			// Credential not in list - might not be configured, assume available
			return true;
		}

		// If policy is admin_provided, admin must have set it
		if (cred.policy === 'admin_provided') {
			return cred.admin_available;
		}

		// If policy is user_provided, user must have set it
		if (cred.policy === 'user_provided') {
			return cred.is_set;
		}

		// If policy is optional, either user or admin can provide
		if (cred.policy === 'optional') {
			return cred.is_set || cred.admin_available;
		}

		return false;
	};
});

/**
 * Get the status of a specific credential
 */
export const getCredentialStatus = derived(credentials, ($credentials) => {
	return (credentialType: string): CredentialStatus | null => {
		return $credentials.credentials.find(c => c.credential_type === credentialType) || null;
	};
});

/**
 * Check if OpenAI features (transcription, TTS) are available
 */
export const canUseOpenAI = derived(canUseFeature, ($canUseFeature) => {
	return $canUseFeature('openai_api_key');
});

/**
 * Check if credentials have been loaded
 */
export const credentialsLoaded = derived(credentials, ($credentials) => $credentials.loaded);

/**
 * Check if there are missing required credentials
 */
export const hasMissingRequiredCredentials = derived(credentials, ($credentials) => $credentials.hasMissingRequired);
