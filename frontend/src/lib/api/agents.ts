/**
 * Agent Export/Import API types and functions
 */

import { api, API_BASE, type ApiError } from './client-core';
import type { Profile } from './types';

// ============================================================================
// Types
// ============================================================================

export interface AgentExportData {
	name: string;
	description: string | null;
	model: string | null;
	permission_mode: string | null;
	system_prompt_type: string | null;
	system_prompt_preset: string | null;
	system_prompt_content: string | null;
	system_prompt_append: string | null;
	system_prompt_inject_env: boolean | null;
	allowed_tools: string[] | null;
	disallowed_tools: string[] | null;
	enabled_agents: string[] | null;
	ai_tools: Record<string, boolean> | null;
	max_turns: number | null;
	max_buffer_size: number | null;
	include_partial_messages: boolean | null;
	continue_conversation: boolean | null;
	fork_session: boolean | null;
	setting_sources: string[] | null;
	env: Record<string, string> | null;
	thinking_mode?: 'default' | 'disabled' | 'adaptive' | 'think' | 'think_harder' | 'ultra_think';
	effort?: 'low' | 'medium' | 'high' | 'max' | null;
}

export interface AgentExport {
	version: string;
	type: string;
	exported_at: string;
	agent: AgentExportData;
}

export interface AgentImportRequest extends AgentExport {
	new_id?: string;
	new_name?: string;
}

// ============================================================================
// Functions
// ============================================================================

/**
 * Export an agent/profile as JSON and trigger download
 */
export async function exportAgent(profileId: string): Promise<void> {
	const response = await fetch(`${API_BASE}/profiles/${profileId}/export`, {
		method: 'GET',
		credentials: 'include'
	});

	if (!response.ok) {
		const error = await response.json().catch(() => ({ detail: 'Export failed' }));
		throw {
			detail: error.detail || 'Export failed',
			status: response.status
		} as ApiError;
	}

	// Get filename from Content-Disposition header
	const contentDisposition = response.headers.get('Content-Disposition');
	let filename = `${profileId}-agent.json`;
	if (contentDisposition) {
		const match = contentDisposition.match(/filename="?([^"]+)"?/);
		if (match) {
			filename = match[1];
		}
	}

	// Create blob and trigger download
	const blob = await response.blob();
	const url = URL.createObjectURL(blob);
	const a = document.createElement('a');
	a.href = url;
	a.download = filename;
	document.body.appendChild(a);
	a.click();
	document.body.removeChild(a);
	URL.revokeObjectURL(url);
}

/**
 * Get agent export data without downloading (for preview)
 */
export async function getAgentExportData(profileId: string): Promise<AgentExport> {
	return api.get<AgentExport>(`/profiles/${profileId}/export`);
}

/**
 * Import an agent from JSON data
 */
export async function importAgent(data: AgentImportRequest): Promise<Profile> {
	return api.post<Profile>('/profiles/import', data);
}

/**
 * Parse and validate agent export file
 */
export function parseAgentExportFile(content: string): AgentExport {
	const parsed = JSON.parse(content);

	// Basic validation
	if (typeof parsed !== 'object' || parsed === null) {
		throw new Error('Invalid JSON structure');
	}

	if (parsed.type !== 'ai-shuffle-agent') {
		throw new Error(`Invalid file type: ${parsed.type}. Expected 'ai-shuffle-agent'`);
	}

	if (!parsed.version) {
		throw new Error('Missing version field');
	}

	if (!parsed.agent || typeof parsed.agent !== 'object') {
		throw new Error('Missing or invalid agent data');
	}

	if (!parsed.agent.name) {
		throw new Error('Agent name is required');
	}

	return parsed as AgentExport;
}
