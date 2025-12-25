/**
 * User Self-Service API Client
 *
 * API functions for API users to manage their own credentials, GitHub connection, and profile.
 */

import { apiCall } from './client';

// Types
export interface UserProfile {
	id: string;
	name: string;
	username: string | null;
	description: string | null;
	project_id: string | null;
	profile_id: string | null;
	is_active: boolean;
	web_login_allowed: boolean;
	created_at: string;
	updated_at: string;
	last_used_at: string | null;
}

export interface CredentialStatus {
	credential_type: string;
	name: string;
	description: string;
	policy: 'admin_provided' | 'user_provided' | 'optional';
	is_set: boolean;
	admin_available: boolean;
	masked_value: string | null;
}

export interface AllCredentialsResponse {
	credentials: CredentialStatus[];
	has_missing_required: boolean;
}

export interface CredentialRequirement {
	credential_type: string;
	name: string;
	description: string;
	is_set: boolean;
	admin_available?: boolean;
}

export interface CredentialRequirements {
	required: CredentialRequirement[];
	optional: CredentialRequirement[];
	admin_provided: CredentialRequirement[];
}

export interface GitHubConfig {
	connected: boolean;
	github_username: string | null;
	github_avatar_url: string | null;
	default_repo: string | null;
	default_branch: string | null;
}

export interface GitHubRepo {
	full_name: string;
	name: string;
	owner: string;
	private: boolean;
	description: string | null;
	default_branch: string;
}

export interface GitHubBranch {
	name: string;
	is_default: boolean;
}

// Profile endpoints
export async function getMyProfile(): Promise<UserProfile> {
	return apiCall<UserProfile>('/api/v1/me');
}

export async function updateMyProfile(data: { name?: string; description?: string }): Promise<UserProfile> {
	return apiCall<UserProfile>('/api/v1/me', {
		method: 'PUT',
		body: JSON.stringify(data)
	});
}

export async function changeMyPassword(currentPassword: string, newPassword: string): Promise<{ success: boolean; message: string }> {
	return apiCall<{ success: boolean; message: string }>('/api/v1/me/password', {
		method: 'PUT',
		body: JSON.stringify({
			current_password: currentPassword,
			new_password: newPassword
		})
	});
}

// Credentials endpoints
export async function getMyCredentials(): Promise<AllCredentialsResponse> {
	return apiCall<AllCredentialsResponse>('/api/v1/me/credentials');
}

export async function getCredentialRequirements(): Promise<CredentialRequirements> {
	return apiCall<CredentialRequirements>('/api/v1/me/credentials/requirements');
}

export async function setMyCredential(credentialType: string, value: string): Promise<{ success: boolean; credential_type: string; masked_value: string }> {
	return apiCall<{ success: boolean; credential_type: string; masked_value: string }>(`/api/v1/me/credentials/${credentialType}`, {
		method: 'POST',
		body: JSON.stringify({ value })
	});
}

export async function deleteMyCredential(credentialType: string): Promise<{ success: boolean; credential_type: string; warning: string | null }> {
	return apiCall<{ success: boolean; credential_type: string; warning: string | null }>(`/api/v1/me/credentials/${credentialType}`, {
		method: 'DELETE'
	});
}

// GitHub endpoints
export async function getMyGitHubConfig(): Promise<GitHubConfig> {
	return apiCall<GitHubConfig>('/api/v1/me/github');
}

export async function connectGitHub(personalAccessToken: string): Promise<{ success: boolean; github_username: string; github_avatar_url: string }> {
	return apiCall<{ success: boolean; github_username: string; github_avatar_url: string }>('/api/v1/me/github/connect', {
		method: 'POST',
		body: JSON.stringify({ personal_access_token: personalAccessToken })
	});
}

export async function disconnectGitHub(): Promise<{ success: boolean }> {
	return apiCall<{ success: boolean }>('/api/v1/me/github', {
		method: 'DELETE'
	});
}

export async function listMyGitHubRepos(page: number = 1, perPage: number = 30): Promise<{ repos: GitHubRepo[]; page: number; per_page: number }> {
	return apiCall<{ repos: GitHubRepo[]; page: number; per_page: number }>(`/api/v1/me/github/repos?page=${page}&per_page=${perPage}`);
}

export async function listGitHubBranches(owner: string, repo: string): Promise<{ branches: GitHubBranch[]; default_branch: string }> {
	return apiCall<{ branches: GitHubBranch[]; default_branch: string }>(`/api/v1/me/github/branches/${owner}/${repo}`);
}

export async function setGitHubDefaults(defaultRepo?: string, defaultBranch?: string): Promise<{ success: boolean; default_repo: string | null; default_branch: string | null }> {
	return apiCall<{ success: boolean; default_repo: string | null; default_branch: string | null }>('/api/v1/me/github/config', {
		method: 'POST',
		body: JSON.stringify({ default_repo: defaultRepo, default_branch: defaultBranch })
	});
}
