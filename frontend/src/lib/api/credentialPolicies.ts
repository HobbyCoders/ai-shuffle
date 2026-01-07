/**
 * Credential Policies API Client (Admin)
 *
 * API functions for administrators to manage credential policies that control
 * whether users must provide their own API keys or can use admin-provided ones.
 */

import { api } from './client';

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
	return api.get<AllCredentialPoliciesResponse>('/settings/credential-policies');
}

// Get credential policies summary (with admin key status and effective status)
export async function getCredentialPoliciesSummary(): Promise<CredentialPoliciesSummaryResponse> {
	return api.get<CredentialPoliciesSummaryResponse>('/settings/credential-policies/summary');
}

// Update a credential policy (admin only)
export async function updateCredentialPolicy(
	policyId: string,
	policy: CredentialPolicyType
): Promise<{ success: boolean; id: string; policy: CredentialPolicyType }> {
	return api.put<{ success: boolean; id: string; policy: CredentialPolicyType }>(
		`/settings/credential-policies/${policyId}`,
		{ policy }
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

// ============================================================================
// Per-User Credential Policies
// ============================================================================

export interface UserCredentialPolicy {
	credential_type: string;
	name: string;
	description: string;
	policy: CredentialPolicyType;
	source: 'user' | 'global';
	global_policy: CredentialPolicyType;
}

export interface UserCredentialPoliciesResponse {
	user_id: string;
	user_name: string;
	policies: UserCredentialPolicy[];
}

// Get credential policies for a specific user
export async function getUserCredentialPolicies(userId: string): Promise<UserCredentialPoliciesResponse> {
	return api.get<UserCredentialPoliciesResponse>(`/settings/users/${userId}/credential-policies`);
}

// Set a credential policy override for a specific user
export async function setUserCredentialPolicy(
	userId: string,
	credentialType: string,
	policy: CredentialPolicyType
): Promise<{ success: boolean; user_id: string; credential_type: string; policy: CredentialPolicyType; source: string }> {
	return api.put<{ success: boolean; user_id: string; credential_type: string; policy: CredentialPolicyType; source: string }>(
		`/settings/users/${userId}/credential-policies/${credentialType}`,
		{ policy }
	);
}

// Delete a credential policy override (revert to global)
export async function deleteUserCredentialPolicy(
	userId: string,
	credentialType: string
): Promise<{ success: boolean; user_id: string; credential_type: string; deleted: boolean; fallback_policy: CredentialPolicyType; source: string }> {
	return api.delete<{ success: boolean; user_id: string; credential_type: string; deleted: boolean; fallback_policy: CredentialPolicyType; source: string }>(
		`/settings/users/${userId}/credential-policies/${credentialType}`
	);
}
