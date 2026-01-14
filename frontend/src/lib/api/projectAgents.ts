/**
 * Project Agent Management API Client
 *
 * Provides functions for managing file-based agents in project directories.
 * Agents are stored as .md files in <project>/.claude/agents/ with YAML frontmatter.
 */

import { api } from './client';

// ============================================================================
// Types
// ============================================================================

export interface ProjectAgent {
	name: string;
	description: string;
	model: string | null;
	tools: string[] | null;
	file_path: string;
	created_at: string | null;
	updated_at: string | null;
}

export interface ProjectAgentDetail extends ProjectAgent {
	prompt: string;
	raw_content: string;
}

export interface CreateProjectAgentRequest {
	name: string;
	description: string;
	prompt: string;
	model?: string;
	tools?: string[];
}

export interface UpdateProjectAgentRequest {
	description?: string;
	prompt?: string;
	model?: string;
	tools?: string[];
}

export interface RenameProjectAgentRequest {
	new_name: string;
}

// ============================================================================
// API Functions
// ============================================================================

/**
 * Get all agents in a project's .claude/agents/ directory
 */
export async function getProjectAgents(projectId: string): Promise<ProjectAgent[]> {
	return api.get(`/projects/${projectId}/agents`);
}

/**
 * Get detailed information about a specific agent including full prompt
 */
export async function getProjectAgent(projectId: string, agentName: string): Promise<ProjectAgentDetail> {
	return api.get(`/projects/${projectId}/agents/${encodeURIComponent(agentName)}`);
}

/**
 * Create a new agent in the project
 */
export async function createProjectAgent(
	projectId: string,
	data: CreateProjectAgentRequest
): Promise<ProjectAgent> {
	return api.post(`/projects/${projectId}/agents`, data);
}

/**
 * Update an existing agent
 */
export async function updateProjectAgent(
	projectId: string,
	agentName: string,
	data: UpdateProjectAgentRequest
): Promise<ProjectAgent> {
	return api.put(`/projects/${projectId}/agents/${encodeURIComponent(agentName)}`, data);
}

/**
 * Rename an agent (changes the .md filename)
 */
export async function renameProjectAgent(
	projectId: string,
	agentName: string,
	newName: string
): Promise<ProjectAgent> {
	return api.post(`/projects/${projectId}/agents/${encodeURIComponent(agentName)}/rename`, {
		new_name: newName
	});
}

/**
 * Delete an agent (removes the .md file)
 */
export async function deleteProjectAgent(projectId: string, agentName: string): Promise<void> {
	return api.delete(`/projects/${projectId}/agents/${encodeURIComponent(agentName)}`);
}
