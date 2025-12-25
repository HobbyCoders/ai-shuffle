/**
 * Credential Policies API Client (Admin)
 *
 * API functions for administrators to manage credential policies that control
 * whether users must provide their own API keys or can use admin-provided ones.
 */

import { apiCall } from './client';

// Types
export type CredentialPolicyType = 'admin_provided' | 'user_provided' | 'optional';

export interface CredentialPolicy {
	id: string;
	policy: CredentialPolicyType;
	description: string | null;
	updated_at: string;
}

export interface CredentialPolicySummary {
	id: string;
	name: string;
	description: string;
	policy: CredentialPolicyType;
	admin_has_key: boolean;
	effective_status: 'admin_provides' | 'needs_admin_key' | 'user_must_provide' | 'optional_with_fallback' | 'optional_no_fallback';
	updated_at: string;
}

export interface AllCredentialPoliciesResponse {
	policies: CredentialPolicy[];
}

export interface CredentialPoliciesSummaryResponse {
	policies: CredentialPolicySummary[];
}

// Get all credential policies
export async function getCredentialPolicies(): Promise<AllCredentialPoliciesResponse> {
	return apiCall<AllCredentialPoliciesResponse>('/api/v1/settings/credential-policies');
}

// Get credential policies summary (with admin key status and effective status)
export async function getCredentialPoliciesSummary(): Promise<CredentialPoliciesSummaryResponse> {
	return apiCall<CredentialPoliciesSummaryResponse>('/api/v1/settings/credential-policies/summary');
}

// Update a credential policy (admin only)
export async function updateCredentialPolicy(
	policyId: string,
	policy: CredentialPolicyType
): Promise<{ success: boolean; id: string; policy: CredentialPolicyType }> {
	return apiCall<{ success: boolean; id: string; policy: CredentialPolicyType }>(
		`/api/v1/settings/credential-policies/${policyId}`,
		{
			method: 'PUT',
			body: JSON.stringify({ policy })
		}
	);
}

// Helper function to get user-friendly policy labels
export function getPolicyLabel(policy: CredentialPolicyType): string {
	switch (policy) {
		case 'admin_provided':
			return 'Admin Provides';
		case 'user_provided':
			return 'User Must Provide';
		case 'optional':
			return 'Optional (User Override)';
		default:
			return policy;
	}
}

// Helper function to get user-friendly effective status labels
export function getEffectiveStatusLabel(status: CredentialPolicySummary['effective_status']): string {
	switch (status) {
		case 'admin_provides':
			return 'Admin provides this key for all users';
		case 'needs_admin_key':
			return 'Set to admin-provided but admin key not configured';
		case 'user_must_provide':
			return 'Each user must provide their own key';
		case 'optional_with_fallback':
			return 'Users can optionally override admin key';
		case 'optional_no_fallback':
			return 'Optional, but no admin fallback configured';
		default:
			return status;
	}
}

// Get color for effective status
export function getEffectiveStatusColor(status: CredentialPolicySummary['effective_status']): 'green' | 'yellow' | 'red' | 'blue' | 'gray' {
	switch (status) {
		case 'admin_provides':
			return 'green';
		case 'needs_admin_key':
			return 'red';
		case 'user_must_provide':
			return 'blue';
		case 'optional_with_fallback':
			return 'green';
		case 'optional_no_fallback':
			return 'yellow';
		default:
			return 'gray';
	}
}
